import csv
import json
import logging
import os
import time

from data_assimilation_engine.soil_moisture.mapping.mapper import map_soil_moisture_data
from data_assimilation_engine.soil_moisture.timeseries.timeseries import soil_moisture_ts
from data_assimilation_engine.swe.mapping.mapper import map_swe_data
from data_assimilation_engine.swe.timeseries.timeseries import swe_ts
from django.contrib.auth import get_user_model
from django.core.cache import cache
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType
from calibration.models import ValidationRun
from calibration.util.calibration_validators import GetImagesByDateRequestSerializer, GetImagesByDateResponseSerializer, \
    ErrorResponseSerializer, ValidationRunSerializer, GetTimeseriesDataResponseSerializer
from calibration.util.file_util import get_single_file
from calibration.util.ngen_locations import get_geopackage_dir_for_job, get_swe_netcdf_file, get_validation_output_valid, \
    get_swe_timeseries_png_filename, get_swe_timeseries_data_filename, get_soil_moisture_timeseries_png_filename, \
    get_soil_moisture_timeseries_data_filename, get_swe_plot_dir, get_soil_moisture_netcdf_file
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, get_validation_run, png_to_base64_url, get_job_description, \
    validate_response, ResponseError, truncate_large_fields, find_validation_worker_with_matching_id, get_user_email, get_elapsed_str

logger = logging.getLogger(__name__)

User = get_user_model()


def derive_secondary_data_file_inputs(run: ValidationRun) -> dict[str, str]:
    """
    Derives the common file inputs from the validation run for SWE and Soil Moisture

    :param run: The ValidationRun object.
    :return: A dict with keys 'ts_csv_location' for the path to the TS CSV file and 'gpkg' for the geopackage file.
    """
    validation_type = ValidationType(run.validation_type)

    # Find the matching worker name for the validation run if applicable.
    worker_name = find_validation_worker_with_matching_id(
        run,
        worker_name=run.iteration.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
        iteration_num=run.iteration.iteration_num if validation_type == ValidationType.VALID_ITERATION else None
    )

    if worker_name:
        # Retrieve paths to required files.
        swe_csv = get_validation_output_valid(run.calibration_run, worker_name)
        gpkg = get_single_file(get_geopackage_dir_for_job(run.calibration_run))
        return {'ts_csv_location': swe_csv, 'gpkg': gpkg}
    else:
        logger.warning(f'Unable to get secondary data locations for {get_job_description(run)}')
        return {}


def get_or_create_swe_plots(run: ValidationRun, date: str, plot_dir: str) -> dict[str, str]:
    """
    Derives file paths based on the validation run, checks the cache, and if needed, checks for existing files or calls run_swe.main to generate plots.

    :param run: The ValidationRun object.
    :param date: The date string (YYYY-MM-DD) for which to generate plots.
    :param plot_dir: Directory where the plots are stored.
    :return: A dict with keys 'sim_map', 'raw_map', and 'lumped_map' corresponding to their file paths.
    """
    # Get common SWE file inputs.
    inputs = derive_secondary_data_file_inputs(run)
    if not inputs:
        return {}
    swe_csv = inputs['ts_csv_location']
    gpkg = inputs['gpkg']

    # We also need the netCDF.
    swe_netcdf = get_swe_netcdf_file(run.calibration_run)

    # Build paths for the three images.
    sim_map_path = os.path.join(plot_dir, f'sim_swe_map_{date}.png')
    raw_map_path = os.path.join(plot_dir, f'raw_swe_map_{date}.png')
    lumped_map_path = os.path.join(plot_dir, f'swe_lumped_map_{date}.png')

    # Build a cache key based on the run id and date.
    cache_key = f"swe_results_{run.id}_{date}"
    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info(f"Returning cached SWE results for {get_job_description(run)}")
        return cached_result

    # If the files do not exist, run the SWE job.
    if not (os.path.exists(sim_map_path) and os.path.exists(raw_map_path) and os.path.exists(lumped_map_path)):
        swe_args = [
            date,
            swe_csv,
            swe_netcdf,
            gpkg,
            sim_map_path,
            raw_map_path,
            lumped_map_path,
            '--direct_s3'
        ]
        logger.info(f"Calling map_swe_data with arguments: {swe_args}")
        start_time = time.time()
        map_swe_data(swe_args)
        elapsed_time = time.time() - start_time
        logger.info(f"Finished running map_swe_data in {elapsed_time:.2f} seconds")
    else:
        logger.info(f"SWE files already exist in {plot_dir} for {get_job_description(run)}")

    # Build the result once and cache it.
    result = {
        'sim_map': sim_map_path,
        'raw_map': raw_map_path,
        'lumped_map': lumped_map_path,
    }
    cache.set(cache_key, result)
    return result


def get_or_create_soil_moisture_plots(run: ValidationRun, date: str, plot_dir: str) -> dict[str, str]:
    """
    Derives file paths based on the validation run, checks the cache, and if needed, checks for existing files or calls run_swe.main to generate plots.

    :param run: The ValidationRun object.
    :param date: The date string (YYYY-MM-DD) for which to generate plots.
    :param plot_dir: Directory where the plots are stored.
    :return: A dict with keys 'sim_map', 'raw_map', and 'lumped_map' corresponding to their file paths.
    """
    # Get common SWE file inputs.
    inputs = derive_secondary_data_file_inputs(run)
    if not inputs:
        return {}
    soil_moisture_csv = inputs['ts_csv_location']
    gpkg = inputs['gpkg']

    # We also need the netCDF.
    soil_moisture_netcdf = get_soil_moisture_netcdf_file(run.calibration_run)

    # Build paths for the three images.
    sim_map_path = os.path.join(plot_dir, f'sim_soil_moisture_map_{date}.png')
    raw_map_path = os.path.join(plot_dir, f'raw_soil_moisture_map_{date}.png')
    lumped_map_path = os.path.join(plot_dir, f'soil_moisture_lumped_map_{date}.png')

    # Build a cache key based on the run id and date.
    cache_key = f"soil_moisture_results_{run.id}_{date}"
    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info(f"Returning cached Soil Moisture results for {get_job_description(run)}")
        return cached_result

    # If the files do not exist, run the Soil Moisture job.
    if not (os.path.exists(sim_map_path) and os.path.exists(raw_map_path) and os.path.exists(lumped_map_path)):
        soil_moisture_args = [
            date,
            soil_moisture_csv,
            soil_moisture_netcdf,
            gpkg,
            sim_map_path,
            raw_map_path,
            lumped_map_path,
            '--direct_s3'
        ]
        logger.info(f"Calling map_soil_moisture_data with arguments: {soil_moisture_args}")
        start_time = time.time()
        map_soil_moisture_data(soil_moisture_args)
        elapsed_time = time.time() - start_time
        logger.info(f"Finished running map_soil_moisture_data in {elapsed_time:.2f} seconds")
    else:
        logger.info(f"SWE files already exist in {plot_dir} for {get_job_description(run)}")

    # Build the result once and cache it.
    result = {
        'sim_map': sim_map_path,
        'raw_map': raw_map_path,
        'lumped_map': lumped_map_path,
    }
    cache.set(cache_key, result)
    return result


def generate_swe_ts_data(validation_run: ValidationRun) -> None:
    """
    Generates SWE timeseries images and CSV data if the validation run is not of type VALID_CONTROL.

    :param validation_run: The ValidationRun object.
    :return: None
    """
    if validation_run.validation_type != ValidationType.VALID_CONTROL.value:
        # Generate SWE timeseries images.
        inputs = derive_secondary_data_file_inputs(validation_run)
        if inputs:
            ts_csv_location = inputs['ts_csv_location']
            gpkg = inputs['gpkg']

            swe_args = [
                ts_csv_location,
                gpkg,
                '--plot_output',
                get_swe_timeseries_png_filename(validation_run),
                '--csv_output',
                get_swe_timeseries_data_filename(validation_run),
                '--direct_s3'
            ]
            logger.info(f"Calling swe_timeseries.swe_ts with arguments: {swe_args}")
            start_time = time.time()
            swe_ts(swe_args)
            elapsed_time = time.time() - start_time
            logger.info(f"Finished running swe_timeseries.swe_ts in {elapsed_time:.2f} seconds")


def generate_soil_moisture_ts_data(validation_run: ValidationRun) -> None:
    """
    Generates Soil Moisture timeseries images and CSV data if the validation run is not of type VALID_CONTROL.

    :param validation_run: The ValidationRun object.
    :return: None
    """
    if validation_run.validation_type != ValidationType.VALID_CONTROL.value:
        # Generate SWE timeseries images.
        inputs = derive_secondary_data_file_inputs(validation_run)
        if inputs:
            ts_csv_location = inputs['ts_csv_location']
            gpkg = inputs['gpkg']

            swe_args = [
                ts_csv_location,
                gpkg,
                '--plot_output',
                get_soil_moisture_timeseries_png_filename(validation_run),
                '--csv_output',
                get_soil_moisture_timeseries_data_filename(validation_run),
                '--direct_s3'
            ]
            logger.info(f"Calling swe_timeseries.swe_ts with arguments: {swe_args}")
            start_time = time.time()
            soil_moisture_ts(swe_args)
            elapsed_time = time.time() - start_time
            logger.info(f"Finished running swe_timeseries.swe_ts in {elapsed_time:.2f} seconds")


@extend_schema(
    request=GetImagesByDateRequestSerializer,
    responses={
        200: GetImagesByDateResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve SWE images for a given date"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_swe_images_by_date(request: Request) -> Response:
    """
    Retrieve SWE images for a given date.

    :param request: The HTTP request object.
    :return: A Response object with SWE image data or an error message.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetImagesByDateRequestSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')
    date = validator.get('date').strftime("%Y-%m-%d")

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR]
    )
    if error_return:
        return error_return

    validation_start_date = run.calibration_run.validation_start_period.strftime("%Y-%m-%d")
    validation_end_date = run.calibration_run.validation_end_period.strftime("%Y-%m-%d")
    if date < validation_start_date or date > validation_end_date:
        return ResponseError(f'Date specified {date} must be within the Validation Simulation range {validation_start_date} to {validation_end_date}')

    # Do not allow snodas plots for Validation Control runs.
    if run.validation_type == ValidationType.VALID_CONTROL.value:
        return ResponseError('Snodas plots are not available for a Validation Control run')

    plot_dir = os.path.join(get_swe_plot_dir(run))
    os.makedirs(plot_dir, exist_ok=True)

    # Retrieve or generate the SWE plots using the helper.
    swe_results = get_or_create_swe_plots(run, date, plot_dir)
    if not swe_results:
        return ResponseError(f'Unable to retrieve SWE data for {get_job_description(run)}')

    response = {
        'message': f'Plots created in {plot_dir}',
        'lumped_map': png_to_base64_url(swe_results['lumped_map']),
        'raw_map': png_to_base64_url(swe_results['raw_map']),
        'sim_map': png_to_base64_url(swe_results['sim_map'])
    }

    response_validator, error_response = validate_response(
        GetImagesByDateResponseSerializer,
        response,
        fields_to_truncate=['lumped_map', 'raw_map', 'sim_map']
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["lumped_map", "raw_map", "sim_map"]))}'
    )

    return Response(response)



@extend_schema(
    request=GetImagesByDateRequestSerializer,
    responses={
        200: GetImagesByDateResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve Soil Moisture images for a given date"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_soil_moisture_images_by_date(request: Request) -> Response:
    """
    Retrieve SWE images for a given date.

    :param request: The HTTP request object.
    :return: A Response object with SWE image data or an error message.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetImagesByDateRequestSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')
    date = validator.get('date').strftime("%Y-%m-%d")

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR]
    )
    if error_return:
        return error_return

    validation_start_date = run.calibration_run.validation_start_period.strftime("%Y-%m-%d")
    validation_end_date = run.calibration_run.validation_end_period.strftime("%Y-%m-%d")
    if date < validation_start_date or date > validation_end_date:
        return ResponseError(f'Date specified {date} must be within the Validation Simulation range {validation_start_date} to {validation_end_date}')

    # Do not allow soil moisture plots for Validation Control runs.
    if run.validation_type == ValidationType.VALID_CONTROL.value:
        return ResponseError('Soil Moisture plots are not available for a Validation Control run')

    plot_dir = os.path.join(get_swe_plot_dir(run))
    os.makedirs(plot_dir, exist_ok=True)

    # Retrieve or generate the Soil Moisture plots using the helper.
    soil_moisture_results = get_or_create_soil_moisture_plots(run, date, plot_dir)
    if not soil_moisture_results:
        return ResponseError(f'Unable to retrieve Soil Moisture data for {get_job_description(run)}')

    response = {
        'message': f'Plots created in {plot_dir}',
        'lumped_map': png_to_base64_url(soil_moisture_results['lumped_map']),
        'raw_map': png_to_base64_url(soil_moisture_results['raw_map']),
        'sim_map': png_to_base64_url(soil_moisture_results['sim_map'])
    }

    response_validator, error_response = validate_response(
        GetImagesByDateResponseSerializer,
        response,
        fields_to_truncate=['lumped_map', 'raw_map', 'sim_map']
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["lumped_map", "raw_map", "sim_map"]))}'
    )

    return Response(response)


@extend_schema(
    request=ValidationRunSerializer,
    responses={
        200: GetTimeseriesDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve SWE timeseries data for a given validation run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_swe_timeseries_data(request: Request) -> Response:
    """
    Retrieve SWE timeseries data for a given validation run.

    :param request: The HTTP request object.
    :return: A Response object with SWE timeseries image and data or an error message.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ValidationRunSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR]
    )
    if error_return:
        return error_return

    # Read the CSV file and convert it to JSON (list of dicts)
    csv_filepath = get_swe_timeseries_data_filename(run)
    try:
        swe_timeseries_data = read_csv_as_json(csv_filepath)
    except Exception as e:
        logger.error(f"Error reading SWE timeseries CSV: {e}")
        return ResponseError(f"Failed to read SWE timeseries data file - {e}")

    response = {
        'message': f'Retrieved SWE timeseries data for Validation Run {run.id}',
        'timeseries_image': png_to_base64_url(get_swe_timeseries_png_filename(run)),
        'timeseries_data': swe_timeseries_data,
    }

    response_validator, error_response = validate_response(
        GetTimeseriesDataResponseSerializer,
        response,
        fields_to_truncate=['timeseries_image', 'timeseries_data'], max_length=50
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["timeseries_image", "timeseries_data"], max_length=50))}'
    )

    return Response(response)


@extend_schema(
    request=ValidationRunSerializer,
    responses={
        200: GetTimeseriesDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve Soil Moisture timeseries data for a given validation run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_soil_moisture_timeseries_data(request: Request) -> Response:
    """
    Retrieve SWE timeseries data for a given validation run.

    :param request: The HTTP request object.
    :return: A Response object with SWE timeseries image and data or an error message.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ValidationRunSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR]
    )
    if error_return:
        return error_return

    # Read the CSV file and convert it to JSON (list of dicts)
    csv_filepath = get_soil_moisture_timeseries_data_filename(run)
    try:
        soil_moisture_timeseries_data = read_csv_as_json(csv_filepath)
    except Exception as e:
        logger.error(f"Error reading Soil Moisture timeseries CSV: {e}")
        return ResponseError(f"Failed to read Soil Moisture timeseries data file - {e}")

    response = {
        'message': f'Retrieved Soil Moisture timeseries data for Validation Run {run.id}',
        'timeseries_image': png_to_base64_url(get_soil_moisture_timeseries_png_filename(run)),
        'timeseries_data': soil_moisture_timeseries_data,
    }

    response_validator, error_response = validate_response(
        GetTimeseriesDataResponseSerializer,
        response,
        fields_to_truncate=['timeseries_image', 'timeseries_data'], max_length=50
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["timeseries_image", "timeseries_data"], max_length=50))}'
    )

    return Response(response)


def read_csv_as_json(csv_filepath: str) -> list[dict[str, str]]:
    """
    Reads a CSV file and returns a list of dictionaries using column names as keys.

    :param csv_filepath: Path to the CSV file.
    :return: A list of dictionaries representing each row in the CSV.
    """
    with open(csv_filepath, newline='') as csvfile:
        return list(csv.DictReader(csvfile))
