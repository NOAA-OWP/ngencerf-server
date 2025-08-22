import json
import logging
import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
from datetimerange import DateTimeRange
from django.conf import settings
from django.db import transaction
from mswm.build_inputs import RealizationBuilder
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, SlurmStatusEnum, ForcingSourceEnum, ObservationalSourceEnum
from calibration.enums_vanilla import JobType
from calibration.models import CalibrationRun, ValidationRun, Iteration, ForecastRun, VerificationRun
from calibration.models.base_run import BaseRun
from calibration.models.forecast_forcing_download_run import ForecastForcingDownloadRun
from calibration.util.file_util import get_single_file
from calibration.util.git_util import get_git_info_internal
from calibration.util.ngen_locations import get_calibration_input_file, get_validation_best_stdout_file, get_validation_control_stdout_file, \
    get_calibration_stdout_file, get_validation_best_input_file, get_validation_control_input_file, get_validation_iteration_stdout_file, \
    get_forecast_forcing_download_stdout_file, get_forecast_stdout_file, get_geopackage_dir_for_job, get_forecast_forcing_download_path, \
    get_forecast_dir, get_forecast_forcing_config_file, get_validation_iteration_git_info_file, get_forecast_download_git_info_file, \
    get_validation_special_git_info_file, get_calibration_git_info_file, get_forecast_git_info_file, get_forcing_dir_for_job, \
    get_verification_git_info_file, get_verification_stdout_file, get_observational_file_for_job
from calibration.views import ngen_cal_input
from calibration.views.common import ResponseError, CerfException, create_validation_run_internal, get_job_description, write_ngen_logging_file
from calibration.views.end_of_job_processing import read_validation_output, read_calibration_output, read_forecast_output, read_verification_output
from calibration.views.forecast_forcing_input import build_forecast_forcing_download_config
from calibration.views.ngen_cal_input import ready_to_run
from cerfServer.settings import NgenEnvironmentEnum

logger = logging.getLogger(__name__)

# Job registry to store subprocess objects keyed by a tuple of (calibration_run_id, validation_run_id)
job_registry: dict[tuple[int, int], subprocess.Popen] = {}


def get_job_registry_key(run: BaseRun) -> tuple[int, int]:
    """
    Generate a unique key for the job registry based on run type.

    The first element is always the calibration run ID.
    The second element is the specific run ID or -1 for CalibrationRun.

    :param run: The CalibrationRun, ValidationRun, or ForecastRun object.
    :return: A tuple (calibration_run_id, specific_run_id).
    """
    if isinstance(run, CalibrationRun):
        return run.id, -1
    elif isinstance(run, (ValidationRun, ForecastRun)):
        return run.calibration_run.id, run.id
    elif isinstance(run, ForecastForcingDownloadRun):
        return run.forecast_run.calibration_run.id, run.forecast_run.id
    elif isinstance(run, VerificationRun):
        return run.id, -1

    raise TypeError(f"Unsupported run type: {type(run).__name__}")


def set_job_status(run: BaseRun, status: StatusEnum) -> None:
    """
    Update the status of a CalibrationRun, ValidationRun, or ForecastRun and clear the job registry if applicable.

    This function updates the `status` field of the job, clears the `slurm_job_id`,
    and removes the job from the global job registry for LOCAL or DOCKER environments.

    :param run: The CalibrationRun, ValidationRun, or ForecastRun object.
    :param status: The new status to set.
    """
    run.status = status.db_instance
    # Doesn't hurt to always update slurm_job_id, even though we only care in PW environment
    run.slurm_job_id = None
    run.save(update_fields=['status', 'slurm_job_id'])
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        key = get_job_registry_key(run)
        job_registry.pop(key, None)


def get_run_owner(run: BaseRun):
    """
    Retrieve the owner of a BaseRun object.

    Determines the owner of the job from its `CalibrationRun`, `ValidationRun`,
    or `ForecastRun` relationship.

    :param run: The BaseRun object (CalibrationRun, ValidationRun, etc.).
    :return: The owner of the associated CalibrationRun or the run itself.
    :raises AttributeError: If the owner cannot be determined.
    """
    if hasattr(run, 'owner'):  # CalibrationRun case
        return run.owner
    elif hasattr(run, 'calibration_run'):  # ValidationRun, ForecastRun
        return run.calibration_run.owner
    elif hasattr(run, 'forecast_run') and hasattr(run.forecast_run, 'calibration_run'):  # ForecastForcingDownloadRun
        return run.forecast_run.calibration_run.owner
    raise AttributeError(f"Cannot determine owner for run of type {type(run).__name__}")


def validate_cmd_args(cmd_line_args: dict[str, str], stdout_file: str) -> None:
    """
    Validates the command-line arguments and output file paths for LOCAL and DOCKER environments.

    This function ensures that all arguments passed to subprocess-based commands are valid types
    (str, bytes, or os.PathLike) and not None. It raises a TypeError if any invalid argument type
    is encountered, or a ValueError if any argument value is None.

    :param cmd_line_args: A dictionary of command-line arguments where the keys are argument names
                          and the values are their corresponding values.
    :param stdout_file: The path to the file where the job's stdout will be written.
                        It must be a valid path-like object.
    :raises TypeError: If any argument or the stdout file is not a valid type.
    :raises ValueError: If any argument value is None.
    """

    # Define allowed types for clarity
    allowed_types = (str, bytes, os.PathLike)

    # Validate each argument in the command-line arguments dictionary
    for key, value in cmd_line_args.items():
        if value is None:
            logger.error(f"Argument '{key}' is None, which is not allowed.")
            raise ValueError(f"Command-line argument '{key}' cannot be None.")

        # Check if the value is one of the allowed types
        if not isinstance(value, allowed_types):
            # Log the invalid argument with valid type information
            logger.error(
                f"Invalid argument for '{key}': {value} (type: {type(value)}). "
                f"Expected one of {allowed_types}."
            )
            # Raise a TypeError with details about the invalid argument
            raise TypeError(
                f"Invalid argument for '{key}': {value} (type: {type(value)}). "
                f"Expected one of {allowed_types}."
            )

    # Validate the stdout file path to ensure it's a valid type
    if not isinstance(stdout_file, allowed_types):
        # Log the invalid stdout file path with valid type information
        logger.error(
            f"Invalid stdout_file: {stdout_file} (type: {type(stdout_file)}). "
            f"Expected one of {allowed_types}."
        )
        # Raise a TypeError with details about the invalid stdout file path
        raise TypeError(
            f"Invalid stdout_file: {stdout_file} (type: {type(stdout_file)}). "
            f"Expected one of {allowed_types}."
        )


def execute_job(run: BaseRun, cmd_line_args: dict[str, str], stdout_file: str, simulate: bool = False) -> None:
    """
    Execute a job based on the configured NGEN environment.

    This function dynamically calls the appropriate job execution function
    based on the environment (LOCAL, DOCKER, or PARALLEL_WORKS).

    :param run: The BaseRun object (CalibrationRun, ValidationRun, etc.).
    :param cmd_line_args: A dictionary of command-line arguments for the job.
    :param stdout_file: The path to the file where the job's stdout will be written.
    :param simulate: For LOCAL or DOCKER jobs, if True, simulates successful execution without running a real job.
    :raises CerfException: If the environment is unsupported.
    """
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        # Validate for LOCAL and DOCKER environments
        validate_cmd_args(cmd_line_args, stdout_file)

        from calibration.run_util.run_ngen_cal_local import run_job_local
        run_job_local(run, cmd_line_args, stdout_file, simulate=simulate)
    elif settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        from calibration.run_util.run_ngen_cal_pw import submit_job_to_slurm
        # Resolve owner dynamically for the Slurm submission
        try:
            owner = get_run_owner(run)  # Use the utility function
        except AttributeError as e:
            raise CerfException(f"Error retrieving owner for run {run.id}: {str(e)}")
        submit_job_to_slurm(run, owner, cmd_line_args, stdout_file)
    else:
        raise CerfException(f"Unsupported environment: {settings.NGEN_ENVIRONMENT}")


def cancel_job_common(run: BaseRun) -> bool:
    """
    Cancel a job using the appropriate environment-specific logic.

    This function handles job cancellation for LOCAL, DOCKER, and PARALLEL_WORKS environments.

    :param run: The CalibrationRun, ValidationRun, or ForecastRun object.
    :return: True if the job was successfully canceled; False otherwise.
    """
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        from calibration.run_util.run_ngen_cal_local import cancel_local_job
        return cancel_local_job(run)
    elif settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        from calibration.run_util.run_ngen_cal_pw import cancel_slurm_job
        return cancel_slurm_job(run)
    else:
        logger.error(f"Unsupported environment: {settings.NGEN_ENVIRONMENT}")
        return False


def run_calibration_job(calibration_run: CalibrationRun) -> None:
    """
    Start a calibration job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param calibration_run: The CalibrationRun object representing the job.
    :raises CerfException: If the input file does not exist.
    """
    input_file = get_calibration_input_file(calibration_run)
    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Calibration Job {calibration_run.id}, user: {calibration_run.owner.username}"
        )

    stdout_file = get_calibration_stdout_file(calibration_run)

    execute_job(
        calibration_run,
        {'input_file': input_file, 'nprocs': str(calibration_run.mpi_nprocs)},
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.CALIBRATION, False)
    )


def run_validation_job(validation_run: ValidationRun) -> None:
    """
    Start a validation job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param validation_run: The ValidationRun object representing the job.
    :raises CerfException: If the input file does not exist.
    """
    if validation_run.validation_type == ValidationType.VALID_BEST.value:
        input_file = get_validation_best_input_file(validation_run.calibration_run)
        stdout_file = get_validation_best_stdout_file(validation_run.calibration_run)
    elif validation_run.validation_type == ValidationType.VALID_CONTROL.value:
        input_file = get_validation_control_input_file(validation_run.calibration_run)
        stdout_file = get_validation_control_stdout_file(validation_run.calibration_run)
    else:
        # Regular validation
        input_file = get_calibration_input_file(validation_run.calibration_run)
        stdout_file = get_validation_iteration_stdout_file(validation_run.calibration_run, validation_run.worker_name, validation_run.iteration_num)

    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Validation Job {validation_run.id}, "
            f"user: {validation_run.calibration_run.owner.username}, type: {validation_run.validation_type}"
        )

    cmd_line_args = {'input_file': input_file}
    if validation_run.validation_type == ValidationType.VALID_ITERATION.value:
        # For running local, we need to leave these out
        cmd_line_args['worker_name'] = validation_run.worker_name
        cmd_line_args['iteration_num'] = str(validation_run.iteration_num)
    cmd_line_args['nprocs'] = str(validation_run.calibration_run.mpi_nprocs)
    execute_job(
        validation_run,
        cmd_line_args,
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.VALIDATION, False)
    )


def run_forecast_forcing_download_job(forecast_forcing_download_run: ForecastForcingDownloadRun) -> None:
    """
    Start a forecast forcing download job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param forecast_forcing_download_run: The ForecastForcingDownloadRun object representing the job.
    """
    build_forecast_forcing_download_config(forecast_forcing_download_run)

    gpkg_file = get_single_file(get_geopackage_dir_for_job(forecast_forcing_download_run.forecast_run.calibration_run))
    cycle_name = forecast_forcing_download_run.forecast_run.cycle.internal_name
    config_file = get_forecast_forcing_config_file(forecast_forcing_download_run.forecast_run)
    forcing_dir = get_forecast_forcing_download_path(forecast_forcing_download_run.forecast_run)
    os.makedirs(forcing_dir, exist_ok=True)
    stdout_file = get_forecast_forcing_download_stdout_file(forecast_forcing_download_run.forecast_run)

    execute_job(
        forecast_forcing_download_run,
        {
            'cycle_name': cycle_name,
            'gpkg_file': gpkg_file,
            'config_file': config_file,
            'forcing_dir': forcing_dir
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.FORECAST_FORCING_DOWNLOAD, False)
    )


def run_forecast_job(forecast_run: ForecastRun) -> None:
    """
    Start a forecast job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param forecast_run: The ForecastRun object representing the job.
    """
    forcing_dir = get_forecast_forcing_download_path(forecast_run)
    validation_best_input = get_validation_best_input_file(forecast_run.calibration_run)
    forecast_dir = os.path.basename(get_forecast_dir(forecast_run))
    stdout_file = get_forecast_stdout_file(forecast_run)

    execute_job(
        forecast_run,
        {
            'forcing_dir': forcing_dir,
            'validation_best_input': validation_best_input,
            'forecast_dir': forecast_dir
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.FORECAST, False)
    )


def run_verification_job(verification_job: VerificationRun) -> None:
    """
    Start a verification job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param verification_job: The VerificationRun object representing the job.
    """
    verification_yaml_file_path = verification_job.verification_yaml_file_path
    verification_dir = verification_job.job_data_dir
    stdout_file = get_verification_stdout_file(verification_job)

    execute_job(
        verification_job,
        {
            'verification_yaml_file_path': verification_yaml_file_path,
            'verification_dir': verification_dir
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.VERIFICATION, False)
    )


def submit_job(run: BaseRun, logging_config=None) -> Response | None:
    """
    Submits a job by setting initial metadata and dispatching it to the appropriate execution function.

    - Sets the submission timestamp and updates the job status to 'SUBMITTED'.
    - For CalibrationRun, performs additional preprocessing, validation, and input generation.
    - Selects the appropriate job runner based on the job type (calibration, validation, forecast, etc.).
    - For each run type, a git info file is created prior to execution.
    - If an error occurs during submission, the job status is set to 'FAILED' and the error is logged.

    :param run: A CalibrationRun, ValidationRun, ForecastForcingDownloadRun, ForecastRun, or VerificationRun object.
    :param logging_config: Optional logging configuration to use when creating calibration job logs.
    :return: None if successful; a DRF Response object if the job is not ready or fails preprocessing.
    :raises CerfException: If the run type is unsupported or job execution fails.
    """
    if isinstance(run, CalibrationRun):
        # Before we attempt to submit, make sure it's ready
        error_object, _ = ready_to_run(run)
        if error_object.has_errors() or error_object.has_warnings():
            return ResponseError(error_object)

    with transaction.atomic():
        # Set submission date and status
        run.submit_date = datetime.now(timezone.utc)
        run.status = StatusEnum.SUBMITTED.db_instance
        run.save(update_fields=['submit_date', 'status'])

    try:
        # Special handling for calibration jobs
        if isinstance(run, CalibrationRun):
            write_ngen_logging_file(run, logging_config)
            fatal, response = prepare_calibration_job(run)
            if response:
                if fatal:
                    run.status = StatusEnum.FAILED.db_instance
                    run.save(update_fields=['status'])
                return response

        # Determine the appropriate job execution function
        if isinstance(run, CalibrationRun):
            create_git_info(get_calibration_git_info_file(run))
            run_calibration_job(run)
        elif isinstance(run, ValidationRun):
            if run.validation_type != ValidationType.VALID_ITERATION.value:
                create_git_info(get_validation_special_git_info_file(run))
            else:
                create_git_info(get_validation_iteration_git_info_file(run, run.worker_name, run.iteration_num))

            run_validation_job(run)
        elif isinstance(run, ForecastForcingDownloadRun):
            create_git_info(get_forecast_download_git_info_file(run))

            run_forecast_forcing_download_job(run)
        elif isinstance(run, ForecastRun):
            create_git_info(get_forecast_git_info_file(run))

            run_forecast_job(run)
        elif isinstance(run, VerificationRun):
            create_git_info(get_verification_git_info_file(run))

            run_verification_job(run)
        else:
            raise CerfException(f"Unsupported run type: {type(run).__name__}")
    except Exception as e:
        # Handle failures by marking the job as FAILED
        run.status = StatusEnum.FAILED.db_instance
        run.save(update_fields=['status'])

        logger.exception(f'Exception submitting {get_job_description(run)} - {str(e)}')
        raise  # Re-raise the exception

    logger.info(f"{get_job_description(run)} successfully submitted.")
    return None


def create_git_info(git_info_file: str) -> None:
    logger.info(f"Writing git info to {git_info_file}")
    git_info_data = get_git_info_internal()
    os.makedirs(os.path.dirname(git_info_file), exist_ok=True)
    with open(git_info_file, 'w') as f:
        f.write(json.dumps(git_info_data, indent=4))


def prepare_calibration_job(calibration_run: CalibrationRun) -> tuple[bool, Response | None]:
    """
    Prepare a CalibrationRun job by validating inputs, preprocessing data, and generating configuration files.

    This function performs:
    - Readiness validation using `ready_to_run`
    - Forcing/observational data subsetting
    - Input file generation using `create_input`

    This is only used internally by `submit_job` for CalibrationRun.

    :param calibration_run: The CalibrationRun object to prepare.
    :return: A tuple (fatal_error: bool, Response). If preparation is successful, returns (False, None).
             If errors occur, returns (True, error response) or (False, warning response).
    """
    error_object, config_file = ngen_cal_input.ready_to_run(calibration_run, build=True)

    if error_object.has_warnings() or error_object.has_errors():
        return error_object.has_errors(), ResponseError(
            f'Calibration Job {calibration_run.id} is not ready',
            validation_errors=error_object.warnings,
            errors=error_object.errors
        )

    try:
        logger.info(f'Final preparation to run Calibration Job {calibration_run.id}')
        validation_errors = final_preprocessing_for_calibration(calibration_run)

        if validation_errors:
            return True, ResponseError(
                f'Calibration Job {calibration_run.id} failed validation after preprocessing',
                errors=validation_errors
            )

        logger.info(f'Running RealizationBuilder.build_calib_realization for Calibration Job {calibration_run.id}')
        # create_input(config_file)
        rb = RealizationBuilder(config_file)
        rb.build_calib_realization()
    except Exception as e:
        CalibrationRun.objects.filter(id=calibration_run.id).update(status=StatusEnum.FAILED.db_instance)
        msg = f'Exception during build_calib_realization for Calibration Job {calibration_run.id} - {str(e)}'
        logger.exception(msg)
        raise CerfException(msg) from e

    logger.info(f'Return from build_calib_realization for Calibration Job {calibration_run.id}')
    return False, None


def create_and_submit_validation_control(calibration_run: CalibrationRun) -> None:
    """
    Create a validation run of type VALID_CONTROL and submit it.

    :param calibration_run: The CalibrationRun object for which the validation control run is created.
    """
    validation_run = create_validation_run_internal(calibration_run, None, validation_type=ValidationType.VALID_CONTROL)
    submit_job(validation_run)


def process_validation_output_and_maybe_create_best(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Process the validation output and create a new VALID_BEST run if the validation type is VALID_CONTROL.

    :param validation_run: The ValidationRun object representing the job run.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    job_description = get_job_description(validation_run)

    try:
        # Process the validation output
        read_validation_output(validation_run, failed_so_far)
        if not failed_so_far:
            set_job_status(validation_run, StatusEnum.DONE)
    except Exception as e:
        # Catch the exception and mark the job as FAILED
        logger.exception(f"Error processing validation output for {job_description}: {str(e)}")
        set_job_status(validation_run, StatusEnum.FAILED)
        return  # Stop further processing if the job failed

    if not failed_so_far:
        # If we just ran Validation Control, see if we want to run Validation Best
        if validation_run.validation_type == ValidationType.VALID_CONTROL.value:
            if validation_run.calibration_run.automatic_validation:
                best_validation_run = create_validation_run_internal(
                    validation_run.calibration_run, None, validation_type=ValidationType.VALID_BEST
                )
                # Set the iteration containing the best values before we run it
                iteration = Iteration.objects.filter(calibration_run=validation_run.calibration_run, best_params=True).get()
                best_validation_run.iteration = iteration
                best_validation_run.save(update_fields=['iteration'])
                submit_job(best_validation_run)


def run_generic_job_end_callback(
        run: BaseRun,
        status: Future | SlurmStatusEnum,
        check_if_failed: Callable[[BaseRun, Future | SlurmStatusEnum], bool],
        finalize_func: Callable[[BaseRun, bool], None]
) -> None:
    """
    Generic callback function for handling job completion.

    :param run: The job object (CalibrationRun, ValidationRun, or ForecastRun) representing the job.
    :param status: The job's completion status. This can be:
        - A `Future` object (for Local environments)
        - A `SlurmStatusEnum` value (for Parallel Works environments)
    :param check_if_failed: Function to check job status based on the environment.
    :param finalize_func: Function to execute finalization logic specific to the job type.
    """
    # TODO Clean up some of the handlers so that we handle the exceptions here instead of the individual handlers
    job_description = get_job_description(run)
    try:
        logger.info(f"Job end callback received for {job_description} with status{status}")

        run.run_end = datetime.now(timezone.utc)
        run.save(update_fields=["run_end"])

        failed_so_far = check_if_failed(run, status)

        # Execute finalization logic
        finalize_func(run, failed_so_far)

    except Exception:
        logger.exception(f"Exception occurred during job end callback for {job_description}")
        try:
            set_job_status(run, StatusEnum.FAILED)
        except Exception:
            logger.exception(f"Failed to set FAILED status for {job_description}")


def finalize_calibration_after_callback(run: CalibrationRun, failed_so_far: bool) -> None:
    """
    Finalizes a calibration job after it has completed.

    :param run: The CalibrationRun object representing the job.
    - Reads the output data generated by the calibration job and processes it.
    - Marks the calibration job as DONE in the database, indicating successful completion.
    - Creates and submits a validation control job to verify the calibration's results.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    job_description = get_job_description(run)

    try:
        # Process the calibration output
        read_calibration_output(run, failed_so_far)  # Process and store the output of the calibration job.
        if not failed_so_far:
            set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.
    except Exception as e:
        # Catch the exception and mark the job as FAILED
        logger.exception(f"Error processing calibration output for {job_description}: {str(e)}")
        set_job_status(run, StatusEnum.FAILED)
        return  # Stop further processing if the job failed

    if failed_so_far:
        return
    # If processing succeeded, continue with the next step
    try:
        create_and_submit_validation_control(run)  # Trigger the creation of validation jobs.
    except Exception as e:
        logger.exception(f"Error creating and submitting validation control run for {job_description}: {str(e)}")
        set_job_status(run, StatusEnum.FAILED)


def finalize_validation_after_callback(run: ValidationRun, failed_so_far: bool) -> None:
    """
    Finalizes a validation job after it has completed.

    :param run: The ValidationRun object representing the validation job.
    - Processes the output of the validation job and evaluates its results.
    - If applicable, creates a 'VALID_BEST' validation run.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    process_validation_output_and_maybe_create_best(run, failed_so_far)  # Process the validation results and handle best-run logic.


def finalize_forecast_forcing_download_after_callback(run: ForecastForcingDownloadRun, failed_so_far: bool) -> None:
    """
    Finalizes a forecast forcing download job after it has completed.

    :param run: The ForecastForcingDownloadRun object representing the job.
    - Processes the output of the forecast forcing download.
    - Marks the forecast forcing download job as DONE in the database.
    - Submits the associated forecast job.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_forecast_output(run, failed_so_far)
    if not failed_so_far:
        set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.
        # submit the forecast job with the forcing data
        submit_job(run.forecast_run)


def finalize_forecast_after_callback(run: ForecastRun, failed_so_far: bool) -> None:
    """
    Finalizes a forecast job after it has completed.

    :param run: The ForecastRun object representing the forecast job.
    - Processes the output of the forecast job.
    - Marks the forecast job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_forecast_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def finalize_verification_after_callback(run: VerificationRun, failed_so_far: bool) -> None:
    """
    Finalizes a verification job after it has completed.

    :param run: The VerificationRun object representing the forecast job.
    - Processes the output of the verification job.
    - Marks the verification job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_verification_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def final_preprocessing_for_calibration(run: CalibrationRun) -> list[str]:
    """
    Executes the long-running preparation steps for the given CalibrationRun.
    Assumes that all prerequisites (paths, date ranges) have been validated.

    :param run: The CalibrationRun to process.
    :return: List of validation error messages.
    """
    errors: list[str] = []

    # Subset forcing data
    if run.forcing_source_requested != ForcingSourceEnum.UPLOAD.db_instance:
        subset_directory_by_time_range(
            run,
            run.forcing_eds_dir_path,
            get_forcing_dir_for_job(run),
            DateTimeRange(
                min(run.calibration_start_period, run.validation_start_period),
                max(run.calibration_end_period, run.validation_end_period)
            )
        )

    # Subset observational data
    if run.observational_source != ObservationalSourceEnum.UPLOAD.db_instance:
        subset_by_time_range(
            run,
            run.observational_eds_file_path,
            get_observational_file_for_job(run),
            DateTimeRange(
                min(run.calibration_start_period, run.validation_start_period),
                max(run.calibration_end_period, run.validation_end_period)
            )
        )

    return errors


def subset_directory_by_time_range(
        run: CalibrationRun,
        input_directory: str,
        output_directory: str,
        date_time_range: DateTimeRange,
        max_workers: int = 4
) -> None:
    """
    Subsets the files in a directory based on a provided time range and saves the filtered
    files into an output directory. Uses parallel processing to handle multiple files at once.

    :param run: The CalibrationRun instance (used for consistent logging context).
    :param input_directory: Path to the input directory.
    :param output_directory: Path to the output directory.
    :param date_time_range: DateTimeRange object specifying the time range for filtering.
    :param max_workers: Maximum number of parallel workers (default is 4 to balance S3FS I/O and system resources).
    - S3FS benefits from parallel reads, but excessive threads can cause API throttling or network congestion.
    - 4 workers provide a good balance between concurrency and avoiding excessive I/O wait.
    - If running on a high-performance instance (e.g., AWS EC2 with high network bandwidth), this value can be increased.
    - If running on a slow or metered connection, keeping this at 4 prevents potential slowdowns.
    """
    start_time = time.time()
    logger.info(f'Starting subsetting for directory {input_directory} with max_workers={max_workers} for Calibration Job {run.id}')

    if not input_directory or not os.path.isdir(input_directory):
        raise ValueError(f"Input path '{input_directory}' is not a directory for Calibration Job {run.id}")

    os.makedirs(output_directory, exist_ok=True)

    files_to_process = [
        (os.path.join(input_directory, filename), os.path.join(output_directory, filename))
        for filename in os.listdir(input_directory)
        if os.path.isfile(os.path.join(input_directory, filename))
    ]

    logger.info(f"Found {len(files_to_process)} files to process in {input_directory} for Calibration Job {run.id}")

    def process_file(input_output_tuple: tuple[str, str]) -> None:
        """
        Processes a single file by applying time-based subsetting.

        :param input_output_tuple: Tuple containing the full path to the input and output files.
        """
        input_file, output_file = input_output_tuple
        # Delegate to the time range subsetting logic
        subset_by_time_range(run, input_file, output_file, date_time_range)

    # Use ThreadPoolExecutor for I/O-bound tasks (like S3FS-based file reads/writes)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(process_file, files_to_process)

    elapsed_time = time.time() - start_time
    logger.info(f"Finished subsetting directory {input_directory} in {elapsed_time:.2f} seconds for Calibration Job {run.id}")


def subset_by_time_range(
        run: CalibrationRun,
        input_file: str, output_file: str,
        date_time_range: DateTimeRange
) -> None:
    """
    Reads a CSV file, filters rows based on a time range, and writes the filtered data
    to an output file with the original column names and timezone-naive datetime values.

    Optimized to take advantage of sorted data for faster processing.
    Chunksize is optimized for **performance**, reducing disk I/O overhead.

    :param run: The CalibrationRun instance (used for logging context only).
    :param input_file: Path to the input CSV file.
    :param output_file: Path to the output CSV file.
    :param date_time_range: DateTimeRange object specifying the time range for filtering.
    """
    logger.info(f'Subsetting file {input_file} to {output_file} with date range {date_time_range} for Calibration Job {run.id}')
    file_basename = os.path.basename(input_file)  # Extract just the filename

    # Dynamically determine the best chunksize for performance
    chunk_size = get_performance_chunksize(input_file)
    logger.info(f"Using optimized chunksize={chunk_size} for {file_basename} for Calibration Job {run.id}")

    # Ensure the output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    start_line = 1  # Track the first row of each chunk (excluding header)

    # DateTimeRange arguments are already in UTC, so use them as-is
    start_datetime = pd.Timestamp(date_time_range.start_datetime)
    end_datetime = pd.Timestamp(date_time_range.end_datetime)

    with open(output_file, 'w') as out_file:
        write_header = True  # Ensure the header is written only once

        # Read the first chunk to detect the datetime column name
        first_chunk = pd.read_csv(input_file, delimiter=',', parse_dates=[0], chunksize=chunk_size)
        for chunk in first_chunk:
            original_time_column = chunk.columns[0]  # Get the first column name dynamically
            break  # Exit after getting the column name

        # Read the CSV in chunks, parsing dates in the detected first column
        for chunk in pd.read_csv(input_file, delimiter=',', parse_dates=[0], chunksize=chunk_size):
            end_line = start_line + len(chunk) - 1  # Compute last row index for this chunk

            # Rename the first column to a consistent name
            if original_time_column in chunk.columns:
                chunk.rename(columns={original_time_column: 'dateTime'}, inplace=True)
            else:
                logger.error(f"Expected datetime column '{original_time_column}' not found in {file_basename} for Calibration Job {run.id}")
                raise KeyError(f"Expected datetime column '{original_time_column}' not found in {file_basename} for Calibration Job {run.id}")

            # Convert to datetime and explicitly assume timestamps are in UTC
            chunk['dateTime'] = pd.to_datetime(chunk['dateTime'], errors='coerce')

            # Validate datetime values before localizing
            if chunk['dateTime'].isna().any():
                logger.error(f"Invalid datetime values found in {file_basename} (lines {start_line}-{end_line}) for Calibration Job {run.id}")
                raise ValueError(f"Invalid datetime values found in {file_basename} (lines {start_line}-{end_line}) for Calibration Job {run.id}")

            # Now localize to UTC
            chunk['dateTime'] = chunk['dateTime'].dt.tz_localize('UTC')

            # Log the original start and end ranges in this chunk, including line numbers
            chunk_start = chunk['dateTime'].min()
            chunk_end = chunk['dateTime'].max()
            logger.debug(
                f'Chunk {file_basename} (lines {start_line}-{end_line}) date range: {chunk_start} - {chunk_end} for Calibration Job {run.id}')

            # Skip chunks that are entirely before the time range
            if chunk_end < start_datetime:
                start_line += chunk_size  # Update row counter
                continue  # No relevant data in this chunk

            # Stop processing early if chunks exceed the time range
            if chunk_start > end_datetime:
                break  # Since files are sorted, no need to read further

            # Filter the data within the time range
            subset_df = chunk.loc[
                (chunk['dateTime'] >= start_datetime) &
                (chunk['dateTime'] <= end_datetime)
                ].copy()  # Explicitly create a copy

            # Convert back to naive timestamps for output (to match original format)
            subset_df['dateTime'] = subset_df['dateTime'].dt.tz_convert(None)

            # Rename datetime column back to its original name
            subset_df.rename(columns={'dateTime': original_time_column}, inplace=True)

            # Write filtered data to output CSV
            subset_df.to_csv(out_file, mode='a', index=False, header=write_header)
            write_header = False  # Ensure subsequent writes do not include headers

            # Update the starting line number for the next chunk
            start_line = end_line + 1

    logger.info(f'Finished subsetting file {input_file} to {output_file} for Calibration Job {run.id}')


def get_performance_chunksize(file_path: str) -> int:
    """
    Dynamically determines an optimal chunksize for high-performance processing
    using a **single row** to estimate memory size since all rows are substantially the same size

    :param file_path: Path to the input CSV file.
    :return: Optimal chunksize for pandas.read_csv()
    """
    file_size = os.path.getsize(file_path)  # Get file size in bytes

    # Read one row (excluding header) to estimate row size
    sample_df = pd.read_csv(file_path, nrows=2)  # Read first two rows (header + 1 row)
    row_size = sample_df.iloc[1:].memory_usage(deep=True).sum()  # Size of first data row (ignore header)

    # Estimate total rows in the file
    estimated_rows = file_size / row_size

    # Adjust chunk fraction based on file size
    if file_size < 50_000_000:  # <50MB
        target_fraction = 0.05  # 5%
    elif file_size < 200_000_000:  # 50MB-200MB
        target_fraction = 0.03  # 3%
    else:
        target_fraction = 0.01  # 1% (limit memory impact for huge files)

    # Set chunksize as 5-10% of total estimated rows
    optimal_chunksize = int(estimated_rows * target_fraction)

    # Ensure reasonable limits (between 10k - 100k)
    return max(10_000, min(optimal_chunksize, 100_000))
