import json
import logging
import os
import shutil

import yaml
from django.core.cache import cache
from django.db import transaction, router
from django.db.models.deletion import Collector
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.enums_vanilla import JobType
from calibration.models import VerificationRun
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, EmptySerializer, \
    VerificationJobDetailsResponseSerializer, VerificationJobSerializer, CreateVerificationJobRequestSerializer, \
    CreateVerificationJobResponseSerializer, RunVerificationJob, SubmitVerificationJobResponseSerializer, \
    GetVerificationStatusRequestSerializer, GetVerificationStatusResponseSerializer, \
    GetVerificationPlotNamesResponseSerializer, GetVerificationPlotRequestSerializer, GetVerificationPlotResponseSerializer, \
    DeleteVerificationJobResponseSerializer
from calibration.util.ngen_locations import get_verification_run_dir, get_verification_yaml_config_file
from calibration.views.calibration_run_views import get_performance_metrics, should_include_metrics, parse_failure_messages
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, \
    get_forecast_run, get_verification_run, ResponseError, get_user_email, get_elapsed_str, \
    create_verification_job_internal, png_to_base64_url, truncate_large_fields, get_job_description
from calibration.views.verification_input import create_verification_input

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: VerificationJobDetailsResponseSerializer,
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
        OpenApiParameter(name='verification_job_id', description='ID of the verification run', required=True, type=int)
    ],
    description="Load verification job data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_verification_job(request: Request) -> Response:
    """
    Load data for a verification job.

    - Calls create_verification_input(verification_job) to generate the config

    :param request: HTTP request containing verification_job_id
    :return: JSON response with forecast cycle values.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationJobSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    verification_job, error_return = get_verification_run(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    if not os.path.exists(get_verification_yaml_config_file(verification_job)):
        try:
            error = create_verification_input(verification_job)
            if error.has_errors():
                return ResponseError(error)
            # Set status to Ready if YAML file is created successfully
            verification_job.status = StatusEnum.READY.db_instance
            verification_job.save()
        except Exception as e:
            return ResponseError(f"Error: {e}")

    response = {
        'verification_job_id': verification_job.id,
        'status': verification_job.status.name,
        'created_at': verification_job.created_at,
        'submit_date': verification_job.submit_date,
        'run_start': verification_job.run_start,
        'run_end': verification_job.run_end
    }

    forecast_run, error_return = get_forecast_run(verification_job.forecast_run.id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    response['forecast_run'] = {
        'calibration_run_id': forecast_run.calibration_run.id,
        'domain_name': forecast_run.calibration_run.gage.domain.name,
        'forecast_run_id': forecast_run.id,
        'configuration': forecast_run.configuration.name,
        'cycle_date': verification_job.forecast_run.cycle_date,
        'gage_id': forecast_run.calibration_run.gage_id,
        'forecast_status': forecast_run.status.name,
        'submit_date': forecast_run.submit_date
    }

    response_validator, error_response = validate_response(VerificationJobDetailsResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=CreateVerificationJobRequestSerializer,
    responses={
        201: CreateVerificationJobResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create a new verification"
)
@api_view(['POST'])
@handle_exceptions
def create_verification_job(request: Request) -> Response:
    """
    Creates a new verification job for the requesting user.

    Handles the creation process by accepting verification details in the request, validating them,
    and creating a new verification job if the request is valid.

    :param request: The HTTP request object, containing user and verification job details.
    :return: A Response object with the serialized verification job data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateVerificationJobRequestSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    forecast_run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    with transaction.atomic():
        run = create_verification_job_internal(forecast_run)

        response = {'message': f'Verification Job {run.id} created', 'verification_run_id': run.id}

        response_validator, error_response = validate_response(CreateVerificationJobResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(json.dumps(response_validator.data))}')
        return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=RunVerificationJob,
    responses={
        200: SubmitVerificationJobResponseSerializer,
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
def run_verification(request: Request) -> Response:
    """
    Submits a verification job for processing.

    :param request: HTTP request containing calibration run details.
    :return: JSON response indicating job submission status.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(RunVerificationJob, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')
    logging_config = validator.get('logging_config')

    run, error_return = get_verification_run(verification_job_id, request.user)
    if error_return:
        return error_return

    error_response = submit_job(run, logging_config=logging_config)
    if error_response:
        return error_response

    response = {'message': f'Verification Job {run.id} has been submitted',
                'verification_job_id': verification_job_id,
                'status': run.status.name,
                'submit_date': run.submit_date}

    response_validator, error_return = validate_response(SubmitVerificationJobResponseSerializer, response)
    if error_return:
        return error_return

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=GetVerificationStatusRequestSerializer,
    responses={
        200: GetVerificationStatusResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the status of a verification job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_verification_status(request: Request) -> Response:
    """
    Retrieves the status of a verification job.
    Optionally includes performance metrics based on the request parameters.

    :param request: HTTP request containing verification run details.
    :return: JSON response with the status and associated job details.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetVerificationStatusRequestSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')
    include_performance_metrics = validator.get('include_performance_metrics')

    verification_job, error_return = get_verification_run(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    # Prepare the main response
    response = {
        'message': f'Verification Job {verification_job.id}, status is {verification_job.status.name}',
        'verification_job_id': verification_job.id,
        'status': verification_job.status.name,
        'submit_date': verification_job.submit_date,
        'run_start': verification_job.run_start,
        'run_end': verification_job.run_end,
    }

    if verification_job.run_end and verification_job.submit_date:
        response['elapsed_time'] = verification_job.run_end - verification_job.submit_date
    else:
        response['elapsed_time'] = None

    # Conditionally retrieve verification performance metrics
    verification_metrics = get_performance_metrics(verification_job.performance_metrics) if should_include_metrics(verification_job.status,
                                                                                                                   include_performance_metrics) else None

    # Conditionally add verification run performance metrics to response if requested and status is DONE or FAIL
    if verification_metrics:
        response['performance_metrics'] = verification_metrics

    fm_ver = parse_failure_messages(verification_job.failure_messages)
    if fm_ver is not None:
        response['failure_messages'] = fm_ver

    response_validator, error_response = validate_response(GetVerificationStatusResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )
    logger.debug(f"[DEBUG] view request type: {type(request)}")
    logger.debug(f"[DEBUG] request._request type: {type(getattr(request, '_request', None))}")
    logger.debug(f"[DEBUG] elapsed_time on _request: {getattr(getattr(request, '_request', None), 'elapsed_time', 'MISSING')}")

    return Response(response_validator.data)


@extend_schema(
    request=VerificationJobSerializer,
    responses={
        200: GetVerificationPlotNamesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get a list of plot names"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_verification_plot_names(request: Request) -> Response:
    """
    Retrieves the list of plot images for a verification job, filtered by applicable optimizations.

    :param request: The request containing either POST data or query parameters.
    :return: A JSON response with the run ID, list of plot images, and run status.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationJobSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    run, error_return = get_verification_run(verification_job_id, request.user,
                                             run_status=[StatusEnum.RUNNING, StatusEnum.DONE, StatusEnum.CANCELLED, StatusEnum.FAILED,
                                                         StatusEnum.SERVER_ERROR])
    if error_return:
        return error_return

    plot_names = []

    # For now, get verification plots directly from the file system
    try:
        with open(get_verification_yaml_config_file(run), 'r') as file:
            yaml_config_data = yaml.safe_load(file)
            if 'general' in yaml_config_data and 'nwm_configuration' in yaml_config_data['general']:
                verification_plot_location = os.path.join(get_verification_run_dir(run), 'plots', yaml_config_data['general']['nwm_configuration'])
                for root, dirs, files in os.walk(verification_plot_location):
                    if files:
                        for file_name in files:
                            plot_names.append({
                                'name': os.path.relpath(os.path.join(root, file_name), get_verification_run_dir(run)),
                                'display_name': file_name,
                                'description': f'Placeholder description of {file_name}',
                                'timeseries_available': False
                            })
    except Exception as e:
        logger.warning(f"Unable to get plots for {get_job_description(run)} due to error: {e}")

    response = {
        "verification_job_id": run.id,
        'plot_names': plot_names,
        'status': run.status.name
    }

    response_validator, error_response = validate_response(
        GetVerificationPlotNamesResponseSerializer,
        response,
        fields_to_truncate=['plot_names'], max_length=3

    )
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {json.dumps(response_validator.data)}')
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["plot_names"], max_length=3))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=GetVerificationPlotRequestSerializer,
    responses={
        200: GetVerificationPlotResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return a base64 URL for a verification plot image and the associated data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_verification_plot(request: Request) -> Response:
    """
    Retrieves a specific plot for a verification run, returning the plot file location.

    :param request: The request containing plot name.
    :return: A JSON response with plot details, or an error if the plot is not found.
    :raises ResponseError: If the plot cannot be found or an error occurs.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetVerificationPlotRequestSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    plot_name = validator.get('plot_name')

    # Replace spaces with underscores in plot_name to avoid CacheKeyWarning
    sanitized_plot_name = plot_name.replace(" ", "_").replace("/", "_")
    # Base cache key common part
    cache_key_base = f"{sanitized_plot_name}_{verification_job_id}"
    cache_key_plot_url = f"plot_url_{cache_key_base}"

    run, error_return = get_verification_run(verification_job_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.DONE])
    if error_return:
        return error_return

    # Just retrieve the file for now
    plot_file_path = os.path.join(get_verification_run_dir(run), plot_name)
    logger.info(f'Plot file path: {plot_file_path}')
    if os.path.exists(plot_file_path):
        plot_url = png_to_base64_url(plot_file_path)
        logger.info(f'Retrieving plot from {plot_file_path}')

        # Cache the plot_url
        cache.set(cache_key_plot_url, plot_url, timeout=3600)
    else:
        return ResponseError(
            f"Error while checking existence of plot '{plot_name}' for {JobType.VERIFICATION.value.capitalize()} {run.id}: File Not Found")

    response = {
        'plot_name': plot_name,
        'plot_url': plot_url,
        'plot_file_path': plot_file_path,
        'verification_job_id': verification_job_id
    }

    # Validate and return response
    response_validator, error_response = validate_response(
        GetVerificationPlotResponseSerializer, response,
        fields_to_truncate=['plot_url', 'plot_data'], max_length=10
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["plot_url"], max_length=10))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=VerificationJobSerializer,
    responses={
        200: DeleteVerificationJobResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a verification job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_verification_job(request: Request) -> Response:
    """
    Delete a verification job. Performs a hard delete if the run status is SAVED or READY, 
    and a soft delete otherwise.

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationJobSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    run, error_return = get_verification_run(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Verification Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id

    # Proceed with deletion
    hard_delete(run)

    response = {'message': f'Verification Job {run.id} and associated records have been deleted', 'verification_job_id': run_id}

    response_validator, error_response = validate_response(DeleteVerificationJobResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def hard_delete(run: VerificationRun) -> None:
    """
    Perform a hard delete on a verification run and its related records. Deletes associated files if they exist.

    :param run: The VerificationRun instance to be deleted.
    """
    collector = Collector(using=router.db_for_write(run.__class__))

    # Collect related objects that will be deleted due to cascade
    collector.collect([run])

    with transaction.atomic():
        # Collect related objects that will be deleted due to cascade
        collector.collect([run])

        logger.debug(f"Deleting (hard delete) Verification Job {run.id}, associated records and files")
        # Iterate through the collected objects and list IDs and other fields
        for model, instances in collector.data.items():
            logger.debug(f"Verification Job {run.id} - {model.__name__}: {len(instances)} instance(s) will be deleted")
            for instance in instances:
                logger.debug(f' - {instance}')

        job_data_dir = get_verification_run_dir(run)
        run.delete()
        logger.debug(f'Deleting directory {job_data_dir} for Calibration Job {run.id}')
        if os.path.exists(job_data_dir):
            shutil.rmtree(job_data_dir)
