import base64
import json
import logging
import os
import time
from datetime import datetime

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ForcingSourceEnum, ObservationalSourceEnum, GeopackageSourceEnum, JobGenesis
from calibration.models import CalibrationFormulation, CalibrationStopCriteria, Gage, CalibrationRun
from calibration.util.caching import get_cached_module_by_name, get_cached_modules_by_id
from calibration.util.calibration_validators import CalibrationRunSerializer, ExportResponseSerializer, ErrorResponseSerializer, \
    LoadCalibrationJobSerializer, LoadCalibrationRunResponseSerializer
from calibration.util.cloud_util import path_exists
from calibration.util.file_util import copy_directory, copy_file_to_directory, get_single_file
from calibration.util.geopkg import gpkg_to_png_selected_layers, get_geometry_from_gpkg
from calibration.util.ngen_locations import get_forcing_dir_for_job, get_observational_dir_for_job, get_geopackage_dir_for_job, \
    get_observational_file_for_job, get_ngen_logging_file
from calibration.views import ngen_cal_input
from calibration.views.calibration_formulation_views import get_sloth_parameters, validate_modules, SLOTH, add_sloth_parameters, validate_formulation
from calibration.views.calibration_gage_views import save_gage, get_data_files_status
from calibration.views.calibration_optimization_views import get_user_optimization, validate_optimizations, validate_objective_function, \
    write_optimization_inputs
from calibration.views.calibration_run_views import resolve_job_data_dir, parse_failure_messages
from calibration.views.calibration_tuning_views import get_times, get_parameters_for_export, validate_and_save_times, validate_parameters, \
    save_parameters, has_user_selected_tuning_parameters, compute_time_range, persist_time_range
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, create_calibration_run_internal, \
    validate_request, get_valid_path, truncate_large_fields, get_user_email, generate_ngen_logging_config, get_elapsed_str, readonly_transaction, \
    format_datetime
from calibration.views.data_services import DataServicesException, get_module_metadata_from_data_services, get_geopackage_from_data_services, \
    get_forcing_data_from_s3, get_observational_data_from_data_services

logger = logging.getLogger(__name__)


def import_calibration_run_data(request: Request,
                                calibration_run_data: dict,
                                genesis: JobGenesis,
                                run: CalibrationRun = None
                                ) -> tuple[CalibrationRun | None, dict | None, Response | None]:
    """
    Imports calibration run data and creates a new CalibrationRun instance if successful.  Also used in cloning

    Minimal refactor:
      - Adds an initial READ-ONLY block for validations / preparation that do not write to the DB.
      - Keeps a single WRITE block that performs all DB mutations in the same order as before.
      - IO (copying files, Data Services calls, etc.) is intentionally left where it was.

    :param request: Django HTTP request with user details.
    :param calibration_run_data: Dictionary with calibration run data.
    :param genesis: Enum indicating the origin of the job.
    :param run: Optional CalibrationRun to update. If None, a new CalibrationRun is created.
    :return: Tuple containing CalibrationRun instance, response_dict, and optional ResponseError.
    """
    # ---------------------------------------------------------------------
    # Initialize containers used across phases
    # ---------------------------------------------------------------------
    errors: list[str] = []
    warnings: list[str] = []
    eds_errors: list[dict] = []
    formulation_errors: list[str] = []
    formulation_warnings: list[str] = []
    have_lstm = False

    # Inputs pulled once
    gage_id = calibration_run_data.get('gage_id')
    modules_list = calibration_run_data.get('modules')
    module_names = set(modules_list) if modules_list else set()
    sloth_parameters = calibration_run_data.get('sloth_parameters')
    use_sloth = calibration_run_data.get('use_sloth')
    parameters = calibration_run_data.get('parameters')
    automatic_validation = calibration_run_data.get('automatic_validation')  # defaults handled later on run
    calibration_times = calibration_run_data.get('calibration_times')
    validation_times = calibration_run_data.get('validation_times')
    optimization_name = calibration_run_data.get('optimization')
    objective_function_name = calibration_run_data.get('objective_function')
    streamflow_threshold = calibration_run_data.get('streamflow_threshold')
    peak_flow_threshold = calibration_run_data.get('peak_flow_threshold')
    optimization_inputs = calibration_run_data.get('optimization_inputs')
    stop_criteria = calibration_run_data.get('stop_criteria')
    save_plot_iteration_frequency = calibration_run_data.get('save_plot_iteration_frequency')
    save_output_iteration = calibration_run_data.get('save_output_iteration')
    logging_config = calibration_run_data.get('logging_config')

    # These are used if the sources are uploads (paths provided by the client)
    geopackage_source_name = calibration_run_data.get('geopackage_source')
    geopackage_user_uploaded_file_path = calibration_run_data.get('geopackage_user_uploaded_file_path')
    forcing_source_name = calibration_run_data.get('forcing_source')
    forcing_user_uploaded_dir_path = calibration_run_data.get('forcing_user_uploaded_dir_path')
    observational_source_name = calibration_run_data.get('observational_source')
    observational_user_uploaded_file_path = calibration_run_data.get('observational_user_uploaded_file_path')

    # Prepared (read-only) outputs
    prepared_inputs = None  # from validate_optimizations
    # Note: validate_objective_function / validate_optimizations assign fields on `run` in memory only; DB save happens later.

    # If a new run is needed, create it up front so we have an ID/paths.
    # This is a single write operation and intentionally left simple (no explicit transaction).
    if not run:
        run = create_calibration_run_internal(request.user, genesis)

    # ---------------------------------------------------------------------
    # READ-ONLY PHASE: validate & prepare (no DB writes)
    # ---------------------------------------------------------------------
    with readonly_transaction():
        # Validate modules list
        if module_names:
            error_message = validate_modules(module_names)
            if error_message:
                return None, None, ResponseError(error_message)

            # Formulation-level checks (read-only)
            f_errors, f_warnings, _ = validate_formulation(module_names)
            formulation_errors.extend(f_errors or [])
            formulation_warnings.extend(f_warnings or [])
            have_lstm = 'LSTM' in module_names

        # LSTM exclusivity checks
        if have_lstm and (sloth_parameters or use_sloth):
            return None, None, ResponseError("You cannot specify sloth_parameters or use_sloth when using LSTM")
        if have_lstm and (
                optimization_name or objective_function_name or
                streamflow_threshold is not None or peak_flow_threshold is not None or
                stop_criteria is not None or
                save_plot_iteration_frequency is not None or save_output_iteration
        ):
            return None, None, ResponseError(
                "You cannot specify optimization_name, objective_function_name, streamflow_threshold, peak_flow_threshold, "
                "stop_criteria, save_plot_iteration_frequency or save_output_iteration when using LSTM"
            )

        # Sloth parameter gating checks (no writes here)
        if use_sloth and not sloth_parameters:
            return None, None, ResponseError(f"If you indicate 'use_sloth', you must enter {SLOTH} parameters")
        if (not use_sloth) and sloth_parameters:
            return None, None, ResponseError(f"You must indicate 'use_sloth' is True to allow {SLOTH} parameters to be specified")

        # Validation times constraints (no DB writes here)
        if not automatic_validation and validation_times:
            return None, None, ResponseError('validation_times cannot be specified unless automatic_validation is True')

        # Optimization validations (assigns to `run` in memory only; no DB write)
        if optimization_name:
            _, prepared_inputs, error_message = validate_optimizations(run, optimization_name, optimization_inputs)
            if error_message:
                return None, None, ResponseError(error_message)
        else:
            if optimization_inputs:
                return None, None, ResponseError('Optimization inputs cannot be specified without an optimization name')

        # Objective function validation (assigns to `run` in memory only; no DB write)
        error_message = validate_objective_function(run, objective_function_name, streamflow_threshold, peak_flow_threshold)
        if error_message:
            return None, None, ResponseError(error_message)

        # If a gage_id was provided, ensure the gage exists (read-only check)
        if gage_id:
            try:
                # We don't persist here; existence check only. `save_gage` will persist later.
                _ = Gage.objects.get(gage_id=gage_id, is_active=True)
            except Gage.DoesNotExist:
                return None, None, ResponseError(
                    f"Gage '{gage_id}' does not exist or is not active",
                    http_status=status.HTTP_404_NOT_FOUND
                )

    # ---------------------------------------------------------------------
    # WRITE PHASE: perform DB mutations & keep IO where it was
    # ---------------------------------------------------------------------
    with transaction.atomic():
        # -----------------------------
        # Gage
        # -----------------------------
        if gage_id:
            # Persist the gage on the run
            try:
                save_gage(run, gage_id)
            except Gage.DoesNotExist:
                return None, None, ResponseError(
                    f"Gage '{gage_id}' does not exist or is not active",
                    http_status=status.HTTP_404_NOT_FOUND
                )

            # -----------------------------
            # Formulations & Modules
            # -----------------------------
            if module_names:
                # Persist formulations for this run
                for m_name in module_names:
                    module_instance = get_cached_module_by_name(m_name)
                    CalibrationFormulation.objects.get_or_create(calibration_run=run, module=module_instance)

                # Handle SLOTH parameters (persist)
                if use_sloth:
                    error_message = add_sloth_parameters(run, sloth_parameters, module_names)  # type: ignore[arg-type]
                    if error_message:
                        return None, None, ResponseError(error_message)

                # Refresh modules queryset on the run (needed for DS metadata and later steps)
                modules = CalibrationFormulation.objects.filter(calibration_run=run)

                # Pull parameter metadata for modules from Data Services (kept where it was)
                try:
                    if modules and run.gage:
                        get_module_metadata_from_data_services(run, modules)  # type: ignore
                except DataServicesException as e:
                    errors.append(f"Error retrieving module parameter data from Data Services - status code: {e.status_code} - {str(e)}")
                    eds_errors.append({
                        'name': 'parameters',
                        'message': str(e),
                        'status_code': e.status_code if e.status_code else None
                    })

        # -----------------------------
        # Geopackage
        # -----------------------------
        run.geopackage_source = GeopackageSourceEnum.get_instance(geopackage_source_name) if geopackage_source_name else None
        if run.geopackage_source == GeopackageSourceEnum.UPLOAD.db_instance:
            if geopackage_user_uploaded_file_path and os.path.exists(geopackage_user_uploaded_file_path):
                copy_file_to_directory(geopackage_user_uploaded_file_path, get_geopackage_dir_for_job(run))
            else:
                if geopackage_user_uploaded_file_path:
                    errors.append(f"User uploaded geopackage data from '{geopackage_user_uploaded_file_path}' not found")
        else:
            try:
                get_geopackage_from_data_services(run)
            except DataServicesException as e:
                errors.append(f"Error retrieving geopackage data from Data Services - status code: {e.status_code} - {str(e)}")
                eds_errors.append({
                    'name': 'geopackage',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })

        # -----------------------------
        # Forcing data
        # -----------------------------
        if forcing_source_name:
            run.forcing_source_requested = run.forcing_source_actual = ForcingSourceEnum.get_instance(forcing_source_name)
        if run.forcing_source_requested == ForcingSourceEnum.UPLOAD.db_instance:
            if forcing_user_uploaded_dir_path and os.path.exists(forcing_user_uploaded_dir_path):
                copy_directory(forcing_user_uploaded_dir_path, get_forcing_dir_for_job(run))
            else:
                if forcing_user_uploaded_dir_path:
                    errors.append(f"User uploaded forcing data from '{forcing_user_uploaded_dir_path}' not found")
        else:
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

        # -----------------------------
        # Observational data
        # -----------------------------
        run.observational_source = ObservationalSourceEnum.get_instance(observational_source_name) if observational_source_name else None
        if run.observational_source == ObservationalSourceEnum.UPLOAD.db_instance:
            if observational_user_uploaded_file_path and os.path.exists(observational_user_uploaded_file_path):
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
                            f'{run.forcing_source_requested.name} forcing data not found.  Using {run.forcing_source_actual.name if run.forcing_source_actual else None}'
                        )
            except DataServicesException as e:
                errors.append(f"Error retrieving observational data from Data Services - status code: {e.status_code} - {str(e)}")
                eds_errors.append({
                    'name': 'observational',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })

        # -----------------------------
        # Tuning (validate & persist)
        # -----------------------------
        run.automatic_validation = automatic_validation

        # Only validate parameters if we didn't hit DS parameter metadata errors
        if parameters and not any(error.get('name') == 'parameters' for error in eds_errors):
            # These validations read from DB; saving persists selections
            parameter_errors, parameter_warnings = validate_parameters(run, parameters)
            if parameter_errors:
                return None, None, ResponseError(parameter_errors)
            save_parameters(run, parameters, allow_nulls=True)

        # Times (persist)
        error_message = validate_and_save_times(run, calibration_times, validation_times)
        if error_message:
            return None, None, ResponseError(error_message)

        # -----------------------------
        # Optimization persistence
        # -----------------------------
        # prepared_inputs came from read-only phase (validate_optimizations)
        if prepared_inputs is not None:
            write_optimization_inputs(run, prepared_inputs)

        # Thresholds & iteration save flags
        run.save_plot_iteration_frequency = save_plot_iteration_frequency
        run.save_output_iteration = bool(save_output_iteration) if save_output_iteration is not None else False
        run.streamflow_threshold = streamflow_threshold
        run.peak_flow_threshold = peak_flow_threshold

        if stop_criteria is not None:
            # assuming single CalibrationStopCriteria
            CalibrationStopCriteria.objects.update_or_create(calibration_run=run, defaults={"value": stop_criteria})

        # -----------------------------
        # Logging config (IO left as-is)
        # -----------------------------
        if logging_config:
            logging_config_path = get_ngen_logging_file(run, import_flag=True)
            os.makedirs(os.path.dirname(logging_config_path), exist_ok=True)
            with open(logging_config_path, 'w') as f:
                json.dump(logging_config, f, indent=4)

        # Final persistence of run fields updated above
        run.use_sloth = use_sloth
        run.user_formulation_name = calibration_run_data.get('formulation_name')
        run.is_aet_rootzone = calibration_run_data.get('is_aet_rootzone', False)
        run.save()

    # ---------------------------------------------------------------------
    # Build response messages (unchanged)
    # ---------------------------------------------------------------------
    messages: dict = {}
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

    Read-only block: fetch run and load data.
    Post-processing: run ready_to_run (outside transactions).

    :param request: Django HTTP request, with parameters in the body for POST or query params for GET.
    :return: Response containing the exported calibration run data or an error.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    # -------------------------------------------------------------
    # Read-only block: fetch run and load data
    # -------------------------------------------------------------
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        calibration_run_data, _ = load_calibration_run_data(run, export=True)

    # -------------------------------------------------------------
    # Post-processing (not inside any transaction)
    # -------------------------------------------------------------
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


def load_calibration_run_data(run: CalibrationRun, export: bool = False, include_gpkg_map: bool = False) -> tuple[dict, dict[str, datetime | None]]:
    """
    Load calibration run data for export, cloning, or UI display.

    NOTE:
      - This function does not itself open a transaction or persist to the DB.
      - It should be called inside a read-only transaction for read-heavy cases
        (see export_job and load_calibration_run).
      - Persistence of the computed time range, if needed, is handled separately.

    :param run: CalibrationRun instance for which data is being loaded.
    :param export: If True, formats the data for export, including all necessary paths for job re-import.
                   If False, formats the data for UI display with only essential details.
    :param include_gpkg_map: If True and export is False, generates a base64-encoded Geopackage map for display.
                             Ignored when export is True.
    :return: A tuple of:
             - calibration_run_data: dict with job metadata and configuration,
             - time_range: dict with 'start_time' and 'end_time', or None if unavailable.
    """
    start_time = time.time()
    logger.info(f"Starting load_calibration_run_data for Calibration Job {run.id} - {run.status.name}")

    calibration_run_data: dict = {}

    #############################
    # Time Range (computed only)
    #############################
    logger.info("Retrieving time range (compute only)")
    time_range_start = time.time()
    time_range = compute_time_range(run)

    # Manually serialize datetime objects since we're not using a serializer for metadata
    serialized_time_range: dict[str, str] = {}
    if time_range:
        serialized_time_range['start_time'] = time_range['start_time'].isoformat()
        serialized_time_range['end_time'] = time_range['end_time'].isoformat()
    logger.info(f"Time range computation completed in {time.time() - time_range_start:.2f}s")

    # Get module IDs once for this run
    module_ids = list(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list('module_id', flat=True)
    )

    geopackage_path = get_valid_path(run.geopackage_eds_file_path, lambda: get_single_file(get_geopackage_dir_for_job(run)))
    num_catchments = len(get_geometry_from_gpkg(geopackage_path)['catchments'].keys()) if geopackage_path and os.path.exists(geopackage_path) else None

    #############################
    # Export or Clone Mode
    #############################
    if export:
        export_start = time.time()
        metadata = {
            'source_calibration_run_id': run.id,
            'last_updated_on': format_datetime(run.updated_at),
            'source_status': run.status.name,
            'time_range': serialized_time_range,
            'job_data_dir': resolve_job_data_dir(run),
            'num_catchments': num_catchments,
            'forcing_source_actual': run.forcing_source_actual.name if run.forcing_source_actual else None,
        }
        fm = parse_failure_messages(run.failure_messages)
        if fm is not None:
            metadata['failure_messages'] = fm

        calibration_run_data['metadata'] = metadata
        calibration_run_data['run_after_import'] = False
        calibration_run_data['gage_id'] = run.gage.gage_id if run.gage else None
        calibration_run_data['forcing_source'] = run.forcing_source_requested.name if run.forcing_source_requested else None

        # Use cached modules when exporting parameters
        calibration_run_data['parameters'] = get_parameters_for_export(run)

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

        calibration_run_data['last_updated_on'] = run.updated_at

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
        fm = parse_failure_messages(run.failure_messages)
        if fm is not None:
            calibration_run_data['failure_messages'] = fm

        calibration_run_data['forcing_source_requested'] = run.forcing_source_requested.name if run.forcing_source_requested else None
        calibration_run_data['forcing_source_actual'] = run.forcing_source_actual.name if run.forcing_source_actual else None

        # Generate Geopackage map if requested
        if include_gpkg_map:
            gpkg_map_start = time.time()
            geopackage_path = get_single_file(
                get_geopackage_dir_for_job(run)) if run.geopackage_source == GeopackageSourceEnum.UPLOAD.db_instance else run.geopackage_eds_file_path
            if geopackage_path and path_exists(geopackage_path):
                geopackage_png = gpkg_to_png_selected_layers(geopackage_path)
                base64_str = base64.b64encode(geopackage_png.getvalue()).decode('utf-8')
                calibration_run_data['geopackage_image_url'] = f'data:image/png;base64,{base64_str}'
            logger.info(f"Geopackage map generation completed in {time.time() - gpkg_map_start:.2f}s")

        # Determine external data status (whether required files are available)
        data_files_status_start = time.time()
        calibration_run_data['external_data_status'] = get_data_files_status(run)
        logger.info(f"Data Files status completed in {time.time() - data_files_status_start:.2f}s")

        calibration_run_data['parameters_selected'] = has_user_selected_tuning_parameters(module_ids)
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

    # Use cache for module resolution
    modules_by_id = get_cached_modules_by_id()
    module_names = {modules_by_id[mid].name for mid in module_ids if mid in modules_by_id}
    calibration_run_data['modules'] = module_names
    calibration_run_data['is_aet_rootzone'] = run.is_aet_rootzone

    # Validation warnings
    formulation_errors, formulation_warnings, _ = validate_formulation(module_names)
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
    logger.info("Processing tuning data")
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
    return calibration_run_data, time_range


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

    Workflow:
      - Reads calibration run data inside a read-only transaction to reduce contention.
      - Returns both the run data and the computed time range.
      - If the time range was newly computed and not yet persisted on the run,
        it is saved in a short write transaction after the read-only block.

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

    # -------------------------------------------------------------
    # Read-only block: fetch run and load data
    # -------------------------------------------------------------
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        # Do all the heavy lifting in read-only mode
        calibration_run_data, time_range = load_calibration_run_data(
            run,
            export=False,
            include_gpkg_map=include_gpkg_map
        )

    # -------------------------------------------------------------
    # Short write block: persist computed time range if needed
    # -------------------------------------------------------------
    # Persist only if we computed a valid time range
    if time_range and (not run.time_range_start or not run.time_range_end):
        with transaction.atomic():
            persist_time_range(run, time_range)

    # -------------------------------------------------------------
    # Validate and return response
    # -------------------------------------------------------------
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
