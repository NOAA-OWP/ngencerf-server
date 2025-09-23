import json
import logging
import shutil

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import ForecastCycleEnum, StatusEnum
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, EmptySerializer, LoadForecastTabResponseSerializer, \
    ForecastRunSerializer, CreateAndRunForecastResponseSerializer, DeleteForecastRunResponseSerializer
from calibration.util.ngen_locations import get_forecast_dir
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, get_forecast_run, create_forecast_run_internal, \
    ResponseError, get_user_email, get_elapsed_str, readonly_transaction

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: LoadForecastTabResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    parameters=[
        OpenApiParameter(name='calibration_run_id', description='ID of the calibration run', required=True, type=int)
    ],
    description="Load forecast tab data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_forecast_tab(request: Request) -> Response:
    """
    Load data for the forecast tab, including forecast cycles with associated data sources and time ranges.

    Runs inside a read-only transaction since no writes are performed.

    :param request: HTTP request containing calibration_run_id
    :return: JSON response with forecast cycle values.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    with readonly_transaction():
        cycle_values = ForecastCycleEnum.get_choices_with_fields(
            fields=['name', 'data_sources', 'time_range', 'is_active']
        )

    response = {'forecast_cycle_values': cycle_values}

    response_validator, error_response = validate_response(LoadForecastTabResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ForecastRunSerializer,
    responses={
        200: CreateAndRunForecastResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Clone and submit a forecast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def clone_and_run_forecast_job(request: Request) -> Response:
    """
    Clone an existing forecast job, creating a new calibration run with identical parameters.

    :param request: The HTTP request object.
    :return: A Response object with the cloned calibration run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastRunSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    new_forecast_run = create_forecast_run_internal(run.calibration_run, run.cycle)
    submit_job(new_forecast_run.forcing_download_run)

    response = {
        'message': f'Forcing download job for Forecast Job {new_forecast_run.id} cloned from Job {run.id} and submitted for Calibration Job {new_forecast_run.calibration_run.id}',
        'calibration_run_id': new_forecast_run.calibration_run.id,
        'forecast_run_id': new_forecast_run.id,
        'submit_date': new_forecast_run.forcing_download_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ForecastRunSerializer,
    responses={
        200: DeleteForecastRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a forecast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_forecast_job(request: Request) -> Response:
    """
    Delete a forecast job along with the associated forcing download job. Performs a hard delete if the run status is SAVED or READY, and a soft delete otherwise.

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastRunSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    if (run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance] or
            run.forcing_download_run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]):
        return ResponseError(f'Forecast Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id

    with transaction.atomic():
        # Delete the Forcing download Run  and that will automatically delete the Forecast Run
        run.forcing_download_run.delete()
        logger.info(f"Deleting directory {get_forecast_dir(run)}")
        shutil.rmtree(get_forecast_dir(run), ignore_errors=True)

    response = {'message': f'Forecast Job {run.id} and associated records have been deleted', 'forecast_run_id': run_id}

    response_validator, error_response = validate_response(DeleteForecastRunResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)
