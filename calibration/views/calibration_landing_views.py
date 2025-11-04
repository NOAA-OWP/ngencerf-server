import json
import logging
import os
import shutil
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction, router
from django.db.models.deletion import Collector
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, JobGenesis, ForecastConfigEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import FooterResponseSerializer, \
    ErrorResponseSerializer, CreateCalibrationRunResponseSerializer, \
    CalibrationRunSerializer, ImportResponseSerializer, \
    CreateAndRunValidationResponseSerializer, CreateValidationRequestSerializer, \
    EmptySerializer, CreateForecastRequestSerializer, CreateAndRunForecastResponseSerializer, \
    ArchiveJobRequestSerializer, GetGitInfoResponseSerializer, CalibrationRunIdList, CalibrationRunListResponse, ImportSerializer, \
    LockJobRequestSerializer
from calibration.util.git_util import get_git_info_internal
from calibration.views import ngen_cal_input
from calibration.views.calibration_import_export_views import load_calibration_run_data, import_calibration_run_data
from calibration.views.calibration_run_views import resolve_job_data_dir
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, get_calibration_run, create_calibration_run_internal, ResponseError, \
    validate_request, create_validation_run_internal, create_forecast_run_internal, get_user_email, get_elapsed_str, readonly_transaction, \
    format_datetime, create_cold_start_run_internal, get_job_description

logger = logging.getLogger(__name__)

User = get_user_model()


@extend_schema(
    request=EmptySerializer,
    responses={
        201: CreateCalibrationRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create a new calibration"
)
@api_view(['POST'])
@handle_exceptions
def create_calibration_run(request: Request) -> Response:
    """
    Creates a new calibration run for the requesting user.

    Handles the creation process by accepting calibration details in the request, validating them,
    and creating a new calibration job if the request is valid.

    :param request: The HTTP request object, containing user and calibration run details.
    :return: A Response object with the serialized calibration run data.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    with transaction.atomic():
        run = create_calibration_run_internal(request.user)

        response = {'message': f'Calibration Job {run.id} created', 'calibration_run_id': run.id, 'job_data_dir': resolve_job_data_dir(run)}

        response_validator, error_response = validate_response(CreateCalibrationRunResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
            f'{json.dumps(response_validator.data)}'
        )
        return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateValidationRequestSerializer,
    responses={
        201: CreateAndRunValidationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create and run a new validation for a specific iteration"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_validation(request: Request) -> Response:
    """
    Creates and runs a new validation run for a specified calibration run and iteration.

    Validates the request, checks if a validation job already exists for the specified calibration run
    and iteration, and creates and submits a new validation job if not.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateValidationRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    iteration_id = validator.get('iteration_id')

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    # Check if a ValidationRun already exists for this CalibrationRun and Iteration
    existing_validation_run_id = (
        ValidationRun.objects.filter(
            calibration_run=calibration_run,
            iteration_id=iteration_id,
            status__in=[StatusEnum.DONE.db_instance, StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]
        )
        .values_list('id', flat=True)
        .first()
    )
    if existing_validation_run_id:
        return ResponseError(f'Validation Job {existing_validation_run_id} already exists for '
                             f'Calibration Job {calibration_run.id}, iteration id {iteration_id}')

    validation_run = create_validation_run_internal(
        calibration_run,
        iteration_id,
        validation_type=ValidationType.VALID_ITERATION
    )
    submit_job(validation_run)

    response = {
        'message': f'Validation Job {validation_run.id} created and submitted for Calibration Job {calibration_run.id}',
        'calibration_run_id': calibration_run.id,
        'validation_run_id': validation_run.id,
        'status': validation_run.status.name,
        'submit_date': validation_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunValidationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateForecastRequestSerializer,
    responses={
        201: CreateAndRunForecastResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create and run a new forecast with optional cold start"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_forecast(request: Request) -> Response:
    """
    Creates and runs a new forecast run with an optional cold start for a specified calibration run and cycle_name name.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateForecastRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    configuration_name = validator.get('configuration_name')
    cycle_date = validator.get('cycle_date')
    cold_start_date = validator.get('cold_start_date')
    logging_config = validator.get('logging_config')

    run_cold_start = cold_start_date is not None

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    forecast_errors = []

    configuration = ForecastConfigEnum.get_instance(configuration_name)
    if configuration.domain != calibration_run.gage.domain:
        forecast_errors.append(f"{configuration_name} is not a valid configuration for domain {calibration_run.gage.domain.name}")

    # Define allowed cycle date range
    min_cycle_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    max_cycle_date = datetime.now(tz=timezone.utc)

    # Reject cycles earlier than the minimum allowed date
    if cycle_date < min_cycle_date:
        forecast_errors.append(f"Cycle cannot start before {format_datetime(min_cycle_date)}")

    # Adjust the maximum allowed cycle date based on forecast availability lag
    future_forecast_availability = configuration.availability_lag or 0

    # Subtract lag hours from max_cycle_date to account for delayed availability
    max_cycle_date = max_cycle_date - timedelta(hours=future_forecast_availability)

    # Warn if cycle date is later than adjusted maximum availability
    if cycle_date > max_cycle_date:
        # TODO Check what to do with this.  Is it a fatal error or just a warning?
        forecast_errors.append(f"Forecast availability is not guaranteed less than {future_forecast_availability} hours ahead of time.")

    if (cycle_date.hour - configuration.cycle_start) % configuration.cycle_freq != 0:
        # Hour offset from cycle start must align with evenly by cycle frequency
        forecast_errors.append(
            f"Cycle hour {cycle_date.hour}:00 is not available. Forecasts are available every {configuration.cycle_freq} hours from {configuration.cycle_start}:00 to {configuration.cycle_end}:00.")

    # If a cold start date is provided, validate its position relative to cycle date
    if run_cold_start:
        if cold_start_date >= cycle_date:
            forecast_errors.append("Cold start date must be earlier than cycle date")
        if cold_start_date < min_cycle_date:
            forecast_errors.append(f"Cold start cannot be before {format_datetime(min_cycle_date)}")

    if forecast_errors:
        return ResponseError("Error submitting forecast", errors=forecast_errors)

    cold_start_run = create_cold_start_run_internal(
        calibration_run,
        configuration,
        cold_start_date=cold_start_date,
        cycle_date=cycle_date
    ) if cold_start_date else None

    forecast_run = create_forecast_run_internal(
        calibration_run,
        cold_start_run,
        configuration,
        cycle_date
    )

    if run_cold_start:
        # Forecast Job will run automatically after the cold start
        submit_job(cold_start_run, logging_config=logging_config)
    else:
        submit_job(forecast_run, logging_config=logging_config)

    msg = get_job_description(cold_start_run if run_cold_start else forecast_run) + ' created and submitted'
    if run_cold_start:
        msg += f', followed by Forecast Job {forecast_run.id}'
    response = {
        'message': msg,
        'calibration_run_id': calibration_run.id,
        'forecast_run_id': forecast_run.id,
        'cold_start_run_id': cold_start_run.id if run_cold_start else None,
        'submit_date': cold_start_run.submit_date if run_cold_start else forecast_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: FooterResponseSerializer,
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get footer data"
)
@api_view(['POST', 'GET'])
@handle_exceptions
@permission_classes([AllowAny])
def get_footer(request: Request) -> Response:
    """
    Retrieve footer data such as version and contact email.

    :param request: The HTTP request object.
    :return: A Response object with version and contact information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    response = {
        "ngenCerf_version": settings.NGENCERF_VERSION,
        "ngenCerf_date": settings.NGENCERF_DATE,
        "ngenCerf_copyright": settings.NGENCERF_COPYRIGHT,
        "contact_email": settings.CONTACT_EMAIL
    }

    response_validator, error_response = validate_response(FooterResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetGitInfoResponseSerializer,
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get footer data"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_git_info(request: Request) -> Response:
    """
    Retrieve git info for all components

    :param request: The HTTP request object.
    :return: A Response object with version and contact information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    response = {"git_info": get_git_info_internal()}

    response_validator, error_response = validate_response(GetGitInfoResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data, default=str)}')
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: ImportResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Clone a calibration job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def clone_job(request: Request) -> Response:
    """
    Clone an existing calibration job, creating a new calibration run with identical parameters.

    Read-heavy parts (load_calibration_run_data) are executed in a read-only block,
    followed by the write-heavy import step in a separate transaction.

    :param request: The HTTP request object.
    :return: A Response object with the cloned calibration run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    # -------------------------------------------------------------
    # Read-only block: get the source run and prepare export data
    # -------------------------------------------------------------
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return

        calibration_run_data, _ = load_calibration_run_data(run, export=True)

    # -------------------------------------------------------------
    # Write block: import a new run from the exported data
    # -------------------------------------------------------------
    new_run, _, fatal_error = import_calibration_run_data(request, calibration_run_data, JobGenesis.CLONE)
    if fatal_error:
        return fatal_error

    # Set the new status to Saved and then we check it
    new_run.status = StatusEnum.SAVED.db_instance
    warnings = None
    errors = None
    if new_run.status in [StatusEnum.SAVED.db_instance, StatusEnum.RUNNING.db_instance]:
        error_object, _ = ngen_cal_input.ready_to_run(new_run)
        if error_object.has_warnings():
            warnings = error_object.warnings
        if error_object.has_errors():
            errors = error_object.errors

    # noinspection PyUnresolvedReferences
    response = {'message': f'Calibration Job {run.id} has been cloned to Calibration Job {new_run.id}',
                'calibration_run_id': new_run.id,
                'status': new_run.status.name}
    # I agree that the message handling got out of hand
    if warnings:
        response['warnings'] = warnings
    if errors:
        response['errors'] = errors

    response_validator, error_response = validate_response(ImportResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@lru_cache
def get_running_statuses():
    return [
        StatusEnum.RUNNING.db_instance,
        StatusEnum.SUBMITTED.db_instance,
    ]


def has_running_associated_jobs(run: CalibrationRun) -> str | None:
    """
    Checks if the given calibration job or any associated jobs are currently running.

    This function checks if the calibration run itself, or any of its associated validation, forecast, or forcing download jobs, are running.

    :param run: The CalibrationRun instance to check.
    :return: A message indicating if the job or its associated jobs are running, or None if there are no running jobs.
    """
    running_statuses = get_running_statuses()

    # Check if the calibration run itself is running
    if run.status in running_statuses:
        return f'Calibration Job {run.id} is running. Cannot proceed while the job is running.'

    # Check if any associated validation jobs are running
    if ValidationRun.objects.filter(calibration_run=run, status__in=running_statuses).exists():
        return f'Calibration Job {run.id} has associated validation jobs that are still running. Cannot proceed until they are completed.'

    # Check if any associated forecast jobs are running
    if ForecastRun.objects.filter(calibration_run=run, status__in=running_statuses).exists():
        return f'Calibration Job {run.id} has associated forecast jobs that are still running. Cannot proceed until they are completed.'

    # # Check if any associated forcing download jobs are running
    # if ForecastForcingDownloadRun.objects.filter(forecast_run__calibration_run=run, status__in=running_statuses).exists():
    #     return f'Calibration Job {run.id} has associated forcing download jobs that are still running. Cannot proceed until they are completed.'

    # No running jobs found
    return None


@extend_schema(
    request=CalibrationRunIdList,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_jobs(request: Request) -> Response:
    """
    Permanently delete multiple calibration jobs.

    :param request: The HTTP request object.
    :return: A Response object with the deletion status for each calibration job.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdList, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')

    job_results = []

    # Process each calibration_run_id in the list
    for calibration_run_id in calibration_run_ids:
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            job_results.append({
                "message": error_return.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Can't delete if the job is locked
        if run.is_locked:
            job_results.append({
                "message": f'Calibration Job {run.id} is locked for deletion',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Check for any running jobs (including the calibration job itself)
        running_jobs_error = has_running_associated_jobs(run)
        if running_jobs_error:
            job_results.append({
                "message": running_jobs_error,
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Proceed with deletion
        hard_delete(run)

        job_results.append({
            "message": f"Calibration Job {calibration_run_id} and associated records have been deleted",
            "calibration_run_id": calibration_run_id,
            "success": True
        })

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ArchiveJobRequestSerializer,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Archive or unarchive a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def archive_jobs(request: Request) -> Response:
    """
    Archive or unarchive multiple calibration jobs. Essentially a soft delete by setting an archive flag.

    :param request: The HTTP request object.
    :return: A Response object with the archive confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ArchiveJobRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')
    archive = validator.get('archive')

    job_results = []

    # Process each calibration_run_id in the list
    for calibration_run_id in calibration_run_ids:
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum), include_archived=True)
        if error_return:
            job_results.append({
                "message": error_return.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        if archive == run.is_archived:
            job_results.append({
                "message": f'Calibration Job {run.id} is {"already" if archive else "not"} archived',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Check for any running jobs (including the calibration job itself)
        if archive:
            running_jobs_error = has_running_associated_jobs(run)
            if running_jobs_error:
                job_results.append({
                    "message": running_jobs_error,
                    "calibration_run_id": calibration_run_id,
                    "success": False
                })
                continue

        run.is_archived = archive
        # If we're archiving, then unlock it
        run.is_locked = False if archive else run.is_locked
        run.save(update_fields=['is_archived', 'is_locked'])

        job_results.append({
            'message': f'Calibration Job {run.id} has been {"archived" if archive else "unarchived"}',
            "calibration_run_id": calibration_run_id,
            "success": True
        })

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=LockJobRequestSerializer,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Lock or unlock a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def lock_jobs(request: Request) -> Response:
    """
    Lock or unlock multiple calibration jobs.  Locking a job prevents it from being deleted

    :param request: The HTTP request object.
    :return: A Response object with the lock confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(LockJobRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')
    lock = validator.get('lock')

    job_results = []

    # Process each calibration_run_id in the list
    for calibration_run_id in calibration_run_ids:
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            job_results.append({
                "message": error_return.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        if lock == run.is_locked:
            job_results.append({
                "message": f'Calibration Job {run.id} is {"already" if lock else "not"} locked',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        run.is_locked = lock
        run.save(update_fields=['is_locked'])

        job_results.append({
            'message': f'Calibration Job {run.id} has been {"locked" if lock else "unlocked"}',
            "calibration_run_id": calibration_run_id,
            "success": True
        })

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def hard_delete(run: CalibrationRun) -> None:
    """
    Perform a hard delete on a calibration run and its related records. Deletes associated files if they exist.

    :param run: The CalibrationRun instance to be deleted.
    """
    collector = Collector(using=router.db_for_write(run.__class__))

    # Collect related objects that will be deleted due to cascade
    collector.collect([run])

    job_data_dir = run.job_data_dir  # stash before delete

    with transaction.atomic():
        logger.debug(f"Deleting (hard delete) Calibration Job {run.id}, associated records and files")
        # Iterate through the collected objects and list IDs and other fields
        for model, instances in collector.data.items():
            logger.debug(f"Calibration Job {run.id} - {model.__name__}: {len(instances)} instance(s) will be deleted")
            for instance in instances:
                logger.debug(f' - {instance}')
        run.delete()
    logger.debug(f'Deleting directory {job_data_dir} for Calibration Job {run.id}')
    if os.path.exists(job_data_dir):
        shutil.rmtree(job_data_dir)


@extend_schema(
    request=ImportSerializer,
    responses={
        200: ImportResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Import a job"
)
@api_view(['POST'])
@handle_exceptions
def import_job(request: Request) -> Response:
    """
    API endpoint to import (create) a calibration job or update an existing job.
     It validates input data, imports calibration run data, and optionally submits a job.

    :param request: Django HTTP request containing job import data.
    :return: HTTP response indicating success or error status.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ImportSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    data = validator.get('data')
    run_after_import = data.get('run_after_import', False)

    if calibration_run_id:
        calibration_run, error_return = get_calibration_run(calibration_run_id, request.user)
        if error_return:
            return error_return
    else:
        calibration_run = None

    run, messages, errors = import_calibration_run_data(request, data, JobGenesis.IMPORT, run=calibration_run)
    if errors:
        return errors

    imported_and_submitted = 'updated' if calibration_run_id else 'imported'

    error_object, config_file = ngen_cal_input.ready_to_run(run)

    if run_after_import and not error_object.warnings and not error_object.errors:
        error_response = submit_job(run)
        if error_response:
            return error_response
        imported_and_submitted = f"{imported_and_submitted} and submitted"

    response = {'message': f'Calibration Job {run.id} {imported_and_submitted}', 'calibration_run_id': run.id, 'status': run.status.name}
    if messages:
        response['messages'] = messages
    if error_object.warnings:
        response['warnings'] = error_object.warnings
    if error_object.errors:
        response['errors'] = error_object.errors

    response_validator, error_response = validate_response(ImportResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)
