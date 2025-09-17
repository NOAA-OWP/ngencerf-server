import base64
import json
import logging
import os
import time

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ForcingSourceEnum, ObservationalSourceEnum, GeopackageSourceEnum, JobGenesis
from calibration.models import CalibrationFormulation, CalibrationStopCriteria, Gage, CalibrationRun
from calibration.util.caching import get_cached_module_by_name
from calibration.util.calibration_validators import CalibrationRunSerializer, ExportResponseSerializer, ErrorResponseSerializer, \
    LoadCalibrationJobSerializer, LoadCalibrationRunResponseSerializer
from calibration.util.file_util import copy_directory, copy_file_to_directory, get_single_file
from calibration.util.geopkg import gpkg_to_png_selected_layers, get_geometry_from_gpkg
from calibration.util.ngen_locations import get_forcing_dir_for_job, get_observational_dir_for_job, get_geopackage_dir_for_job, \
    get_observational_file_for_job, get_ngen_logging_file
from calibration.views import ngen_cal_input
from calibration.views.calibration_formulation_views import get_sloth_parameters, validate_modules, SLOTH, add_sloth_parameters, validate_formulation
from calibration.views.calibration_gage_views import save_gage, get_data_files_status
from calibration.views.calibration_optimization_views import get_user_optimization, validate_optimizations, validate_objective_function, \
    write_optimization_inputs
from calibration.views.calibration_run_views import resolve_job_data_dir
from calibration.views.calibration_tuning_views import get_times, get_parameters_for_export, validate_and_save_times, validate_parameters, \
    save_parameters, get_time_range, has_user_selected_tuning_parameters
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, create_calibration_run_internal, \
    validate_request, get_valid_path, truncate_large_fields, get_user_email, generate_ngen_logging_config, get_elapsed_str, readonly_transaction
from calibration.views.data_services import DataServicesException, get_module_metadata_from_data_services, get_geopackage_from_data_services, \
    get_forcing_data_from_s3, get_observational_data_from_data_services

logger = logging.getLogger(__name__)


def import_calibration_run_data(request: Request, calibration_run_data: dict, genesis: JobGenesis, run: CalibrationRun = None) -> tuple[
    CalibrationRun | None, dict | None, Response | None]:
    """
    Imports calibration run data and creates a new CalibrationRun instance if successful.  Also used in cloning

    :param request: Django HTTP request with user details.
    :param calibration_run_data: Dictionary with calibration run data.
    :param genesis: Enum value indicating the origin of the job.
    :param run: Optional CalibrationRun to update.  If None, a new CalibrationRun is created.
    :return: Tuple containing CalibrationRun instance, response_dict, and optional ResponseError.
    """
    with transaction.atomic():
        run = run if run else create_calibration_run_internal(request.user, genesis)

        errors = []
        warnings = []
        eds_errors = []

        formulation_errors: list[str] = []
        formulation_warnings: list[str] = []
        modules = None
        have_lstm = False

        #############################
        # Gage
        #############################
        gage_id = calibration_run_data.get('gage_id')
        if gage_id:
            try:
                save_gage(run, gage_id)
            except Gage.DoesNotExist:
                return None, None, ResponseError(f"Gage '{gage_id}' does not exist or is not active", http_status=status.HTTP_404_NOT_FOUND)

            #############################
            # Formulations and Modules
            #############################
            modules_list = calibration_run_data.get('modules')
            module_names = set(modules_list) if modules_list else set()

            # Validate module names
            error_message = validate_modules(module_names)
            if error_message:
                return None, None, ResponseError(error_message)

            # TODO Eventually, we will have more user properties that are specific to certain modules
            # so we'll need a separate table to control those.
            # For now, we are forced to hard-code module names and specific flags
            # Only allow AET Rootzone to be True if CFE is included in the formulation
            run.is_aet_rootzone = calibration_run_data.get('is_aet_rootzone', False)
            if run.is_aet_rootzone and not any(cfe in module_names for cfe in ('CFE-S', 'CFE-X')):
                return None, None, ResponseError('AET Rootzone cannot be True for formulations not using CFE.')

            formulation_errors, formulation_warnings, _ = validate_formulation(module_names)
            have_lstm = 'LSTM' in module_names

            sloth_parameters = calibration_run_data.get('sloth_parameters')
            use_sloth = calibration_run_data.get('use_sloth')
            if have_lstm and (sloth_parameters or use_sloth):
                return None, None, ResponseError("You cannot specify sloth_parameters or use_sloth when using LSTM")

            # Set formulation name
            run.user_formulation_name = calibration_run_data.get('formulation_name')

            # Handling of sloth parameters
            run.use_sloth = use_sloth

            sloth_parameters = sloth_parameters
            if not run.use_sloth and sloth_parameters:
                return None, None, ResponseError(f"You must indicate 'use_sloth' is True to allow {SLOTH} parameters to be specified")

            if run.use_sloth and not sloth_parameters:
                return None, None, ResponseError(f"If you indicate 'use_sloth', you must enter {SLOTH} parameters")

            # Create any new formulations and process sloth parameters
            for m_name in module_names:
                module_instance = get_cached_module_by_name(m_name)
                CalibrationFormulation.objects.get_or_create(calibration_run=run, module=module_instance)

            error_message = add_sloth_parameters(run, sloth_parameters, module_names)
            if error_message:
                return None, None, ResponseError(error_message)

            # Get the list of modules for this Run
            modules = CalibrationFormulation.objects.filter(calibration_run=run)

            if modules and run.gage:
                try:
                    get_module_metadata_from_data_services(run, modules)  # type: ignore
                except DataServicesException as e:
                    errors.append(f"Error retrieving module parameter data from Data Services - status code: {e.status_code} - {str(e)}")
                    eds_errors.append({
                        'name': 'parameters',
                        'message': str(e),
                        'status_code': e.status_code if e.status_code else None
                    })

        # Note that for EDS, only the paths are copied.  The files will be copied to the job-specific directory in ready_to_run
        #############################
        # Geopackage Handling
        #############################
        geopackage_source_name = calibration_run_data.get('geopackage_source')
        geopackage_user_uploaded_file_path = calibration_run_data.get('geopackage_user_uploaded_file_path')

        run.geopackage_source = GeopackageSourceEnum.get_instance(geopackage_source_name) if geopackage_source_name else None

        if run.geopackage_source == GeopackageSourceEnum.UPLOAD.db_instance:
            if geopackage_user_uploaded_file_path and os.path.exists(geopackage_user_uploaded_file_path):
                # Copy file to job-specific directory
                copy_file_to_directory(geopackage_user_uploaded_file_path, get_geopackage_dir_for_job(run))
            else:
                if geopackage_user_uploaded_file_path:
                    errors.append(f"User uploaded geopackage data from '{geopackage_user_uploaded_file_path}' not found")
        else:
            # Fetch geopackage from Data Services
            try:
                get_geopackage_from_data_services(run)
            except DataServicesException as e:
                errors.append(f"Error retrieving geopackage data from Data Services - status code: {e.status_code} - {str(e)}")
                eds_errors.append({
                    'name': 'geopackage',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })
        #############################
        # Forcing Data Handling
        #############################
        forcing_source_name = calibration_run_data.get('forcing_source')
        forcing_user_uploaded_dir_path = calibration_run_data.get('forcing_user_uploaded_dir_path')

        if forcing_source_name:
            run.forcing_source_requested = run.forcing_source_actual = ForcingSourceEnum.get_instance(forcing_source_name)

        if run.forcing_source_requested == ForcingSourceEnum.UPLOAD.db_instance:
            if forcing_user_uploaded_dir_path and os.path.exists(forcing_user_uploaded_dir_path):
                # Copy directory to job-specific path
                copy_directory(forcing_user_uploaded_dir_path, get_forcing_dir_for_job(run))
            else:
                if forcing_user_uploaded_dir_path:
                    errors.append(f"User uploaded forcing data from '{forcing_user_uploaded_dir_path}' not found")
        else:
            # Fetch forcing data from S3
            try:
                if gage_id and run.forcing_source_requested:
                    get_forcing_data_from_s3(run, run.forcing_source_requested.name)
            except DataServicesException as e:
                errors.append(f"Error retrieving forcing data from Data Services - status code: {e.status_code} - {str(e)}")
                eds_errors.append({
                    'name': 'forcing',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })

        #############################
        # Observational Data Handling
        #############################
        observational_source_name = calibration_run_data.get('observational_source')
        observational_user_uploaded_file_path = calibration_run_data.get('observational_user_uploaded_file_path')

        run.observational_source = ObservationalSourceEnum.get_instance(observational_source_name) if observational_source_name else None

        if run.observational_source == ObservationalSourceEnum.UPLOAD.db_instance:
            if observational_user_uploaded_file_path and os.path.exists(observational_user_uploaded_file_path):
                # Copy file to job-specific path
                copy_file_to_directory(observational_user_uploaded_file_path, get_observational_dir_for_job(run))
            else:
                if observational_user_uploaded_file_path:
                    errors.append(f"User uploaded observational data from '{observational_user_uploaded_file_path}' not found")
        else:
            try:
                if gage_id:
                    get_observational_data_from_data_services(run)
                    if run.forcing_source_requested != run.forcing_source_actual:
                        warnings.append(
                            f'{run.forcing_source_requested.name} forcing data not found.  Using {run.forcing_source_actual.name if run.forcing_source_actual else None}')
            except DataServicesException as e:
                errors.append(f"Error retrieving observational data from Data Services - status code: {e.status_code} - {str(e)}")
                eds_errors.append({
                    'name': 'observational',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })

        #############################
        # Tuning
        #############################
        parameters = calibration_run_data.get('parameters')
        automatic_validation = calibration_run_data.get('automatic_validation')  # defaults to True
        if have_lstm and parameters:
            return None, None, ResponseError("You cannot specify parameters when using LSTM")

        if parameters and not modules:
            return None, None, ResponseError('Parameters cannot be specified without modules')

        # Don't bother validating parameters if we got a Data Services error
        if not any(error.get('name') == 'parameters' for error in eds_errors):
            parameter_errors, parameter_warnings = validate_parameters(run, parameters)
            if parameter_errors:
                return None, None, ResponseError(parameter_errors)

            save_parameters(run, parameters, allow_nulls=True)

        # Set automatic validation flags
        run.automatic_validation = automatic_validation

        calibration_times = calibration_run_data.get('calibration_times')
        validation_times = calibration_run_data.get('validation_times')

        if not run.automatic_validation and validation_times:
            return None, None, ResponseError('validation_times cannot be specified unless automatic_validation is True')

        error_message = validate_and_save_times(run, calibration_times, validation_times)
        if error_message:
            return None, None, ResponseError(error_message)

        #############################
        # Optimization
        #############################
        optimization_name = calibration_run_data.get('optimization')
        objective_function_name = calibration_run_data.get('objective_function')
        streamflow_threshold = calibration_run_data.get('streamflow_threshold')
        peak_flow_threshold = calibration_run_data.get('peak_flow_threshold')
        optimization_inputs = calibration_run_data.get('optimization_inputs')
        stop_criteria = calibration_run_data.get('stop_criteria')
        save_plot_iteration_frequency = calibration_run_data.get('save_plot_iteration_frequency')
        save_output_iteration = calibration_run_data.get('save_output_iteration')
        if have_lstm and (
                optimization_name or objective_function_name or
                streamflow_threshold is not None or peak_flow_threshold is not None or optimization_name or
                stop_criteria is not None or
                save_plot_iteration_frequency is not None or save_output_iteration
        ):
            return None, None, ResponseError(
                "You cannot specify optimization_name, objective_function_name, streamflow_threshold, peak_flow_threshold, "
                "stop_criteria, save_plot_iteration_frequency or save_output_iteration when using LSTM")

        if not optimization_name:
            if optimization_inputs:
                return None, None, ResponseError('Optimization inputs cannot be specified without an optimization name')
        else:
            optimization, prepared_inputs, error_message = validate_optimizations(run, optimization_name, optimization_inputs)
            if error_message:
                return None, None, ResponseError(error_message)
            write_optimization_inputs(run, prepared_inputs)

        error_message = validate_objective_function(run, objective_function_name, streamflow_threshold, peak_flow_threshold)
        if error_message:
            return None, None, ResponseError(error_message)

        # Set run parameters and save
        run.save_plot_iteration_frequency = save_plot_iteration_frequency
        run.save_output_iteration = bool(save_output_iteration) if save_output_iteration is not None else False
        run.streamflow_threshold = streamflow_threshold
        run.peak_flow_threshold = peak_flow_threshold

        if stop_criteria is not None:
            # I'm assuming for now that there is just one CalibrationStopCriteria for this run, but that might change in the future
            CalibrationStopCriteria.objects.update_or_create(calibration_run=run, defaults={"value": stop_criteria})

        #############################
        # Logging
        #############################
        # Get logging_config which has been imported from json
        logging_config = calibration_run_data.get('logging_config')

        if logging_config:
            # Create a logging_config_import file with the imported data
            logging_config_path = get_ngen_logging_file(run, import_flag=True)
            os.makedirs(os.path.dirname(logging_config_path), exist_ok=True)
            with open(logging_config_path, 'w') as f:
                json.dump(logging_config, f, indent=4)

        run.save()
    messages = {}
    if errors:
        messages['errors'] = errors + formulation_errors
    if formulation_warnings:
        messages['warnings'] = formulation_warnings
    if warnings:
        messages.setdefault('warnings', []).extend(warnings)
    if eds_errors:
        messages['eds_errors'] = eds_errors

    return run, messages, None


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: ExportResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Export a job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def export_job(request: Request) -> Response:
    """
    API endpoint to export calibration job data.
    Runs in READ ONLY mode to reduce contention.

    :param request: Django HTTP request, with parameters in the body for POST or query params for GET.
    :return: Response containing the exported calibration run data or an error.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        calibration_run_data = load_calibration_run_data(run, export=True)

    error_object, _ = ngen_cal_input.ready_to_run(run)
    if error_object:
        if error_object.has_warnings():
            calibration_run_data['metadata']['warnings'] = error_object.warnings
        if error_object.has_errors():
            calibration_run_data['metadata']['errors'] = error_object.errors

    response_validator, error_response = validate_response(ExportResponseSerializer, calibration_run_data)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def load_calibration_run_data(run: CalibrationRun, export: bool = False, include_gpkg_map: bool = False) -> dict:
    """
    Loads calibration run data for export, cloning or UI display.

    NOTE: This function does not itself open a transaction.
    It should always be called from a surrounding read-only transaction
    (see export_job and load_calibration_run) to ensure reduced lock contention.

    :param run: CalibrationRun instance for which data is being loaded.
    :param export: If True, formats the data for export, including all necessary paths for job re-import.
                   If False, formats the data for UI display with only essential details.
    :param include_gpkg_map: If True and export is False, generates a base64-encoded Geopackage map for display.
                             Ignored when export is True.
    :return: Dictionary containing the calibration run data.
    """
    start_time = time.time()
    logger.info(f"Starting load_calibration_run_data for Calibration Job {run.id} - {run.status.name}")

    calibration_run_data = {}

    #############################
    # Time Range
    #############################
    logger.info("Retrieving time range")
    time_range_start = time.time()
    time_range = get_time_range(run)

    # Manually serialize datetime objects since we're not using a serializer for metadata
    serialized_time_range = {}
    if time_range:
        if time_range.get('start_time'):
            serialized_time_range['start_time'] = time_range['start_time'].isoformat()
        if time_range.get('end_time'):
            serialized_time_range['end_time'] = time_range['end_time'].isoformat()
    logger.info(f"Time range retrieval completed in {time.time() - time_range_start:.2f}s")

    module_objects = CalibrationFormulation.objects.filter(calibration_run=run)

    geopackage_path = get_valid_path(run.geopackage_eds_file_path, lambda: get_single_file(get_geopackage_dir_for_job(run)))
    num_catchments = len(get_geometry_from_gpkg(geopackage_path)['catchments'].keys()) if geopackage_path and os.path.exists(
        geopackage_path) else None

    #############################
    # Export or Clone Mode
    #############################
    if export:
        export_start = time.time()
        metadata = {
            'source_calibration_run_id': run.id,
            'source_status': run.status.name,
            'time_range': serialized_time_range,
            'job_data_dir': resolve_job_data_dir(run),
            'num_catchments': num_catchments,
            'forcing_source_actual': run.forcing_source_actual.name if run.forcing_source_actual else None

        }
        calibration_run_data['metadata'] = metadata

        calibration_run_data['run_after_import'] = False

        calibration_run_data['gage_id'] = run.gage.gage_id if run.gage else None

        calibration_run_data['forcing_source'] = run.forcing_source_requested.name if run.forcing_source_requested else None

        calibration_run_data['parameters'] = get_parameters_for_export(module_objects)  # type: ignore

        # For export, we need these paths only for user-uploaded data, so we can copy the data to the newly imported job

        if run.geopackage_source == GeopackageSourceEnum.UPLOAD.db_instance:
            user_uploaded_geopackage_file = get_single_file(get_geopackage_dir_for_job(run))
            calibration_run_data[
                'geopackage_user_uploaded_file_path'] = user_uploaded_geopackage_file if user_uploaded_geopackage_file and os.path.exists(
                user_uploaded_geopackage_file) else None

        if run.observational_source == ObservationalSourceEnum.UPLOAD.db_instance:
            user_uploaded_observational_file = get_observational_file_for_job(run)
            calibration_run_data[
                'observational_user_uploaded_file_path'] = user_uploaded_observational_file if user_uploaded_observational_file and os.path.exists(
                user_uploaded_observational_file) else None

        if run.forcing_source_requested == ForcingSourceEnum.UPLOAD.db_instance:
            user_uploaded_forcing_dir = get_forcing_dir_for_job(run)
            calibration_run_data['forcing_user_uploaded_dir_path'] = user_uploaded_forcing_dir if user_uploaded_forcing_dir and os.path.exists(
                user_uploaded_forcing_dir) else None

        logger.info(f"Export data preparation completed in {time.time() - export_start:.2f}s")

    #############################
    # UI Display Mode (Non-Export)
    #############################
    else:
        calibration_run_data['job_data_dir'] = resolve_job_data_dir(run)

        ui_display_start = time.time()
        calibration_run_data['calibration_run_id'] = run.id
        calibration_run_data['submit_date'] = run.submit_date
        calibration_run_data['time_range'] = time_range

        # Gage information
        calibration_run_data['gage'] = {
            'gage_id': run.gage.gage_id,
            'agency': run.gage.agency,
            'station_name': run.gage.station_name if run.gage.station_name else "<unknown>",
            'latitude': run.gage.latitude,
            'longitude': run.gage.longitude,
            'altitude': run.gage.altitude
        } if run.gage else None
        calibration_run_data['num_catchments'] = num_catchments
        calibration_run_data['status'] = run.status.name

        calibration_run_data['forcing_source_requested'] = run.forcing_source_requested.name if run.forcing_source_requested else None
        calibration_run_data['forcing_source_actual'] = run.forcing_source_actual.name if run.forcing_source_actual else None

        # Generate Geopackage map if requested
        if include_gpkg_map:
            gpkg_map_start = time.time()
            geopackage_path = get_single_file(
                get_geopackage_dir_for_job(run)) if run.geopackage_source == GeopackageSourceEnum.UPLOAD.db_instance else run.geopackage_eds_file_path
            if geopackage_path and os.path.exists(geopackage_path):
                geopackage_png = gpkg_to_png_selected_layers(geopackage_path)
                base64_str = base64.b64encode(geopackage_png.getvalue()).decode('utf-8')
                calibration_run_data['geopackage_image_url'] = f'data:image/png;base64,{base64_str}'
            logger.info(f"Geopackage map generation completed in {time.time() - gpkg_map_start:.2f}s")

        # Determine external data status (whether required files are available)
        data_files_status_start = time.time()
        calibration_run_data['external_data_status'] = get_data_files_status(run)
        logger.info(f"Data Files status completed in {time.time() - data_files_status_start:.2f}s")

        calibration_run_data['parameters_selected'] = has_user_selected_tuning_parameters(module_objects)  # type: ignore
        logger.info(f"UI display data preparation completed in {time.time() - ui_display_start:.2f}s")

    #############################
    # Gage Data
    #############################
    logger.info("Processing gage data")
    gage_start = time.time()
    calibration_run_data['observational_source'] = run.observational_source.name if run.observational_source else None
    calibration_run_data['geopackage_source'] = run.geopackage_source.name if run.geopackage_source else None
    logger.info(f"Gage data processed in {time.time() - gage_start:.2f}s")

    #############################
    # Formulation Data
    #############################
    logger.info("Processing formulation data")
    formulation_start = time.time()

    calibration_run_data['formulation_name'] = run.user_formulation_name

    # Extract module names
    modules = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list('module__name', flat=True)
    )

    calibration_run_data['modules'] = modules

    calibration_run_data['is_aet_rootzone'] = run.is_aet_rootzone

    # Validation warnings
    formulation_errors, formulation_warnings, _ = validate_formulation(modules)
    if formulation_warnings and not export:
        calibration_run_data['formulation_warnings'] = formulation_warnings
    if formulation_errors and not export:
        calibration_run_data['formulation_errors'] = formulation_errors

    # Handle SLOTH parameters
    calibration_run_data['use_sloth'] = run.use_sloth
    if run.use_sloth:
        calibration_run_data['sloth_parameters'] = get_sloth_parameters(run)
    logger.info(f"Formulation data processed in {time.time() - formulation_start:.2f}s")

    #############################
    # Tuning Data
    #############################
    logger.info("Processing turning data")
    tuning_start = time.time()

    calibration_run_data['automatic_validation'] = run.automatic_validation

    calibration_times, validation_times = get_times(run)
    calibration_run_data['calibration_times'] = calibration_times
    calibration_run_data['validation_times'] = validation_times

    logger.info(f"Tuning data processed in {time.time() - tuning_start:.2f}s")

    #############################
    # Optimization Data
    #############################
    logger.info("Processing optimization data")
    optimization_start = time.time()

    calibration_run_data['objective_function'] = run.objective_function.name if run.objective_function else None
    calibration_run_data['streamflow_threshold'] = run.streamflow_threshold
    calibration_run_data['peak_flow_threshold'] = run.peak_flow_threshold

    # Fetch optimization details
    optimization, optimization_inputs = get_user_optimization(run)
    calibration_run_data['optimization'] = optimization
    calibration_run_data['optimization_inputs'] = optimization_inputs
    calibration_run_data['save_plot_iteration_frequency'] = run.save_plot_iteration_frequency
    calibration_run_data['save_output_iteration'] = run.save_output_iteration

    # Stop criteria
    calibration_stop_criteria = CalibrationStopCriteria.objects.filter(calibration_run=run).first()
    calibration_run_data['stop_criteria'] = calibration_stop_criteria.value if calibration_stop_criteria else None
    logger.info(f"Optimization data processed in {time.time() - optimization_start:.2f}s")

    # Export the logging data.
    calibration_run_data['logging_config'] = generate_ngen_logging_config(run)

    logger.info(f"load_calibration_run_data completed for Calibration Job {run.id} in {time.time() - start_time:.2f}s")
    return calibration_run_data


@extend_schema(
    request=LoadCalibrationJobSerializer,
    responses={
        200: LoadCalibrationRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Load all data for a previously saved calibration"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def load_calibration_run(request: Request) -> Response:
    """
    Load all data for a previously saved calibration run.
    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object.
    :return: A Response object containing the serialized calibration run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(LoadCalibrationJobSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    include_gpkg_map = validator.get('include_gpkg_map')

    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        calibration_run_data = load_calibration_run_data(run, export=False, include_gpkg_map=include_gpkg_map)

    response_validator, error_response = validate_response(
        LoadCalibrationRunResponseSerializer,
        calibration_run_data,
        fields_to_truncate=['geopackage_image_url']
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["geopackage_image_url"]))}'
    )

    return Response(response_validator.data)
