import json
from datetime import datetime, timezone, timedelta

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, ForecastConfigEnum, JobGenesis
from calibration.models import ValidationRun
from calibration.run_util.job_lifecycle import launch_job
from calibration.util.calibration_validators import EmptySerializer, CreateCalibrationRunResponseSerializer, ErrorResponseSerializer, \
    CreateValidationRequestSerializer, CreateAndRunValidationResponseSerializer, CreateForecastRequestSerializer, \
    CreateAndRunForecastResponseSerializer, CreateHindcastRequestSerializer, CreateAndRunHindcastResponseSerializer, \
    CreateAndValidateHindcastResponseSerializer, CalibrationRunIdSerializer, ImportResponseSerializer
from calibration.views import ngen_cal_input
from calibration.views.calibration_import_export_views import load_calibration_run_data, import_calibration_run_data
from calibration.views.calibration_landing_views import logger, validate_forecast_cycle_date
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, get_user_email, validate_request, create_calibration_run_internal, validate_response, \
    get_elapsed_str, get_calibration_run, ResponseError, create_validation_run_internal, format_datetime, create_cold_start_run_internal, \
    create_forecast_run_internal, get_job_description, get_cold_start_run, create_hindcast_run_internal, readonly_transaction


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

        response = {
            'message': f'Calibration Job {run.id} created',
            'calibration_run_id': run.id,
            'job_data_dir': run.job_data_dir
        }

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

    Additionally, disallows submission if either the VALID_CONTROL or VALID_BEST
    validation job for this calibration run is still RUNNING or SUBMITTED.

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
    assert calibration_run is not None

    # ─────────────────────────────────────────────
    # Require BOTH VALID_CONTROL and VALID_BEST to be DONE.
    # Block if EITHER is missing or not DONE.
    # ─────────────────────────────────────────────
    required_types = [
        ValidationType.VALID_CONTROL.value,
        ValidationType.VALID_BEST.value,
    ]

    # Build dict of existing runs
    control_best_dict = {
        vr.validation_type: vr
        for vr in ValidationRun.objects.filter(
            calibration_run=calibration_run,
            validation_type__in=required_types
        )
    }

    # Ensure both exist and both are DONE
    for vt in required_types:
        vr = control_best_dict.get(vt)
        if not vr or vr.status != StatusEnum.DONE.db_instance:
            label = "VALID_CONTROL" if vt == ValidationType.VALID_CONTROL.value else "VALID_BEST"
            status_name = vr.status.name if vr else "MISSING"
            return ResponseError(
                f"Cannot submit a new validation job because {label} is {status_name} for "
                f"Calibration Job {calibration_run.id}. Both VALID_CONTROL and VALID_BEST must be DONE."
            )

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
    launch_job(validation_run)

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
    assert calibration_run is not None

    forecast_errors = []

    configuration = ForecastConfigEnum.get_instance(configuration_name)
    if configuration.domain != calibration_run.gage.domain:
        forecast_errors.append(
            f"{configuration_name} is not a valid configuration for domain "
            f"{calibration_run.gage.domain.name}"
        )

    # Define the supported forecast window. Forecast cycles before min_cycle_date
    # are invalid, and cycles after max_cycle_date may not yet be available.
    min_cycle_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    future_forecast_availability = configuration.availability_lag or 0
    max_cycle_date = datetime.now(tz=timezone.utc) - timedelta(hours=future_forecast_availability)

    # Validate the requested forecast cycle date against the configured schedule.
    validate_forecast_cycle_date(
        check_date=cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=forecast_errors,
        label="Cycle",
    )

    # If a cold start date is provided, it must be earlier than the requested
    # forecast cycle date and still within the supported forecast window.
    if run_cold_start:
        if cold_start_date >= cycle_date:
            forecast_errors.append("Cold start date must be earlier than cycle date")
        if cold_start_date < min_cycle_date:
            forecast_errors.append(f"Cold start cannot be before {format_datetime(min_cycle_date)}")

    if forecast_errors:
        return ResponseError("Error submitting forecast", errors=forecast_errors)

    cold_start_run = None
    if run_cold_start:
        cold_start_run = create_cold_start_run_internal(
            calibration_run,
            configuration,
            cold_start_date=cold_start_date,
            cycle_date=cycle_date
        )

    forecast_run = create_forecast_run_internal(
        calibration_run,
        cold_start_run,
        configuration,
        cycle_date
    )

    if cold_start_run is not None:
        # The Forecast Job will be submitted automatically after the Cold Start Job finishes.
        launch_job(cold_start_run, logging_config=logging_config)
    else:
        launch_job(forecast_run, logging_config=logging_config)

    if cold_start_run is not None:
        msg = (
            f'{get_job_description(cold_start_run)} created and submitted, '
            f'followed by {get_job_description(forecast_run)}'
        )
        submit_date = cold_start_run.submit_date
    else:
        msg = f'{get_job_description(forecast_run)} created and submitted'
        submit_date = forecast_run.submit_date

    response = {
        'message': msg,
        'calibration_run_id': calibration_run.id,
        'forecast_run_id': forecast_run.id,
        'cold_start_run_id': cold_start_run.id if cold_start_run is not None else None,
        'submit_date': submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateHindcastRequestSerializer,
    responses={
        201: OpenApiResponse(
            response={
                "oneOf": [
                    CreateAndRunHindcastResponseSerializer,
                    CreateAndValidateHindcastResponseSerializer,
                ]
            },
            description="Created and submitted hindcast, or validation-only response"
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
    description="Create and run a new hindcast with optional cold start, or validate only"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_hindcast(request: Request) -> Response:
    """
    Creates and runs a new hindcast run using either an existing cold start run
    or a newly created cold start for a specified calibration run.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateHindcastRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    configuration_name = validator.get('configuration_name')
    cycle_date = validator.get('cycle_date')
    interval_cycle = validator.get('interval_cycle')
    num_iterations = validator.get('num_iterations')
    cold_start_date = validator.get('cold_start_date')
    cold_start_run_id = validator.get('cold_start_run_id')
    logging_config = validator.get('logging_config')
    validate_only = validator.get('validate_only')

    if cold_start_run_id:
        if cold_start_date or cycle_date:
            return ResponseError(
                "You must specify either an existing cold start id, or both cycle_date "
                "and cold_start_date, but not both"
            )
    else:
        if not cold_start_date or not cycle_date:
            return ResponseError(
                "You must specify either an existing cold start id, or both cycle_date "
                "and cold_start_date"
            )

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return
    assert calibration_run is not None

    hindcast_errors = []

    configuration = ForecastConfigEnum.get_instance(configuration_name)

    if not configuration.supports_hindcast:
        hindcast_errors.append(f"Configuration '{configuration_name}' does not support hindcast")

    if configuration.domain != calibration_run.gage.domain:
        hindcast_errors.append(
            f"'{configuration_name}' is not a valid configuration for domain "
            f"{calibration_run.gage.domain.name}"
        )

    cold_start_run = None
    if cold_start_run_id:
        cold_start_run, error_return = get_cold_start_run(cold_start_run_id, request.user, run_status=[StatusEnum.DONE])
        if error_return:
            return error_return
        assert cold_start_run is not None

        if cold_start_run.calibration_run_id != calibration_run.id:
            hindcast_errors.append(
                f"Cold Start Job {cold_start_run.id} does not belong to Calibration Job {calibration_run.id}"
            )

        cold_start_date = cold_start_run.cold_start_date
        cycle_date = cold_start_run.cycle_date

    # Define the supported forecast window used to validate both the requested
    # hindcast cycle date and the furthest projected cycle date.
    min_cycle_date = datetime(2022, 1, 1, tzinfo=timezone.utc)

    # Forecast data may not be available immediately. Shift the latest allowed
    # cycle date backward by the configuration's availability lag.
    future_forecast_availability = configuration.availability_lag or 0
    max_cycle_date = datetime.now(tz=timezone.utc) - timedelta(hours=future_forecast_availability)

    # Validate the requested hindcast starting cycle date using the same
    # schedule rules as forecast.
    validate_forecast_cycle_date(
        check_date=cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=hindcast_errors,
        label="Cycle",
    )

    # Hindcast must also validate the furthest cycle date that could be reached
    # after advancing by interval_cycle hours for num_iterations steps.
    max_projected_cycle_date = cycle_date + timedelta(hours=interval_cycle * num_iterations)

    # Validate the furthest projected cycle date against the same schedule rules
    # as the starting cycle date.
    validate_forecast_cycle_date(
        check_date=max_projected_cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=hindcast_errors,
        label="Maximum projected cycle",
    )

    # Cold start date must be earlier than the requested
    # hindcast cycle date and still within the supported forecast window.
    if cold_start_date >= cycle_date:
        hindcast_errors.append("Cold start date must be earlier than cycle date")
    if cold_start_date < min_cycle_date:
        hindcast_errors.append(f"Cold start cannot be before {format_datetime(min_cycle_date)}")

    if hindcast_errors:
        return ResponseError("Error submitting hindcast", errors=hindcast_errors)

    if validate_only:
        response = {
            'message': 'Hindcast request is valid',
            'calibration_run_id': calibration_run.id,
            'cold_start_run_id': cold_start_run.id if cold_start_run is not None else None,
        }

        response_validator, error_response = validate_response(CreateAndValidateHindcastResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
        return Response(response_validator.data)

    run_cold_start = False

    # Need to create a new cold start if we don't already have one
    if not cold_start_run:
        cold_start_run = create_cold_start_run_internal(
            calibration_run,
            configuration,
            cold_start_date=cold_start_date,
            cycle_date=cycle_date
        )
        run_cold_start = True

    hindcast_run = create_hindcast_run_internal(
        calibration_run,
        cold_start_run,
        configuration,
        cycle_date,
        interval_cycle,
        num_iterations,
        created_new_cold_start=run_cold_start,
    )

    if run_cold_start:
        # The Hindcast Job will be submitted automatically after the Cold Start Job finishes.
        launch_job(cold_start_run, logging_config=logging_config)
    else:
        launch_job(hindcast_run, logging_config=logging_config)

    if run_cold_start:
        msg = (
            f'{get_job_description(cold_start_run)} created and submitted, '
            f'followed by {get_job_description(hindcast_run)}'
        )
        submit_date = cold_start_run.submit_date
    else:
        msg = f'{get_job_description(hindcast_run)} created and submitted'
        submit_date = hindcast_run.submit_date

    response = {
        'message': msg,
        'calibration_run_id': calibration_run.id,
        'hindcast_run_id': hindcast_run.id,
        'cold_start_run_id': cold_start_run.id,
        'submit_date': submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunHindcastResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CalibrationRunIdSerializer,
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

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
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
        assert run is not None

        calibration_run_data, _ = load_calibration_run_data(run, export=True)

    # -------------------------------------------------------------
    # Write block: import a new run from the exported data
    # -------------------------------------------------------------
    new_run, _, fatal_error = import_calibration_run_data(request, calibration_run_data, JobGenesis.CLONE)
    if fatal_error:
        return fatal_error
    assert new_run is not None

    # Set the new status to Saved and then we check it
    new_run.status = StatusEnum.SAVED.db_instance
    warnings = None
    errors = None
    if new_run.status in [StatusEnum.SAVED.db_instance, StatusEnum.RUNNING.db_instance]:
        error_object, _ = ngen_cal_input.ready_to_run(new_run)
        if error_object is not None:
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
