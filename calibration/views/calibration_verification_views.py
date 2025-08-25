import json
import logging
import os
import shutil
import yaml

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.db import transaction, router
from django.db.models.deletion import Collector
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework import status

from calibration.enums import StatusEnum
from calibration.enums_vanilla import JobType
from calibration.util.file_util import delete_all_files_in_directory
from calibration.models import VerificationRun
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, EmptySerializer, \
    VerificationJobsResponseSerializer, VerificationJobSerializer, CreateVerificationJobResponseSerializer, UploadVerificationYamlFileRequestSerializer, \
    SaveVerificationSetupRequestSerializer, SaveVerificationSetupResponseSerializer, \
    UploadVerificationYamlFileRequestSerializer, UploadVerificationYamlFileResponseSerializer, \
    RunVerificationJob, SubmitVerificationJobResponseSerializer, \
    GetVerificationStatusRequestSerializer, GetVerificationStatusResponseSerializer, \
    GetVerificationPlotRequestSerializer, GetVerificationPlotResponseSerializer, \
    DeleteVerificationJobResponseSerializer
from calibration.views.calibration_run_views import get_performance_metrics, should_include_metrics
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, \
    get_verification_job, ResponseError, get_user_email, get_elapsed_str, create_verification_job_internal, \
    png_to_base64_url, truncate_large_fields

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: VerificationJobsResponseSerializer,
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
    Load data for a verification run.

    :param request: HTTP request containing verification_job_id
    :return: JSON response with forecast cycle values.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationJobSerializer, data)
    if error_return:
        return error_return
    
    verification_job_id = validator.get('verification_job_id')

    verification_job, error_return = get_verification_job(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    yaml_config_data = {}
    yaml_config_error_message = None
    if verification_job.verification_yaml_file_path:
        try:
            with open(verification_job.verification_yaml_file_path, 'r') as file:
                yaml_config_data = yaml.safe_load(file)
        except FileNotFoundError:
            yaml_config_error_message = "Error: Uploaded YAML file not readable."
        except yaml.YAMLError as exc:
            yaml_config_error_message = f"Error parsing YAML file: {exc}"

    response = {
        'verification_job_id': verification_job.id,
        'status': verification_job.status.name,
        'created_at': verification_job.created_at,
        'submit_date': verification_job.submit_date,
        'run_start': verification_job.run_start,
        'run_end': verification_job.run_end,
        'verification_yaml_file_path': verification_job.verification_yaml_file_path,
        'yaml_config_data': yaml_config_data,
        'yaml_config_error_message': yaml_config_error_message,
        'job_data_dir': verification_job.job_data_dir
    }

    response_validator, error_response = validate_response(VerificationJobsResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {json.dumps(response_validator.data)}')

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
    Delete a verification job. Performs a hard delete on all statuses.

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationJobSerializer, data)
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    run, error_return = get_verification_job(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Verification Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id

    with transaction.atomic():
        # Delete the Verifciation Job
        run.delete()

    response = {'message': f'Verification Job {run.id} and associated records have been deleted', 'verification_job_id': run_id}

    response_validator, error_response = validate_response(DeleteVerificationJobResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=EmptySerializer,
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

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    with transaction.atomic():
        run = create_verification_job_internal(request.user)

        response = {'message': f'Verification Job {run.id} created', 'verification_job_id': run.id, 'job_data_dir': resolve_job_data_dir(run)}

        response_validator, error_response = validate_response(CreateVerificationJobResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(json.dumps(response_validator.data))}')
        return Response(response_validator.data, status=status.HTTP_201_CREATED)


def resolve_job_data_dir(run: VerificationRun) -> str:
    """
    Resolves the job data directory for the given VerificationRun object, converting paths if necessary
    based on the current settings.

    :param run: The VerificationRun object.
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
    request=UploadVerificationYamlFileRequestSerializer,
    responses={
        200: UploadVerificationYamlFileResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Allow user to upload verification YAML file"
)
@api_view(['POST'])
@handle_exceptions
def upload_verification_yaml_file(request: Request) -> Response:
    """
    Upload YAML file for a verification job.

    This function handles the upload of a YAML file by saving it to the job-specific directory and updating the verification job.

    :param request: The HTTP request containing the YAML file data.
    :return: A JSON response confirming the upload or reporting errors.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(UploadVerificationYamlFileRequestSerializer, data, context={'request': request})
    if error_return:
        return error_return

    verification_job_id = validator.get('verification_job_id')

    run, error_return = get_verification_job(verification_job_id, request.user)
    if error_return:
        return error_return

    # Save to the run-specific observational directory
    verif_data_dir_root = resolve_job_data_dir(run)
    fs = FileSystemStorage(location=os.path.join(verif_data_dir_root, 'Verification_YAML'))

    # Create the directory
    os.makedirs(fs.location, exist_ok=True)

    files = request.FILES.getlist('verification_yaml_file')

    verification_yaml_file = files[0]

    # Delete the file if it's already there
    delete_all_files_in_directory(fs.location)
    verification_yaml_file_path = os.path.join(fs.location, verification_yaml_file.name)
    logger.info(f"Saving user-uploaded verification YAML file to {verification_yaml_file_path}")
    fs.save(verification_yaml_file.name, verification_yaml_file)

    run.verification_yaml_file_path = verification_yaml_file_path
    
    message = f"YAML file '{verification_yaml_file.name}' saved for Verification Job {run.id}"

    try:
        with open(verification_yaml_file_path, 'r') as file:
            yaml_config_data = yaml.safe_load(file)

            # Add hard-coded file paths to YAML
            yaml_config_data['file_paths'] = {
                'data_dir_root': verif_data_dir_root,
                'location_list_file': settings.VERF_LOCATION_LIST_FILE,
                'crosswalk_file': {
                    'nwm30': settings.VERF_CROSSWALK_FILE
                },
                'gage_meta_file': settings.VERF_GAGE_META_FILE,
                'geometry_file': settings.VERF_GEOMETRY_FILE
            }

            # Rename user-uploaded YAML file and then save the updated YAML in the original location
            temp_list = (verification_yaml_file.name).split('.')
            temp_list[-2] += '_raw'
            old_verification_yaml_file_name = '.'.join(temp_list)
            old_verification_yaml_file_path = os.path.join(fs.location, old_verification_yaml_file_name)
            os.rename(verification_yaml_file_path, old_verification_yaml_file_path)
            logger.info(f"Renaming raw YAML file from {verification_yaml_file_path} to {old_verification_yaml_file_path}")
            
            with open(verification_yaml_file_path, 'w') as updated_file:
              yaml.dump(yaml_config_data, updated_file, default_flow_style=False)
              logger.info(f"Writing new YAML file to {verification_yaml_file_path}")
          
        # Set run status to Ready only if the file can be read (validation to be added later)
        run.status = StatusEnum.READY.db_instance
    # except FileNotFoundError:
    #     message = "Error: Uploaded YAML file not readable."
    #     run.status = StatusEnum.SAVED.db_instance
    # except yaml.YAMLError as exc:
    #     message = f"Error parsing YAML file: {exc}"
    #     run.status = StatusEnum.SAVED.db_instance
    except Exception as exc:
        message = f"Error: {exc}"
        run.status = StatusEnum.SAVED.db_instance

    with transaction.atomic():
        run.save()

    response = {
        'message': message, 
        'verification_job_id': run.id,
        'verification_yaml_file': verification_yaml_file.name,
        'verification_yaml_file_path': verification_yaml_file_path,
        'yaml_config_data': yaml_config_data,
        'status': run.status.name
      }

    response_validator, error_response = validate_response(UploadVerificationYamlFileResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)

# Commenting out this endpoint for now since the file upload handles the save already
# @extend_schema(
#     request=SaveVerificationSetupRequestSerializer,
#     responses={
#         200: SaveVerificationSetupResponseSerializer,
#         400: OpenApiResponse(
#             response=ErrorResponseSerializer,
#             description="Validation error or parsing error"
#         ),
#         500: OpenApiResponse(
#             response=ErrorResponseSerializer,
#             description="Internal server error"
#         )
#     },
#     description="Save gage tab data"
# )
# @api_view(['POST'])
# @handle_exceptions
# def save_verification_setup(request: Request) -> Response:
#     """
#     Save verification setup and update the verification job with new information.

#     This function handles updating the YAML file, clearing previously ploaded files, and updating the 
#     verification job status.

#     :param request: The HTTP request containing POST data with verification setup details.
#     :return: A JSON response confirming the update and including any errors.
#     """
#     data = request.data
#     logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

#     validator, error_return = validate_request(SaveVerificationSetupRequestSerializer, data)
#     if error_return:
#         return error_return

#     verification_job_id = validator.get('verification_job_id')
#     verification_yaml_file = validator.get('verification_yaml_file')

#     run, error_return = get_verification_job(verification_job_id, request.user)
#     if error_return:
#         return error_return
    
#     # Update the YAML file path - this might not be needed if the upload endpoint takes care of it
#     if run.verification_yaml_file_path != verification_yaml_file:
#         run.verification_yaml_file_path = verification_yaml_file

#     #Set run status to Ready
#     run.status = StatusEnum.READY

#     with transaction.atomic():
#         run.save()

#     response = {'message': f'Verification Job {run.id} updated', 'verification_job_id': run.id, 'status': run.status.name,
#                 'verification_yaml_file_path': verification_yaml_file}

#     response_validator, error_response = validate_response(SaveVerificationSetupResponseSerializer, response)
#     if error_response:
#         return error_response
#     logger.debug(
#         f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
#         f'{json.dumps(response_validator.data)}'
#     )

#     return Response(response_validator.data)


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

    run, error_return = get_verification_job(verification_job_id, request.user)
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

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

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

    verification_job, error_return = get_verification_job(verification_job_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    
    # Conditionally retrieve verification performance metrics
    verification_metrics = get_performance_metrics(verification_job.performance_metrics) if should_include_metrics(verification_job.status,include_performance_metrics) else None

    # Prepare the main response
    response = {
        'message': f'Verification Job {verification_job.id}, status is {verification_job.status.name}',
        'verification_job_id': verification_job.id,
        'status': verification_job.status.name,
        'submit_date': verification_job.submit_date,
        'run_start': verification_job.run_start,
        'run_end': verification_job.run_end,
        'elapsed_time': verification_job.performance_metrics.elapsed_time if verification_job.performance_metrics else None
    }

    # Conditionally add verification run performance metrics to response if requested and status is DONE or FAIL
    if verification_metrics:
        response['performance_metrics'] = verification_metrics

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

    plot_url = cache.get(cache_key_plot_url)
    plot_file_path = None
    plot_url_calculated = False  # Tracks if plot_url was calculated in this request

    run, error_return = get_verification_job(verification_job_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.DONE])
    if error_return:
        return error_return

    # Just retrieve the file for now
    plot_file_path = os.path.join(run.job_data_dir, plot_name)
    logger.info(f'Plot file path: {plot_file_path}')
    if os.path.exists(plot_file_path):
        plot_url = png_to_base64_url(plot_file_path)
        logger.info(f'Retrieving plot from {plot_file_path}')

        # Cache the plot_url
        cache.set(cache_key_plot_url, plot_url, timeout=3600)
    else:
        return ResponseError(f"Error while checking existence of plot '{plot_name}' for {JobType.VERIFICATION.value.capitalize()} {run.id}: File Not Found")

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

    run, error_return = get_verification_job(verification_job_id, request.user, run_status=list(StatusEnum))
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
    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

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

        job_data_dir = run.job_data_dir
        run.delete()
        logger.debug(f'Deleting directory {job_data_dir} for Calibration Job {run.id}')
        if os.path.exists(job_data_dir):
            shutil.rmtree(job_data_dir)
