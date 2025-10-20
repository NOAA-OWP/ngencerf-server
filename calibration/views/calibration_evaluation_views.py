import io
import json
import logging
import os
import threading
import time
import zipfile
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, QuerySet
from django.http import HttpResponse, StreamingHttpResponse, FileResponse, JsonResponse
from django.views.decorators.http import require_GET
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationMetricPeriod, ValidationType, LogCategory, LogName
from calibration.models import Iteration, NWMRetrospectiveMetrics, CalibrationRun, ValidationRun
from calibration.util.calibration_validators import CalibrationRunSerializer, CalibrationOrValidationRunSerializer, \
    ErrorResponseSerializer, GetCalibrationDataByIterationResponseSerializer, GetLogsResponseSerializer, \
    GetLogNamesResponseSerializer, GetLogRequestSerializer, GetLogStatusRequestSerializer, \
    GetLogStatusResponseSerializer, GenericMessageWithIdResponseSerializer
from calibration.util.ngen_locations import get_calibration_stdout_file, get_validation_best_stdout_file, get_validation_control_stdout_file, \
    get_validation_iteration_stdout_file, get_ngen_stdout_log_filename, get_ngen_log_path
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, handle_exceptions, validate_response, validate_request, truncate_large_fields, \
    get_validation_run, CerfException, replace_nan_and_inf_with_none, process_worker_dirs, get_user_email, ResponseError, get_elapsed_str

logger = logging.getLogger(__name__)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetCalibrationDataByIterationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve metrics and parameters by iteration for a specific calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_calibration_data_by_iteration(request: Request) -> Response:
    """
    Retrieves calibration data by iteration for a specific calibration run.

    - Handles user authentication and validation.
    - Fetches retrospective metrics and iteration data.
    - Constructs a response containing iterations, parameters, metrics, and validation information.

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response with calibration data or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    # Fetch retrospective metrics data associated with the calibration run
    nwm_retrospective_data = list(
        NWMRetrospectiveMetrics.objects
        .filter(period=ValidationMetricPeriod.valid.value, calibration_run=run)
        .select_related('metric')
        .only('metric__name', 'metric_value')
        .annotate(
            metric_name=F('metric__name'),
            metric_display_name=F('metric__display_name'),
        )
        .values('metric_name', 'metric_display_name', 'metric_value')
    )

    retrospective_data = [{'name': 'NWM 3.0', 'data': nwm_retrospective_data}]

    iterations = get_iterations_for_calibration_job(run)

    # Prefetch validation runs for all iterations
    validation_runs = ValidationRun.objects.filter(
        iteration__in=iterations,
        status__in=[
            StatusEnum.DONE.db_instance,
            StatusEnum.RUNNING.db_instance,
            StatusEnum.SUBMITTED.db_instance,
        ],
    ).select_related('calibration_run')

    validation_runs_by_iteration = {vr.iteration_id: vr for vr in validation_runs}

    # Construct iteration data with parameters, metrics, and validation reference
    iteration_data = []
    for iteration in iterations:
        validation_run = validation_runs_by_iteration.get(iteration.id)

        iteration_element = {
            'iteration_num': iteration.iteration_num,
            'iteration_id': iteration.id,
            'worker_name': iteration.worker_name,
            'best_params': iteration.best_params,
            'objective_function_value': iteration.objective_function_value,
            'parameters': [
                {'parameter_name': param.calibration_parameter.name, 'parameter_value': param.tuned_value}
                for param in iteration.iterationparameter_set.all()
            ],
            'metrics': [
                {
                    'metric_name': metric.metric.name,
                    'metric_display_name': metric.metric.display_name,
                    'metric_value': metric.metric_value
                }
                for metric in iteration.iterationmetric_set.all()
            ]
        }
        if validation_run:
            iteration_element['validation_run_id'] = validation_run.id
        iteration_data.append(iteration_element)

    response = {
        'message': f'Calibration Job {run.id}, data retrieved',
        # Will be none for LSTM
        'objective_function_metric': run.objective_function.name if run.objective_function else None,
        'iteration_data': iteration_data,
        'retrospective_data': retrospective_data
    }

    # Replace NaN values with None for JSON compatibility
    response = replace_nan_and_inf_with_none(response)

    response_validator, error_response = validate_response(
        GetCalibrationDataByIterationResponseSerializer,
        response,
        fields_to_truncate=['iteration_data'],
        max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["iteration_data"], max_length=10))}'
    )

    return Response(response_validator.data)


def get_iterations_for_calibration_job(calibration_run: CalibrationRun, worker_name: str | None = None) -> QuerySet[Iteration]:
    """
        Fetches iterations for the given calibration run.

        - Optionally filters by worker name.
        - Prefetches related parameters and metrics for optimized retrieval.

        :param calibration_run: The CalibrationRun instance to fetch iterations for.
        :param worker_name: Optional worker name to filter iterations.
        :return: QuerySet of Iteration objects associated with the calibration run.
        """
    queryset = Iteration.objects.filter(calibration_run=calibration_run)

    if worker_name:
        queryset = queryset.filter(worker_name=worker_name)

    return (
        queryset
        .select_related('calibration_run')
        .prefetch_related('iterationparameter_set__calibration_parameter',
                          'iterationmetric_set')
        .order_by('worker_name', 'iteration_num')
    )


@extend_schema(
    request=CalibrationOrValidationRunSerializer,
    responses={
        200: GetLogNamesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve available log names for a given validation run"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_log_names(request: Request) -> Response:
    """
    Retrieves a list of available log names for a specific calibration or validation run.

    - Handles request validation and user permissions.
    - Returns logs categorized by their association (calibration, validation, global, or forecast).

    :param request: The HTTP request object containing validation run ID.
    :return: JSON response with log names or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationOrValidationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')

    if validation_run_id:
        validation_run, error_return = get_validation_run(
            validation_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return
        # TODO calibration_run variable not used right now, but we might need later for forecast
        # calibration_run = validation_run.calibration_run

        # Define available log categories and names
        log_names = [
            {LogCategory.CALIBRATION.value: ['ngen stdout', 'ngen-cal stdout']},
            {LogCategory.VALIDATION.value: ['ngen-cal stdout']},
            {LogCategory.GLOBAL.value: ['ngen']},
        ]
    else:
        calibration_run, error_return = get_calibration_run(
            calibration_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return

        # Define available log categories and names
        log_names = [
            {LogCategory.CALIBRATION.value: ['ngen stdout', 'ngen-cal stdout']},
            {LogCategory.GLOBAL.value: ['ngen']},
        ]

    # Include forecast logs if applicable
    # Commenting out for now since we have nowhere for the UI to display these
    # if ForecastRun.objects.filter(calibration_run=calibration_run).exists():
    #     log_names.append({LogCategory.FORECAST.value: ['ngen stdout', 'forecast stdout']})

    response = {'log_names': log_names}

    response_validator, error_response = validate_response(GetLogNamesResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


VALID_LOG_NAMES = {
    LogCategory.CALIBRATION: [LogName.NGEN_CAL_STDOUT, LogName.NGEN_STDOUT],
    LogCategory.VALIDATION: [LogName.NGEN_CAL_STDOUT, LogName.NGEN_STDOUT],
    LogCategory.FORECAST: [LogName.FORECAST_STDOUT, LogName.NGEN_STDOUT],
    LogCategory.GLOBAL: [LogName.NGEN]
}


def validate_log_name(log_category: LogCategory, log_name: LogName):
    """
    Validates whether the provided log name is valid for the given log category.

    - Ensures the log name exists in the predefined valid logs for the category.

    :param log_category: The category of the log (enum representation of LogCategory).
    :param log_name: The log name to validate.
    :raises ValueError: If the log name is not valid for the given category.
    """
    valid_logs = VALID_LOG_NAMES.get(log_category)

    valid_log_values = [log.value for log in valid_logs]

    if log_name not in valid_logs:
        raise ValueError(f"Invalid log name '{log_name.value}' for category '{log_category.value}'. Valid options are: {valid_log_values}.")


@extend_schema(
    request=GetLogRequestSerializer,
    responses={
        200: GetLogsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve a specific log file with pagination support"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_log(request: Request) -> Response:
    """
    Retrieves a specific log file for a calibration run (or validation run and its associated calibration run).

    - Supports pagination for large log files.
    - Validates log category and log name.

    :param request: The HTTP request object containing calibration/validation run and log information.
    :return: JSON response with log file content or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetLogRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    log_category = LogCategory(validator.get('log_category'))
    log_name = LogName(validator.get('log_name'))
    start = validator.get('start')
    limit = validator.get('limit')

    # Validate log category and log name
    try:
        validate_log_name(log_category, log_name)
    except ValueError as e:
        raise CerfException(str(e))

    if validation_run_id:
        validation_run, error_return = get_validation_run(
            validation_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return
        calibration_run = validation_run.calibration_run
    else:
        calibration_run, error_return = get_calibration_run(
            calibration_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return
        validation_run = None

    log_path = None
    match log_category:
        case LogCategory.CALIBRATION:
            log_path = get_calibration_log(calibration_run, log_name)
        case LogCategory.VALIDATION:
            if validation_run:
                log_path = get_validation_log(validation_run, log_name)
            else:
                raise CerfException(f"Log category '{log_category.value}' not applicable for calibration run")
        case LogCategory.GLOBAL:
            if validation_run:
                log_path = get_global_log(validation_run, log_name)
            else:
                log_path = get_global_log(calibration_run, log_name)
        case _:
            raise CerfException(f"Unknown log category '{log_category.value}'")

    # Check if the log file exists
    if log_path and not os.path.exists(log_path):
        raise CerfException(f"Log file not found: {log_path}")

    # Get the file size in bytes
    file_size = os.path.getsize(log_path)

    # Count the total number of lines in the file for pagination metadata
    with open(log_path, 'r') as f:
        total_lines = sum(1 for _ in f)

    # Read the requested lines from the log file with null replacement
    with open(log_path, 'r') as file:
        all_lines = [line.replace('\x00', ' ') for line in file]

    if start == -1:
        # Just get the last 'limit' lines
        paginated_lines = all_lines[-limit:]
    else:
        paginated_lines = all_lines[start:start + limit]

    pagination_metadata = {
        'start': start,
        'limit': limit,
        'count': total_lines
    }

    response = {
        'message': f"{log_category.value.capitalize()} {log_name.value} log file retrieved",
        'log_data': paginated_lines,
        'log_path': log_path,
        'byte_offset': file_size,
        'pagination_metadata': pagination_metadata,
        'status': validation_run.status.name if validation_run else calibration_run.status.name
    }

    response_validator, error_response = validate_response(GetLogsResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=GetLogStatusRequestSerializer,
    responses={
        200: GetLogStatusResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve a specific log file with pagination support"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_log_status(request: Request) -> Response:
    """
    Checks the status a specific log file to see if it has been updated since it was last requested.

    - Uses byte_offset to compare the size of the last data set retrieved to what is currently in the file/cache.

    :param request: The HTTP request object containing calibration/validation run and log information.
    :return: JSON response with log file content or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetLogStatusRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    log_path = validator.get('log_path')
    byte_offset = validator.get('byte_offset')

    if validation_run_id:
        validation_run, error_return = get_validation_run(
            validation_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return
        calibration_run = validation_run.calibration_run
    else:
        calibration_run, error_return = get_calibration_run(
            calibration_run_id,
            request.user,
            run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED, StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return
        validation_run = None

    # Check if the log file exists
    # TO DO: Get this from the cache if it's already been cached
    if not os.path.exists(log_path):
        raise CerfException(f"Log file not found: {log_path}")

    # Get the file size in bytes
    file_size = os.path.getsize(log_path)

    response = {
        'message': f"log file {log_path} has " + ("changed" if file_size != byte_offset else "not changed"),
        'file_updated': True if file_size != byte_offset else False,
        'status': validation_run.status.name if validation_run else calibration_run.status.name
    }

    response_validator, error_response = validate_response(GetLogStatusResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def get_calibration_log(calibration_run: CalibrationRun, log_name: LogName) -> str:
    """
    Retrieves the appropriate log file for a given calibration run.

    - Determines the log file based on the specified log name.
    - Supports logs like `ngen.stdout` and `ngen-cal.stdout`.

    :param calibration_run: The CalibrationRun object for which the log is retrieved.
    :param log_name: The LogName enum specifying the log type.
    :return: The path to the log file.
    """
    if log_name == LogName.NGEN_STDOUT:
        return find_ngen_stdout_log(calibration_run)
    elif log_name == LogName.NGEN_CAL_STDOUT:
        return get_calibration_stdout_file(calibration_run)

    raise CerfException(f'Invalid log name: {log_name}')


def get_validation_log(validation_run: ValidationRun, log_name: LogName) -> str:
    """
    Fetches the appropriate log file for a specific validation run.

    - Handles various validation types (best, control, iteration).
    - Supports logs like `ngen.stdout` and `ngen-cal.stdout`.

    :param validation_run: The ValidationRun object for which the log is retrieved.
    :param log_name: The LogName enum specifying the log type.
    :return: The path to the log file.
    """
    validation_type = validation_run.validation_type

    if validation_type in {ValidationType.VALID_BEST.value, ValidationType.VALID_CONTROL.value}:
        if log_name == LogName.NGEN_CAL_STDOUT:
            return (
                get_validation_best_stdout_file(validation_run.calibration_run)
                if validation_type == ValidationType.VALID_BEST.value
                else get_validation_control_stdout_file(validation_run.calibration_run)
            )

    elif validation_type == ValidationType.VALID_ITERATION.value:
        if log_name == LogName.NGEN_CAL_STDOUT:
            return get_validation_iteration_stdout_file(
                validation_run.calibration_run,
                validation_run.worker_name,
                validation_run.iteration_num
            )

    if log_name == LogName.NGEN_STDOUT:
        return find_ngen_stdout_log(validation_run)

    raise CerfException(f'Invalid log_name: {log_name}')


def get_global_log(run: CalibrationRun | ValidationRun, log_name: LogName) -> str:
    """
    Retrieves the global log file, if applicable.

    - Only supports `ngen` logs currently.

    :param run: The CalibrationRun or ValidationRun object.
    :param log_name: The LogName enum specifying the log type.
    :return: The path to the global log file.
    """
    if log_name == LogName.NGEN:
        return get_ngen_log_path(run if isinstance(run, CalibrationRun) else run.calibration_run)

    raise CerfException(f'Invalid log name: {log_name}')


def find_ngen_stdout_log(run: CalibrationRun | ValidationRun) -> str | None:
    """
    Searches for the `ngen.stdout` log file in worker directories of a given run.

    - Iterates over worker directories using `process_worker_dirs`.
    - Returns the path to the log file if found.

    :param run: The CalibrationRun or ValidationRun object.
    :return: The path of the `ngen.stdout` log file, or None if not found.
    """
    ngen_log_path = None

    # Custom function to check worker directories for the ngen log file
    def check_worker(worker_dir: str, _run: CalibrationRun | ValidationRun):
        nonlocal ngen_log_path
        potential_log_path = os.path.join(worker_dir, get_ngen_stdout_log_filename())

        # Check if ngen stdout file exists in the current worker directory
        if potential_log_path and os.path.isfile(potential_log_path):
            ngen_log_path = potential_log_path

    # Call process_worker_dirs to iterate through the worker directories
    process_worker_dirs(run, check_worker)

    if not ngen_log_path:
        raise CerfException('Could not find ngen log in worker directory')

    return ngen_log_path


def get_zip_cache_key(calibration_run_id: int) -> str:
    """
    Returns the standardized cache key used to track zip job status.
    This ensures consistent key usage across all endpoints.
    """
    return f'zip_status_{calibration_run_id}'


downloadable_statuses = [s for s in StatusEnum if s not in {StatusEnum.READY, StatusEnum.SAVED}]


@api_view(['GET', 'POST'])
@handle_exceptions
def get_calibration_job_zip(request: Request) -> HttpResponse:
    """
    Zips up all files in the user's working directory for the given calibration_job_id and returns the 
    resulting file as a response to the browser.

    :param request: The HTTP request object containing calibration run data.
    :return: ZIP response containing all files in the user's working directory for the given calibration_job_id

    This is a synchronous endpoint that is not currently used by the UI, but is used by the CLI
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return

    bytes_io = io.BytesIO()
    job_data_dir = calibration_run.job_data_dir

    with zipfile.ZipFile(bytes_io, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for root, _, files in os.walk(job_data_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arc_name = os.path.relpath(file_path, job_data_dir)
                try:
                    zip_file.write(file_path, arc_name)
                except FileNotFoundError:
                    logger.error(f"Unable to read file: {arc_name} while building zip file")

    response = HttpResponse(bytes_io.getvalue(), content_type='application/zip')
    zip_name = f"{os.path.basename(job_data_dir)}_{calibration_run.user_formulation_name}"
    response['Content-Disposition'] = f'attachment; filename="{zip_name}.zip"'

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
    return response


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GenericMessageWithIdResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Starts a background process to zip calibration job files. Use `get_zip_status` to track progress."
)
@api_view(['GET', 'POST'])
@handle_exceptions
def start_zip_for_calibration_job(request: Request) -> Response:
    """
    Starts the process to zip calibration job files in a background thread.
    Returns immediately with a job ID (calibration_run_id).
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    cache_key = get_zip_cache_key(calibration_run_id)

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return

    zip_status = cache.get(cache_key)
    if zip_status and zip_status.get('status') == 'pending':
        logger.info(f"Zip job already in progress for Calibration Job {calibration_run_id}")
        return Response({
            "message": "Zip job already in progress",
            "status": zip_status["status"],
            "calibration_run_id": calibration_run_id
        })

    # Mark status as pending (shared across workers)
    cache.set(cache_key, {
        "status": "pending",
        "path": None,
        "started_at": datetime.now().isoformat()
    }, timeout=None)

    # Launch zip process in background
    def zip_job():
        start_time = datetime.now()
        try:
            job_data_dir = run.job_data_dir
            zip_name = f"{os.path.basename(job_data_dir)}_{run.user_formulation_name}"
            zip_path = os.path.join(settings.CACHE_DIRECTORY, f'{zip_name}.zip')

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, _, files in os.walk(job_data_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.relpath(file_path, job_data_dir)
                        try:
                            zip_file.write(file_path, arc_name)
                        except FileNotFoundError:
                            logger.warning(f"File not found during zipping: {arc_name}")

            # Mark the zip job as complete
            cache.set(cache_key, {
                'status': 'done',
                'path': zip_path,
                'started_at': cache.get(cache_key).get('started_at')
            }, timeout=None)

            duration = datetime.now() - start_time
            logger.info(f"Zip job completed for Calibration Job {run.id} in {duration.total_seconds():.2f} seconds")

        except Exception as e:
            cache.set(cache_key, {
                'status': 'error',
                'path': None,
                'started_at': cache.get(cache_key).get('started_at')
            }, timeout=None)
            duration = datetime.now() - start_time
            logger.exception(f"Failed to zip Calibration Job {run.id} after {duration.total_seconds():.2f} seconds: {e}")

    threading.Thread(target=zip_job, daemon=True).start()

    response = ({"message": "Zip job started", "calibration_run_id": calibration_run_id})

    response_validator, error_response = validate_response(GenericMessageWithIdResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: OpenApiResponse(description="Server-Sent Events stream with zip job status updates"),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Streams zip status updates in real time via Server-Sent Events (SSE)"
)
# NOTE: We use require_GET instead of @api_view because:
# - @api_view is part of Django REST Framework (DRF), which handles content negotiation.
# - For Server-Sent Events (SSE), DRF expects the client to accept "application/json", which causes issues.
# - If the client sends "text/event-stream", DRF may reject it with a 406 Not Acceptable error.
# - require_GET is a plain Django view decorator that avoids DRF’s content negotiation and lets us stream raw SSE.
# - Because this bypasses DRF, we return a JsonResponse directly for errors instead of DRF’s Response.
@require_GET
@handle_exceptions
def get_zip_status(request: Request, calibration_run_id: int) -> StreamingHttpResponse | JsonResponse:
    """
    SSE (Server-Sent Events) endpoint that streams the status of a background zip job.

    - Streams status updates (e.g., "pending", "done", "error") to the client.
    - Closes the connection once the job is complete or encounters an error.
    - Returns a JSON error response if no zip job has been started.
    - Uses require_GET instead of @api_view to support SSE without 406 errors due to DRF content negotiation.

    :param request: HTTP request object.
    :param calibration_run_id: The ID of the calibration job being zipped.
    :return: StreamingHttpResponse with real-time status updates, or JsonResponse if the job is not found.
    """
    cache_key = get_zip_cache_key(calibration_run_id)
    zip_status = cache.get(cache_key)
    if not zip_status:
        logger.info(f"get_zip_status called for Calibration Job {calibration_run_id} but no zip job found")
        return JsonResponse(
            {
                "response_type": "error",
                "message": f"No zip job found for Calibration Job {calibration_run_id}"
            },
            status=404
        )

    def event_stream():
        try:
            start_time = datetime.now()
            logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} for Calibration Run id {calibration_run_id}')

            # Stream loop: keep checking the job status until it is "done" or "error"
            while True:
                # Retrieve the current zip status from in-memory map
                current_zip_status = cache.get(cache_key, {"status": "not_found"})

                # Format the status as an SSE-compatible message
                yield f"data: {json.dumps(current_zip_status)}\n\n"

                # If job has finished or failed, stop the stream (connection closes)
                if current_zip_status["status"] in ["done", "error"]:
                    duration = datetime.now() - start_time
                    logger.debug(
                        f'{get_caller_name()}() streaming complete for {get_user_email(request)} - '
                        f'calibration_run_id={calibration_run_id} - status={current_zip_status["status"]} - '
                        f'duration={duration.total_seconds():.2f}s'
                    )
                    break

                # Sleep before checking again (keeps CPU usage low and reduces frequency)
                time.sleep(1)
        except GeneratorExit:
            # Happens if the client closes the connection
            logger.info(f"Client disconnected during SSE stream for run {calibration_run_id}")
        except Exception as e:
            logger.exception(f"Unhandled exception in event_stream for {calibration_run_id}: {e}")

    # Return a streaming HTTP response using the generator function above
    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")

    # Add CORS header if Origin is allowed
    origin = request.headers.get("Origin")
    if origin in settings.CORS_ALLOWED_ORIGINS:
        response["Access-Control-Allow-Origin"] = origin

    return response


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: OpenApiResponse(description="ZIP file ready for download"),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Returns the zipped calibration job if ready. Automatically deletes the file after sending."
)
@api_view(['GET', 'POST'])
@handle_exceptions
def download_calibration_zip(request: Request) -> FileResponse | Response:
    """
    Serves the zipped calibration job data for download after it has been prepared.

    - Extracts calibration_run_id from POST or GET parameters.
    - Validates that the zip process has completed.
    - Returns the ZIP file as an attachment if available.
    - Returns an error response if the file is not ready or missing.

    :param request: The HTTP request object.
    :return: HTTP response with the ZIP file or a formatted error response.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_response = validate_request(CalibrationRunSerializer, data)
    if error_response:
        return error_response

    calibration_run_id = validator.get("calibration_run_id")
    cache_key = get_zip_cache_key(calibration_run_id)
    zip_status = cache.get(cache_key)

    if not zip_status:
        return ResponseError(f"Zip job not found for Calibration Job {calibration_run_id}", http_status=status.HTTP_404_NOT_FOUND)

    if zip_status["status"] != "done":
        return ResponseError(f"Zip file for Calibration Job {calibration_run_id} is not ready yet")

    zip_path = zip_status.get("path")
    if not zip_path or not os.path.exists(zip_path):
        return ResponseError(f"Zip file is missing for Calibration Job {calibration_run_id}")

    try:
        response = FileResponse(open(zip_path, 'rb'), content_type='application/zip')
        filename = os.path.basename(zip_path)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        def cleanup():
            try:
                os.remove(zip_path)
                logger.info(f"Deleted zip file after download: {zip_path}")
            except Exception as ex:
                logger.warning(f"Failed to delete zip file {zip_path}: {ex}")
            cache.delete(cache_key)

        # ------------------------------------------------------------------
        # Wrap the original response.close() method so cleanup() runs first.
        # This ensures the file and cache entry are removed immediately
        # after the response is finished sending to the client.
        # ------------------------------------------------------------------
        original_close = response.close

        def wrapped_close():
            cleanup()
            return original_close()

        response.close = wrapped_close
        # ------------------------------------------------------------------

        logger.debug(
            f'Returning zip for Calibration Job {calibration_run_id} to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
        return response

    except IOError as e:
        logger.exception(f"Failed to read zip file for Calibration Job {calibration_run_id}: {e}")
        return ResponseError(f"Failed to read zip file for Calibration Job {calibration_run_id}")
