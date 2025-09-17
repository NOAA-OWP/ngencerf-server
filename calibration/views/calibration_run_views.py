import json
import logging
import os
from datetime import datetime, timezone

from django.conf import settings
from django.db import transaction
from django.forms import model_to_dict
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.enums_vanilla import JobType
from calibration.models import Iteration, ValidationRun, ForecastRun, ForecastForcingDownloadRun, CalibrationRun, Status
from calibration.run_util.run_common import cancel_job_common, submit_job
from calibration.run_util.run_ngen_cal_pw import SlurmStatusEnum, run_calibration_job_callback_pw, run_validation_job_callback_pw, \
    run_forecast_job_callback_pw, run_forecast_forcing_download_job_callback_pw
from calibration.util.calibration_validators import CalibrationRunSerializer, GenericResponseSerializer, \
    ErrorResponseSerializer, ReportIterationSerializer, SubmitCalibrationJobResponseSerializer, GetIterationsResponseSerializer, \
    CalibrationJobSlurmCallbackRequestSerializer, ValidationJobSlurmCallbackRequestSerializer, EmptySerializer, \
    GetStatusRequestSerializer, GetStatusResponseSerializer, \
    GetStatusForComparisonRequestSerializer, GetStatusForComparisonResponseSerializer, \
    CalibrationOrValidationOrForecastRunSerializer, ForecastJobSlurmCallbackRequestSerializer, \
    ForecastForcingDownloadJobSlurmCallbackRequestSerializer, CancelJobResponseSerializer, ValidationRunSerializer, \
    GenericResponseSerializerWithValidator, RunCalibrationJob, MPINodesRulesSerializer, MPINodesRulesResponseSerializer
from calibration.views import ngen_cal_input
from calibration.views.calibration_swe_views import generate_swe_ts_data
from calibration.views.called_from import get_caller_name
from calibration.views.common import ResponseError, get_calibration_run, handle_exceptions, validate_response, validate_request, \
    generate_custom_token, TOKEN_SLURM_SCOPE, get_validation_run, get_forecast_run, get_forecast_forcing_download_run, join_with_or, get_user_email, \
    get_job_description, get_elapsed_str, readonly_transaction, truncate_large_fields, auth_scope_required
from calibration.views.end_of_job_processing import read_calibration_output

logger = logging.getLogger(__name__)


@extend_schema(
    request=GetStatusRequestSerializer,
    responses={
        200: GetStatusResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the status of a calibration job and associated validation and forecast jobs"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_status(request: Request) -> Response:
    """
    Retrieves the status of a calibration job, including associated validation and forecast jobs.
    Optionally includes performance metrics based on the request parameters.
    Runs in READ ONLY mode to avoid locking contention.

    :param request: HTTP request containing calibration run details.
    :return: JSON response with the status and associated job details.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetStatusRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    include_performance_metrics = validator.get('include_performance_metrics')

    # All DB access below is read-only
    with readonly_transaction():
        calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        # Conditionally retrieve calibration performance metrics
        calibration_metrics = (
            get_performance_metrics(calibration_run.performance_metrics)
            if should_include_metrics(calibration_run.status, include_performance_metrics)
            else None
        )

        # Retrieve validation runs with related PerformanceMetrics data
        validation_runs = ValidationRun.objects.filter(calibration_run=calibration_run).select_related(
            "performance_metrics"
        ).only(
            "id", "status__name", "validation_type", "submit_date",
            "performance_metrics__elapsed_time", "performance_metrics__num_cpus",
            "performance_metrics__cpu_time", "performance_metrics__max_rss",
            "performance_metrics__max_disk_read", "performance_metrics__max_disk_write",
            "performance_metrics__reserved_time"

        )

        # Retrieve forecast runs with related PerformanceMetrics data
        forecast_runs = ForecastRun.objects.filter(calibration_run=calibration_run).select_related(
            "performance_metrics", "forcing_download_run"
        ).only(
            "id", "status__name", "submit_date",
            "performance_metrics__elapsed_time", "performance_metrics__num_cpus",
            "performance_metrics__cpu_time", "performance_metrics__max_rss",
            "performance_metrics__max_disk_read", "performance_metrics__max_disk_write",
            "performance_metrics__reserved_time",
            "forcing_download_run__status__name",
            "forcing_download_run__performance_metrics__elapsed_time",
            "forcing_download_run__performance_metrics__num_cpus",
            "forcing_download_run__performance_metrics__cpu_time",
            "forcing_download_run__performance_metrics__max_rss",
            "forcing_download_run__performance_metrics__max_disk_read",
            "forcing_download_run__performance_metrics__max_disk_write",
            "forcing_download_run__performance_metrics__reserved_time"
        )

    # Construct validation response with performance metrics as needed
    validation_response = []
    for run in validation_runs:
        validation_data = {
            'validation_run_id': run.id,
            'status': run.status.name,
            'validation_type': run.validation_type,
            'iteration_num': run.iteration_num,
            'submit_date': run.submit_date,
            'run_start': run.run_start,
            'run_end': run.run_end
        }

        if run.performance_metrics:
            validation_data['elapsed_time'] = run.performance_metrics.elapsed_time
        else:
            validation_data['elapsed_time'] = run.run_end - run.run_start if run.run_end and run.run_start else None

        if should_include_metrics(run.status, include_performance_metrics):
            validation_data['performance_metrics'] = get_performance_metrics(run.performance_metrics)

        validation_response.append(validation_data)

    # Construct validation response with performance metrics as needed
    forecast_response = []
    for run in forecast_runs:
        forcing_download = run.forcing_download_run
        forecast_data = {
            'forecast_run_id': run.id,
            'status': run.status.name,
            'cycle': run.cycle.name,
            'submit_date': run.submit_date,
            'run_start': run.run_start,
            'run_end': run.run_end
        }

        if run.performance_metrics:
            forecast_data['elapsed_time'] = run.performance_metrics.elapsed_time
        else:
            forecast_data['elapsed_time'] = run.run_end - run.run_start if run.run_end and run.run_start else None

        if should_include_metrics(run.status, include_performance_metrics):
            forecast_data['performance_metrics'] = get_performance_metrics(run.performance_metrics)

        if forcing_download:
            forcing_download_data = {
                'forcing_download_run_id': forcing_download.id,
                'status': forcing_download.status.name,
                'elapsed_time': forcing_download.performance_metrics.elapsed_time if forcing_download.performance_metrics else None
            }
            if should_include_metrics(forcing_download.status, include_performance_metrics):
                forcing_download_data['performance_metrics'] = get_performance_metrics(forcing_download.performance_metrics)
            forecast_data['forcing_download'] = forcing_download_data

        forecast_response.append(forecast_data)

    # Prepare the main response without calibration performance metrics if not requested
    response = {
        'message': f'Calibration Job {calibration_run.id}, status is {calibration_run.status.name}',
        'calibration_run_id': calibration_run.id,
        'status': calibration_run.status.name,
        'submit_date': calibration_run.submit_date,
        'run_start': calibration_run.run_start,
        'run_end': calibration_run.run_end,
        'validations': validation_response,
        'forecasts': forecast_response
    }

    # if performance metrics are unavailable, find the difference between start and end time as a fallback
    if calibration_run.performance_metrics:
        response['elapsed_time'] = calibration_run.performance_metrics.elapsed_time
    else:
        response[
            'elapsed_time'] = calibration_run.run_end - calibration_run.run_start if calibration_run.run_end and calibration_run.run_start else None

    # Conditionally add calibration run performance metrics to response if requested and status is DONE or FAIL
    if calibration_metrics:
        response['performance_metrics'] = calibration_metrics

    # Add error messages if applicable
    if calibration_run.status in [StatusEnum.SAVED.db_instance, StatusEnum.READY.db_instance]:
        error_object, _ = ngen_cal_input.ready_to_run(calibration_run)
        if error_object:
            if error_object.has_warnings():
                response['warnings'] = error_object.warnings
            if error_object.has_errors():
                response['errors'] = error_object.errors

    response_validator, error_response = validate_response(GetStatusResponseSerializer, response,
                                                           fields_to_truncate=['validations', 'forecasts'],
                                                           max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["validations", "forecasts"], max_length=10))}'
    )
    logger.debug(f"[DEBUG] view request type: {type(request)}")
    logger.debug(f"[DEBUG] request._request type: {type(getattr(request, '_request', None))}")
    logger.debug(f"[DEBUG] elapsed_time on _request: {getattr(getattr(request, '_request', None), 'elapsed_time', 'MISSING')}")

    return Response(response_validator.data)


@extend_schema(
    request=GetStatusForComparisonRequestSerializer,
    responses={
        200: GetStatusForComparisonResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the status of a calibration job and associated validation and forecast jobs"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_status_for_comparison(request: Request) -> Response:
    """
    Retrieves the status of multiple calibration jobs, including performance metrics.
    calibration_run_ids should be given as an array.
    Runs in READ ONLY mode to avoid locking contention.

    :param request: HTTP request containing calibration run details.
    :return: JSON response with the status and associated job details.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetStatusForComparisonRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')

    response = {
        'calibration_run_ids': calibration_run_ids,
        'statuses': [],
        'errors': []
    }

    with readonly_transaction():
        for calibration_run_id in calibration_run_ids:
            calibration_error = None

            calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
            if error_return:
                calibration_error = {'calibration_run_id': calibration_run_id, 'message': error_return}

            if not calibration_error:
                calibration_metrics = (
                    get_performance_metrics(calibration_run.performance_metrics)
                    if calibration_run.status in [StatusEnum.DONE.db_instance, StatusEnum.FAILED.db_instance]
                    else None
                )
                # Prepare the response for this job
                status_response = {
                    'calibration_run_id': calibration_run.id,
                    'formulation_name': calibration_run.user_formulation_name,
                    'status': calibration_run.status.name,
                    'submit_date': calibration_run.submit_date,
                    'run_start': calibration_run.run_start,
                    'run_end': calibration_run.run_end,
                    'elapsed_time': (
                        calibration_run.performance_metrics.elapsed_time
                        if calibration_run.performance_metrics
                        else (
                            calibration_run.run_end - calibration_run.run_start
                            if calibration_run.run_end and calibration_run.run_start
                            else None
                        )
                    ),
                }

                if calibration_metrics:
                    status_response['performance_metrics'] = calibration_metrics

                response['statuses'].append(status_response)

            else:
                response['errors'].append(calibration_error)

    response_validator, error_response = validate_response(GetStatusForComparisonResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=RunCalibrationJob,
    responses={
        200: SubmitCalibrationJobResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Run a calibration"
)
@api_view(['POST'])
@handle_exceptions
def run_calibration(request: Request) -> Response:
    """
    Submits a calibration job for processing.

    :param request: HTTP request containing calibration run details.
    :return: JSON response indicating job submission status.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(RunCalibrationJob, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    logging_config = validator.get('logging_config')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return

    error_response = submit_job(run, logging_config=logging_config)
    if error_response:
        return error_response

    response = {'message': f'Calibration Job {run.id} has been submitted',
                'calibration_run_id': calibration_run_id,
                'status': run.status.name,
                'submit_date': run.submit_date}

    response_validator, error_return = validate_response(SubmitCalibrationJobResponseSerializer, response)
    if error_return:
        return error_return

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def get_performance_metrics(performance_metrics) -> dict[str, str | int | float | None]:
    """
    Helper function to retrieve selected performance metrics, converting numeric fields to 'K' units.
    """
    if not performance_metrics:
        return {field: None for field in [
            "elapsed_time", "num_cpus", "cpu_time", "max_rss", "max_disk_read", "max_disk_write", "reserved_time", "io_throughput"
        ]}

    # Convert numeric fields to kilobytes
    metrics_dict = model_to_dict(performance_metrics, fields=[
        "elapsed_time", "num_cpus", "cpu_time", "max_rss", "max_disk_read", "max_disk_write", "reserved_time"
    ])
    # Manually add io_throughput since it's a generated field
    metrics_dict["io_throughput"] = performance_metrics.io_throughput

    # Convert relevant fields to 'K' units
    for field in ["max_rss", "max_disk_read", "max_disk_write"]:
        value = metrics_dict.get(field)
        if value is not None:  # Only convert non-null values
            metrics_dict[field] = f"{value:.2f}K"

    # Format io_throughput in 'K/s'
    io_throughput = metrics_dict.get("io_throughput")
    if io_throughput is not None:
        metrics_dict["io_throughput"] = f"{io_throughput:.2f}K/s"

    return metrics_dict


def should_include_metrics(run_status: Status, include_performance_metrics: bool = False) -> bool:
    """
    Determines if performance metrics should be included based on job status and request parameters.
    """
    return include_performance_metrics and run_status in [StatusEnum.DONE.db_instance, StatusEnum.FAILED.db_instance]


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Process the output of a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def process_calibration_output(request):
    """
    This endpoint is mostly for testing, to kick off the processing of output for a completed job.
    Normally read_calibration_output() is called automatically when a job completes.
    This endpoint can be used in case the output processing doesn't work.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE, StatusEnum.FAILED])

    if error_return:
        return error_return

    read_calibration_output(run, False)

    response = {'message': f"End of job processing completed for Calibration Job {run.id}",
                'calibration_run_id': run.id,
                'status': run.status.name}

    response_validator, error_response = validate_response(GenericResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ValidationRunSerializer,
    responses={
        200: GenericResponseSerializerWithValidator,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Process the output of a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def process_swe_timeseries(request: Request) -> Response:
    """
    This endpoint is mostly for testing, to kick off the processing of the SWE timeseries for a  completed job.
    Normally generate_swe_ts_data() is called automatically when a job completes.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    validator, error_return = validate_request(ValidationRunSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')

    run, error_return = get_validation_run(validation_run_id, request.user, run_status=[StatusEnum.DONE])

    if error_return:
        return error_return

    generate_swe_ts_data(run)

    response = {'message': f"SWE Timeseries processing completed for Validation Job {run.id}",
                'validation_run_id': run.id,
                'status': run.status.name}

    response_validator, error_response = validate_response(GenericResponseSerializerWithValidator, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@api_view(['GET', 'POST'])
@handle_exceptions
def update_mpi_rules(request: Request) -> Response:
    """
    Undocumented endpoint for updating the MPI rules
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    validator, error_return = validate_request(MPINodesRulesSerializer, data)
    if error_return:
        return error_return

    mpi_rules = validator.get('mpi_rules')
    if mpi_rules:
        ngen_cal_input.MPI_NODE_RULES = mpi_rules

    message = "Updated MPI Rules" if mpi_rules else "Current MPI Rules"
    response = {
        'message': message,
        'mpi_rules': ngen_cal_input.MPI_NODE_RULES
    }

    response_validator, error_response = validate_response(MPINodesRulesResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ReportIterationSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Report iteration of a running calibration"
)
# Called by ngen_cal
@api_view(['POST'])
@handle_exceptions
def report_iteration(request):
    """
    Reports an iteration for a running calibration job. This endpoint updates or creates an
    iteration record for a specific worker in the calibration job.

    Splits heavy reads into a READ ONLY transaction to reduce contention,
    then performs the write in a short atomic block.

    :param request: HTTP request containing iteration details.
    :return: JSON response indicating the success of the operation.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ReportIterationSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    iteration_number = validator.get('iteration')
    worker_name = validator.get('worker_name')
    first_iteration_for_worker = validator.get('first_iteration_for_worker')

    logger.debug(
        f"Report Iteration for calibration_run_id {calibration_run_id}, iteration number: {iteration_number}, "
        f"worker: {worker_name}, first_iteration: {first_iteration_for_worker}"
    )

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.RUNNING])
    if error_return:
        return error_return

    with transaction.atomic():
        if first_iteration_for_worker:
            # Atomically grab the next available worker number
            run = CalibrationRun.objects.select_for_update().get(id=run.id)
            worker_number = run.next_worker_number
            run.next_worker_number += 1
            run.save(update_fields=['next_worker_number'])
            logger.debug(f"Assigned new worker: '{worker_name}' #{worker_number}")
        else:
            # Use get() to fetch the latest iteration for the given worker_name and run
            existing_iteration = Iteration.objects.filter(calibration_run=run, worker_name=worker_name).order_by('-iteration_num').first()
            if existing_iteration:
                worker_number = existing_iteration.worker_number
            else:
                return ResponseError(f"Worker '{worker_name}' not found for calibration run {run.id}.")

        iteration_object, created = Iteration.objects.get_or_create(
            calibration_run=run,
            iteration_num=iteration_number,
            worker_name=worker_name,
            defaults={'worker_number': worker_number}
        )
        if not created:
            return ResponseError(
                f'Iteration object already exists for calibration run {run.id}, worker {worker_name}, iteration {iteration_number}'
            )

    response = {
        'message': f"Iteration {iteration_number} for worker_name '{worker_name}' set for Calibration Job {run.id}",
        'calibration_run_id': run.id,
        'status': run.status.name
    }

    response_validator, error_response = validate_response(GenericResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetIterationsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get iteration of a running calibration"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_iteration(request: Request) -> Response:
    """
    Retrieves the current iteration of a running calibration job.
    Runs in READ ONLY mode to avoid locking contention.

    :param request: HTTP request containing calibration run details.
    :return: JSON response with the current iteration details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    with readonly_transaction():
        # Allow status Ready for UI polling immediately after submission.
        run, error_return = get_calibration_run(
            calibration_run_id,
            request.user,
            run_status=[StatusEnum.READY, StatusEnum.RUNNING, StatusEnum.DONE,
                        StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return

        high_iteration = Iteration.objects.filter(calibration_run=run, worker_number=1).order_by('-iteration_num').first()
        high_iteration_number = high_iteration.iteration_num if high_iteration else None

        response = {'message': f'Calibration Job {run.id} has completed {high_iteration_number} iterations',
                    'calibration_run_id': run.id,
                    'status': run.status.name,
                    'iteration': high_iteration_number}

        response_validator, error_response = validate_response(GetIterationsResponseSerializer, response)
        if error_response:
            return error_response
        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=CalibrationOrValidationOrForecastRunSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Cancel a running job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def cancel_job(request: Request) -> Response:
    """
    Cancel a running job for CalibrationRun, ValidationRun, or ForecastRun.

    :param request: The HTTP request containing the run ID to cancel.
    :return: A Response indicating the cancellation result.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationOrValidationOrForecastRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')

    # Determine job type and retrieve the appropriate run instance
    if calibration_run_id:
        run_type = JobType.CALIBRATION.value
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED])
    elif validation_run_id:
        run_type = JobType.VALIDATION.value
        run, error_return = get_validation_run(validation_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED])
    else:
        # Retrieve the ForecastForcingDownloadRun regardless of its status,
        # using the provided forecast_run_id to get the forcing download run.
        # First, get the ForecastRun (to access the forcing_download_run id).
        forecast_run_unfiltered, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        forecast_forcing_download_run, forcing_error = get_forecast_forcing_download_run(
            forecast_run_unfiltered.forcing_download_run.id,
            request.user,
            run_status=list(StatusEnum)
        )
        if forcing_error:
            return forcing_error

        # Check the status of the forcing download run.
        if forecast_forcing_download_run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
            # Forcing download run is running: cancel it.
            run = forecast_forcing_download_run
            # Call it a Forecast job and not Forcing Download
            run_type = JobType.FORECAST.value
        elif forecast_forcing_download_run.status == StatusEnum.DONE.db_instance:
            # Forcing download run is done.
            # Retrieve the forecast run from the forcing run.
            forecast_run = forecast_forcing_download_run.forecast_run
            if forecast_run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
                run = forecast_run
                run_type = JobType.FORECAST.value

            else:
                error = (f'{ForecastRun.__name__} {forecast_run.id} is not in an allowed status: '
                         f'{join_with_or([StatusEnum.RUNNING.value, StatusEnum.SUBMITTED.value])}. '
                         f'Current status: {forecast_run.status.name}')
                return ResponseError(error)
        else:
            error = (f'{ForecastForcingDownloadRun.__name__} {forecast_forcing_download_run.id} is not in an allowed status: '
                     f'{join_with_or([StatusEnum.RUNNING.value, StatusEnum.SUBMITTED.value, StatusEnum.DONE.value])}. '
                     f'Current status: {forecast_forcing_download_run.status.name}')
            return ResponseError(error)

    if not cancel_job_common(run):
        return ResponseError(f"Unable to cancel {run_type.capitalize()} Job {run.id}")

    run.status = StatusEnum.CANCELLED.db_instance
    run.save(update_fields=['status'])

    response = {
        'message': f"{run_type.capitalize()} Job {run.id} has been canceled",
        f"{run_type}_run_id": run.id,
        'status': run.status.name  # type: ignore[attr-defined]
    }
    response_validator, error_response = validate_response(CancelJobResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def resolve_job_data_dir(run: CalibrationRun) -> str:
    """
    Resolves the job data directory for the given CalibrationRun object, converting paths if necessary
    based on the current settings.

    :param run: The CalibrationRun object.
    :return: The resolved host path to the job data directory as a plain string.
    :raises ValueError: If the path is not absolute or does not start with the expected root.
    """
    container_job_data_dir: str = run.job_data_dir

    if settings.NGEN_CAL_DATA_PATH and settings.NGEN_CAL_DATA_PATH != settings.NGEN_CAL_MOUNT_POINT:
        # Ensure the absolute path starts with the old root
        if not os.path.isabs(container_job_data_dir):
            raise ValueError(f"The path '{container_job_data_dir}' is not absolute.")
        if not container_job_data_dir.startswith(settings.NGEN_CAL_MOUNT_POINT):
            raise ValueError(f"The path '{container_job_data_dir}' does not start with the old root '{settings.NGEN_CAL_MOUNT_POINT}'.")

        # Replace the old root with the new root
        relative_path = os.path.relpath(container_job_data_dir, start=settings.NGEN_CAL_MOUNT_POINT)
        return os.path.join(settings.NGEN_CAL_DATA_PATH, relative_path)

    return container_job_data_dir


@extend_schema(
    request=CalibrationJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a calibration job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def calibration_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a calibration job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        CalibrationJobSlurmCallbackRequestSerializer,
        get_calibration_run,
        run_calibration_job_callback_pw
    )


@extend_schema(
    request=ValidationJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a validation job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def validation_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a validation job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ValidationJobSlurmCallbackRequestSerializer,
        get_validation_run,
        run_validation_job_callback_pw
    )


@extend_schema(
    request=ForecastForcingDownloadJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a forecast forcing download job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def forecast_forcing_download_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a forecast forcing download job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ForecastForcingDownloadJobSlurmCallbackRequestSerializer,
        get_forecast_forcing_download_run,
        run_forecast_forcing_download_job_callback_pw
    )


@extend_schema(
    request=ForecastJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a forecast job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def forecast_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a forecast job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ForecastJobSlurmCallbackRequestSerializer,
        get_forecast_run,
        run_forecast_job_callback_pw
    )


def handle_slurm_callback(request: Request, serializer_class, get_run_fn, job_end_callback_fn) -> Response:
    """
    Common handler for Slurm callback endpoints for any run type that inherits from BaseRun.

    :param request: The incoming HTTP request.
    :param serializer_class: The serializer used for validating the incoming data.
    :param get_run_fn: A function that returns the correct run object given its ID.
    :param job_end_callback_fn: A function that handles the job completion logic.
    :return: HTTP 202 Response or error Response.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(serializer_class, data)
    if error_return:
        return error_return

    run_id = validator.get(next(k for k in validator.keys() if k.endswith("_id")))
    job_status = validator.get("job_status")
    slurm_status = SlurmStatusEnum(job_status)

    # If Slurm is reporting that the job is now starting, we expect to be in Submitted status
    # For any other status changes, we should be Running or Submitted.  We allow Submitted just in case
    #  1) The job doesn't properly transition to Running
    #  2) To allow a submitted job to be canceled
    expected_status = [StatusEnum.SUBMITTED] if slurm_status == SlurmStatusEnum.STARTING else [StatusEnum.RUNNING, StatusEnum.SUBMITTED]

    run, error_return = get_run_fn(run_id, None, run_status=expected_status)
    if error_return:
        return error_return

    if slurm_status == SlurmStatusEnum.STARTING:
        logger.info(f'{get_job_description(run)} is starting')
        run.status = StatusEnum.RUNNING.db_instance
        run.run_start = datetime.now(timezone.utc)
        run.save(update_fields=["status", "run_start"])
    else:
        # Job has ended
        logger.info(f'{get_job_description(run)} is ending')
        job_end_callback_fn(run, slurm_status)

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
    return Response(status=status.HTTP_202_ACCEPTED)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: OpenApiResponse(
            response=OpenApiTypes.OBJECT,  # Indicates the response is an object
            description="Success",
            examples=[
                OpenApiExample(
                    'Example response',
                    value={'access': 'your_access_token_here'}
                )
            ],  # Defines the example using OpenApiExample
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return a token for use by Slurm"
)
@api_view(['GET'])
@handle_exceptions
def get_slurm_token(request: Request) -> Response:
    """
    Generates and returns a token for use by Slurm.

    :param request: HTTP request.
    :return: JSON response containing the generated token.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    return Response({'access': generate_custom_token(request.user, TOKEN_SLURM_SCOPE)})
