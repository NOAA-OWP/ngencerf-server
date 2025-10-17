import json
import logging
import os
import shutil

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import ForecastConfigEnum, StatusEnum
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, LoadForecastTabResponseSerializer, \
    ForecastRunSerializer, CreateAndRunForecastResponseSerializer, DeleteForecastRunResponseSerializer, CalibrationRunSerializer, \
    ForecastRunDataResponseSerializer
from calibration.util.ngen_locations import get_forecast_dir, get_forecast_output_file, get_cold_start_output_file
from calibration.views.calibration_secondary_data_views import read_csv_as_json
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, get_forecast_run, create_forecast_run_internal, \
    ResponseError, get_user_email, get_elapsed_str, readonly_transaction, get_calibration_run, truncate_large_fields

logger = logging.getLogger(__name__)


@extend_schema(
    request=CalibrationRunSerializer,
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

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    with readonly_transaction():
        # TODO This will only return active configurations.  Do we want to return everything and let the UI filter?
        configuration_values = ForecastConfigEnum.get_choices_with_fields(
            fields=['name', 'data_sources', 'time_range', 'is_active',
                    'cycle_start', 'cycle_end', 'cycle_freq', 'fcst_win', 'fcst_timestep', 'availability_lag'
                    ],
            extra_filter={'domain': calibration_run.gage.domain}
        )

    response = {'forecast_configuration_values': configuration_values}

    response_validator, error_response = validate_response(LoadForecastTabResponseSerializer,
                                                           response,
                                                           fields_to_truncate=['forecast_configuration_values'],
                                                           max_length=5
                                                           )
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - '
                 f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_configuration_values"], max_length=5))}'
                 )

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

    new_forecast_run = create_forecast_run_internal(run.calibration_run, run.cold_start_run, run.configuration, run.cycle_date)
    submit_job(new_forecast_run)

    response = {
        'message': f'Forecast Job {new_forecast_run.id} cloned from Job {run.id} and submitted for Calibration Job {new_forecast_run.calibration_run.id}',
        'calibration_run_id': new_forecast_run.calibration_run.id,
        'forecast_run_id': new_forecast_run.id,
        'submit_date': new_forecast_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ForecastRunSerializer,
    responses={
        200: ForecastRunDataResponseSerializer,
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
def get_forecast_data(request: Request) -> Response:
    """
    Load results for a forecast job (and related cold start job).

    :param request: HTTP request containing forecast_run_id
    :return: JSON response with forecast cycle values.
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

    # Read the output data from forecast (and possibly cold start)
    forecast_output = get_forecast_output_file(run)
    if not os.path.exists(forecast_output):
        raise FileNotFoundError(f"File not found: {forecast_output}")

    # Explicitly enforce the exact keys we want instead of inheriting CSV header
    forecast_data = read_csv_as_json(forecast_output, keys=["Time", "sim_flow"])

    cold_start_output = get_cold_start_output_file(run)
    if cold_start_output and os.path.exists(cold_start_output):
        cold_start_data = read_csv_as_json(cold_start_output, keys=["Time", "cold_start_flow"])

        data = []
        # Append all cold start rows first (cold_start_flow populated, sim_flow None),
        # then append all forecast rows (sim_flow populated, cold_start_flow None).
        for row in cold_start_data:
            data.append({
                "time": row["Time"],
                "cold_start_flow": row["cold_start_flow"],
                "sim_flow": None
            })
        for row in forecast_data:
            data.append({
                "time": row["Time"],
                "cold_start_flow": None,
                "sim_flow": row["sim_flow"]
            })

    else:
        # No cold start — forecast only
        data = [
            {"time": row["Time"], "cold_start_flow": None, "sim_flow": row["sim_flow"]}
            for row in forecast_data
        ]

    response = {
        'forecast_run_id': forecast_run_id,
        'plot_data': data,
        'total_count': len(data)  # since using full file read
    }
    # Validate and return response
    response_validator, error_response = validate_response(
        ForecastRunDataResponseSerializer, response,
        fields_to_truncate=['plot_data'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["plot_data"], max_length=10))}'
    )

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
    Delete a forecast job along with the associated cold start job. 
    In the future, we shouldn't delete the cold start job with the forecast.  It should be treated independently

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

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Forecast Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id
    forecast_dir = get_forecast_dir(run)  # Save before delete

    with transaction.atomic():
        cold_start_run = run.cold_start_run

        # Delete the Forecast Run
        run.delete()

        # Delete the Cold Start Run if linked
        if cold_start_run:
            cold_start_run.delete()

        logger.info(f"Deleting directory {forecast_dir}")
        shutil.rmtree(forecast_dir, ignore_errors=True)

        shutil.rmtree(get_forecast_dir(run), ignore_errors=True)

    message = f"Forecast Job {run_id} has been deleted"
    if cold_start_run:
        message += f" along with Cold Start Run {cold_start_run.id}"

    response = {'message': message, 'forecast_run_id': run_id}

    response_validator, error_response = validate_response(DeleteForecastRunResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)
