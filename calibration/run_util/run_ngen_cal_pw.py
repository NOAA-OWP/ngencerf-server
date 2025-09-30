import functools
import logging
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import status

from calibration.enums import StatusEnum, SlurmStatusEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun
from calibration.models.base_run import BaseRun
from calibration.run_util.run_common import set_job_status, run_generic_job_end_callback, finalize_calibration_after_callback, \
    finalize_validation_after_callback, finalize_forecast_after_callback
from calibration.util.calibration_validators import GenericMessageResponseSerializer, SlurmSubmitResponseSerializer
from calibration.views.common import generate_custom_token, TOKEN_SLURM_SCOPE, get_job_description, validate_response_data

logger = logging.getLogger(__name__)

User = get_user_model()  # Dynamically fetch the custom user model


def submit_job_to_slurm(run: BaseRun, owner: User, arguments: dict[str, str], stdout_file: str) -> None:
    """
    Submits a job to Slurm, determining the appropriate endpoint, payload, and handling HTTP responses.

    This function determines the appropriate Slurm endpoint based on the job type,
    constructs the payload with input arguments and authentication token,
    and submits the job using an HTTP POST request.

    :param run: The CalibrationRun, ValidationRun, ForecastRun object.
    :param owner: The owner (user instance) of the job, used to generate the auth token.
    :param arguments: Dictionary containing command-line arguments for the job (e.g., 'input_file').
    :param stdout_file: The path to the file where job output will be written.
    :raises ValueError: If the run type is unsupported.
    :raises SlurmJobException: If there is an HTTP error during the job submission.
    """
    if isinstance(run, CalibrationRun):
        url_endpoint = settings.SLURM_SUBMIT_CALIBRATION_JOB_ENDPOINT
        payload = {
            'calibration_run_id': (None, run.id),
            'input_file': (None, arguments['input_file']),
            'output_file': (None, stdout_file),
            'nprocs': (None, arguments['nprocs'])
        }
    elif isinstance(run, ValidationRun):
        url_endpoint = settings.SLURM_SUBMIT_VALIDATION_JOB_ENDPOINT
        payload = {
            'validation_run_id': (None, run.id),
            'validation_type': (None, run.validation_type),
            'input_file': (None, arguments['input_file']),
            'output_file': (None, stdout_file),
            'nprocs': (None, arguments['nprocs']),
            'worker_name': (None, arguments.get('worker_name')),
            'iteration': (None, arguments.get('iteration_num'))
        }
    # TODO Have Alvaro remove Forecast Download
    elif isinstance(run, ForecastRun):
        url_endpoint = settings.SLURM_SUBMIT_FORECAST_JOB_ENDPOINT
        payload = {
            'forecast_run_id': (None, run.id),
            'validation_yaml': (None, arguments['validation_yaml']),
            'forecast_realization': (None, arguments['forecast_realization']),
            # 'cold_start_realization': (None, arguments['cold_start_realization']),
            'stdout_file': (None, stdout_file),
        }
    else:
        raise ValueError(
            f"Unsupported run type: {type(run).__name__}. Expected one of CalibrationRun, ValidationRun, ForecastRun."
        )

    url = urljoin(settings.SLURM_URL, url_endpoint)
    # Common payload preparation
    payload['auth_token'] = (None, generate_custom_token(owner, TOKEN_SLURM_SCOPE))

    logger.info(f"Submitting Slurm job to {url} with payload: {payload}")
    response = requests.post(url, files=payload)
    handle_slurm_http_error(response, url, run.id)

    logger.info(f"Slurm response for {url_endpoint}: {response.json()}")
    slurm_response = validate_response_data(
        SlurmSubmitResponseSerializer,
        response.json(),
        'Submit job response data from Slurm is not in the expected format',
    )

    # Dynamically update fields
    run.slurm_job_id = slurm_response.get('slurm_job_id')

    run.save(update_fields=['slurm_job_id'])
    logger.info(f"{get_job_description(run)} submitted successfully! Slurm id: {run.slurm_job_id}")


def check_pw_for_failure(run: BaseRun, slurm_status: SlurmStatusEnum) -> bool:
    """
    Checks the status of a job executed in a Parallel Works environment and updates its status accordingly.

    This function updates the job's status based on its Slurm completion status,
    and determines whether the job was successful, canceled, or failed.

    :param run: The job object (CalibrationRun, ValidationRun, ForecastRun, etc.) being monitored.
    :param slurm_status: The SlurmStatusEnum indicating the job's completion status.
    :return: True if the job failed or was canceled, False otherwise.
    """
    if slurm_status == SlurmStatusEnum.CANCELED:
        logger.error(f"{get_job_description(run)} was cancelled")
        set_job_status(run, StatusEnum.CANCELLED)
        return True
    elif slurm_status == SlurmStatusEnum.FAILED:
        logger.error(f"{get_job_description(run)} ending due to abnormal return code {slurm_status}")
        set_job_status(run, StatusEnum.FAILED)
        return True
    return False


# Parallel Works callbacks
# These callbacks are used to handle job completion events for Calibration, Validation, and Forecast jobs
# in the Parallel Works (PW) environment. They wrap the `run_generic_job_callback` function,
# providing environment-specific status checks (`check_pw_status`) and job-specific finalization functions.

# Handles the completion of a calibration job in the PW environment.
# - Uses `check_pw_status` to check the Slurm job's status (e.g., CANCELED or FAILED).
# - Executes `finalize_calibration` to read job output, mark the job as DONE, and possibly create validation runs.
run_calibration_job_callback_pw = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_pw_for_failure, finalize_func=finalize_calibration_after_callback
)

# Handles the completion of a validation job in the PW environment.
# - Uses `check_pw_status` to validate the job's status.
# - Executes `finalize_validation` to process validation results and potentially mark the best validation run.
run_validation_job_callback_pw = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_pw_for_failure, finalize_func=finalize_validation_after_callback
)

# Handles the completion of a forecast job in the PW environment.
# - Uses `check_pw_status` to validate the job's status.
# - Executes `finalize_forecast` to finalize the forecast job and mark it as DONE.
run_forecast_job_callback_pw = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_pw_for_failure, finalize_func=finalize_forecast_after_callback
)

# # Handles the completion of a forecast job in the PW environment.
# # - Uses `check_pw_status` to validate the job's status.
# # - Executes `finalize_forecast` to finalize the forecast job and mark it as DONE.
# run_forecast_forcing_download_job_callback_pw = functools.partial(
#     run_generic_job_end_callback, check_if_failed=check_pw_for_failure, finalize_func=finalize_forecast_forcing_download_after_callback
# )


def cancel_slurm_job(run: BaseRun) -> bool:
    """
    Cancel a running Slurm job by sending a cancellation request.

    This function constructs the payload with the Slurm job ID, sends an HTTP POST
    request to the Slurm cancellation endpoint, and validates the response.

    :param run: The CalibrationRun, ValidationRun, ForecastRun, etc. object to terminate.
    :return: True if the job was successfully canceled, False otherwise.
    :raises requests.exceptions.HTTPError: If the cancellation request fails with an HTTP error.
    """
    job_description = get_job_description(run)
    logger.info(f"Cancelling slurm job {run.slurm_job_id} for {job_description}")

    url = urljoin(settings.SLURM_URL, settings.SLURM_CANCEL_JOB_ENDPOINT)
    payload = {'slurm_job_id': (None, str(run.slurm_job_id))}

    logger.info(f'Slurm cancel-job payload to {url}: {payload}')
    response = requests.post(url, files=payload)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error(f"Call to Slurm {url} failed with {response.status_code}.")
        logger.error(f"Failed to cancel job: {response.json().get('error')}, {str(e)}")
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return False
        raise

    logger.info(f'Response from cancel slurm: {response.json()}')

    validate_response_data(GenericMessageResponseSerializer, response.json(),
                           'Cancel job response data from Slurm is not in the expected format')

    logger.info(f"{job_description} - {payload['slurm_job_id']} cancelled successfully")
    return True


class SlurmJobException(Exception):
    """
    Custom exception class for handling Slurm job-related errors.

    This exception encapsulates HTTP-related issues or invalid responses
    during Slurm job submissions or cancellations.

    :param message: The error message describing the exception.
    :param status_code: Optional HTTP status code associated with the error.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def handle_slurm_http_error(response: requests.Response, url: str, job_id: int) -> None:
    """
    Handle HTTP errors for Slurm job submissions or cancellations, and log detailed error messages.

    This function logs detailed error messages, validates response content,
    and raises a custom exception for unsupported or invalid responses.

    :param response: The HTTP response object from the Slurm API call.
    :param url: The URL that was called.
    :param job_id: The calibration or validation run ID.
   :raises SlurmJobException: If the response indicates an error or is in an unexpected format.
    """
    # Initialize variables to store status code and response text
    status_code = response.status_code
    response_text = response.text

    try:
        # Check if the response is HTML (likely an error page)
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' in content_type:
            logger.warning(f"Received HTML response from {url} for job {job_id} - truncating output")
            response_text = response_text[:500] + '... (truncated)'
            raise SlurmJobException(f"Call to {url} for job {job_id} returned HTML. Response text: {response_text}", status_code)

        # Raise an exception if the HTTP request failed
        response.raise_for_status()

    except requests.exceptions.HTTPError as e:
        message = f"Call to {url} failed with {status_code}. Response text: {response_text or 'No response received'}"
        logger.error(message)
        raise SlurmJobException(message, status_code) from e

    except requests.exceptions.RequestException as e:
        # Handle connection errors or timeouts
        message = f"Call to {url} for job {job_id} failed to connect or timed out."
        logger.error(message)
        raise SlurmJobException(message) from e
