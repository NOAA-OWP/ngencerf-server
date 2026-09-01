"""
Job lifecycle management for Django.

Responsibilities:

- Job preprocessing and preparation
- Job submission entry point
- Persisting run state transitions
- Lifecycle event handling
- Terminal callback processing
- Post-processing output files
- Creating follow-on jobs
- Finalization and cleanup

Execution backend details are intentionally abstracted away:

- Docker
    - Development execution path

- Slurm
    - AWS/HPC execution path

Lifecycle behavior should remain identical regardless of execution backend.

This module answers:

    "What should happen to the run?"

This module does not:

- Build runtime commands
- Select execution backends
- Execute Docker or Slurm jobs directly
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, cast
from typing import TypeVar
from urllib.parse import urlparse

import fsspec
import pandas as pd
from datetimerange import DateTimeRange
from django.contrib.auth import get_user_model
from django.db import transaction, close_old_connections
from mswm.manager import build_fcst, build_calib
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, SlurmCallbackStatusEnum
from calibration.enums_vanilla import JobExecutionMode
from calibration.models import CalibrationRun, ValidationRun, Iteration, ForecastRun, ColdStartRun, VerificationRun
from calibration.models.base_run import BaseRun
from calibration.models.hindcast_run import HindcastRun
from calibration.run_util.job_submission import cancel_job_request
from calibration.util.git_util import get_git_info_internal
from calibration.util.ngen_locations import get_calibration_input_file, get_validation_best_stdout_file, get_validation_control_stdout_file, \
    get_calibration_stdout_file, get_validation_best_input_file, get_validation_control_input_file, get_validation_iteration_stdout_file, \
    get_forecast_stdout_file, get_forecast_dir, get_validation_iteration_git_info_file, get_validation_special_git_info_file, \
    get_calibration_git_info_file, get_forecast_git_info_file, get_verification_git_info_file, get_forecast_realization_file, \
    get_cold_start_realization_file, \
    get_cold_start_stdout_file, get_cold_start_dir, \
    get_cold_start_git_info_file, get_hindcast_stdout_file, get_hindcast_git_info_file, get_hindcast_dir, get_cold_start_state, \
    get_verification_stdout_file
from calibration.views import ngen_cal_input
from calibration.views.common import ResponseError, CerfException, create_validation_run_internal, get_job_description, write_ngen_logging_file
from calibration.views.end_of_job_processing import read_validation_output, read_calibration_output, read_forecast_output, \
    read_cold_start_output, read_verification_output, read_hindcast_output
from calibration.views.forecast_input import create_forecast_input
from calibration.views.ngen_cal_input import ready_to_run
from calibration.views.verification_input import create_verification_input
from cerfServer.settings import JOB_EXECUTION_MODE

logger = logging.getLogger(__name__)

User = get_user_model()

# Terminal Slurm processing runs in this Django process after the callback
# request returns. This allows the Slurm batch script to exit before Django
# retrieves finalized accounting data from SlurmDB.
#
# The run is not marked DONE until finalization and end-of-job processing
# complete successfully. If the Django server restarts during this work, the
# run remains nonterminal and performance or other output data may be incomplete.
_JOB_FINALIZATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="job-finalization",
)


def set_job_status(run: BaseRun, status: StatusEnum | None, failure_messages: dict | None = None) -> None:
    """
    Update the persisted run status and optional failure messages.

    :param run: The CalibrationRun, ValidationRun, ForecastRun or HindcastRun object.
    :param status: The new status to set. If None, status is left unchanged.
    :param failure_messages: Optional failure details to record.
    """
    update_fields: list[str] = []

    if status:
        run.status = status.db_instance
        update_fields.append("status")

    if failure_messages:
        run.failure_messages = json.dumps(failure_messages)
        update_fields.append("failure_messages")

    if update_fields:
        run.save(update_fields=update_fields)


def validate_cmd_args(
        cmd_line_args: dict[str, str],
        stdout_file: str
) -> None:
    """
    Validate job execution payload values.

    Payload values must already be normalized to strings before they are passed
    to the configured execution backend.

    :param cmd_line_args: Command-line arguments keyed by argument name.
    :param stdout_file: Path to the file where the job's stdout will be written.
    :raises TypeError: If any argument or stdout_file is not a string.
    :raises ValueError: If any argument value or stdout_file is None.
    """

    # Validate each command-line argument value.
    for key, value in cmd_line_args.items():
        if value is None:
            logger.error(f"Argument '{key}' is None, which is not allowed.")
            raise ValueError(f"Command-line argument '{key}' cannot be None.")

        # Execution payload values must already be normalized...
        if not isinstance(value, str):
            value_type = type(value).__name__

            logger.error(
                f"Invalid argument for '{key}': {value} (type: {value_type}). "
                "The value must be a string before submitting to the execution backend."
            )

            raise TypeError(
                f"Invalid argument for '{key}': {value} (type: {value_type}). "
                "The value must be a string before submitting to the execution backend."
            )

    if stdout_file is None:
        logger.error("stdout_file is None, which is not allowed.")
        raise ValueError("stdout_file cannot be None.")

    # stdout_file must also be normalized to a string path.
    if not isinstance(stdout_file, str):
        stdout_file_type = type(stdout_file).__name__

        logger.error(
            f"Invalid stdout_file: {stdout_file} (type: {stdout_file_type}). "
            "The stdout_file value must be a string path."
        )

        raise TypeError(
            f"Invalid stdout_file: {stdout_file} (type: {stdout_file_type}). "
            "The stdout_file value must be a string path."
        )


def queue_job(run: BaseRun, cmd_line_args: dict[str, str], stdout_file: str) -> None:
    """
    Submit a job through the configured execution backend.

    The execution backend may be:

    - Docker
    - Slurm

    :param run: The BaseRun object (CalibrationRun, ValidationRun, etc.).
    :param cmd_line_args: A dictionary of command-line arguments for the job.
    :param stdout_file: The path to the file where the job's stdout will be written.
    """
    from calibration.run_util.job_submission import submit_job_request

    validate_cmd_args(cmd_line_args, stdout_file)

    submit_job_request(
        run,
        cmd_line_args,
        stdout_file,
    )

    run.sent_date = datetime.now(timezone.utc)
    run.save(update_fields=["sent_date"])


def cancel_job_common(run: BaseRun) -> bool:
    """
    Cancel a job through the configured execution backend.

    For Docker, cancellation targets the running container.
    For Slurm, cancellation requires the persisted slurm_job_id.

    :param run: The run object to cancel.
    :return: True if the cancellation request succeeded.
    """
    return cancel_job_request(run)


def run_calibration_job(calibration_run: CalibrationRun) -> None:
    """
    Queue a calibration job after resolving its input and output files.

    :param calibration_run: The CalibrationRun object representing the job.
    :raises CerfException: If the input file does not exist.
    """
    input_file = get_calibration_input_file(calibration_run)
    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Calibration Job {calibration_run.id}, "
            f"user: {calibration_run.owner.username}"
        )

    stdout_file = get_calibration_stdout_file(calibration_run)

    queue_job(
        calibration_run,
        {
            "input_file": input_file,
            "nprocs": str(calibration_run.mpi_nprocs),
        },
        stdout_file,
    )


def run_validation_job(validation_run: ValidationRun) -> None:
    """
    Queue a validation job after resolving its input and output files.

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
        stdout_file = get_validation_iteration_stdout_file(
            validation_run.calibration_run,
            validation_run.worker_name,
            validation_run.iteration_num
        )

    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Validation Job {validation_run.id}, "
            f"user: {validation_run.calibration_run.owner.username}, type: {validation_run.validation_type}"
        )

    cmd_line_args = {
        "input_file": input_file,
        "nprocs": str(validation_run.calibration_run.mpi_nprocs),
    }

    if validation_run.validation_type == ValidationType.VALID_ITERATION.value:
        # Validation iteration jobs include worker and iteration information.
        cmd_line_args["worker_name"] = validation_run.worker_name
        cmd_line_args["iteration_num"] = str(validation_run.iteration_num)

    queue_job(validation_run, cmd_line_args, stdout_file)


def run_cold_start_job(cold_start_run: ColdStartRun) -> None:
    """
    Queue a cold start job after resolving its input and output files.

    :param cold_start_run: The ColdStartRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(cold_start_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(cold_start_run)}"
        )

    realization_file = get_cold_start_realization_file(cold_start_run)
    stdout_file = get_cold_start_stdout_file(cold_start_run)

    queue_job(
        cold_start_run,
        {
            "validation_yaml": validation_yaml,
            "realization_file": realization_file,
        },
        stdout_file,
    )


def run_forecast_job(forecast_run: ForecastRun) -> None:
    """
    Queue a forecast job after resolving its input and output files.

    :param forecast_run: The ForecastRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(forecast_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(forecast_run)}"
        )

    realization_file = get_forecast_realization_file(forecast_run)
    stdout_file = get_forecast_stdout_file(forecast_run)

    queue_job(
        forecast_run,
        {
            "validation_yaml": validation_yaml,
            "realization_file": realization_file,
        },
        stdout_file,
    )


def run_hindcast_job(hindcast_run: HindcastRun) -> None:
    """
    Queue a hindcast job after resolving its input and output files.

    :param hindcast_run: The HindcastRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(hindcast_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(hindcast_run)}"
        )

    stdout_file = get_hindcast_stdout_file(hindcast_run)

    cold_start_state = get_cold_start_state(hindcast_run.cold_start_run)
    if not os.path.exists(cold_start_state):
        # Issue an explicit error for legacy Cold Start runs that might not have a saved state
        raise RuntimeError(f'Saved state not found for cold start run {get_job_description(hindcast_run)}')

    queue_job(
        hindcast_run,
        {
            "validation_yaml": validation_yaml,
            "config_file": create_forecast_input(hindcast_run),
            "run_name": os.path.basename(get_hindcast_dir(hindcast_run)),
            "interval_cycle": str(hindcast_run.interval_cycle),
            "num_iterations": str(hindcast_run.num_iterations),
            "use_state": cold_start_state,
        },
        stdout_file,
    )


def run_verification_job(verification_run: VerificationRun) -> None:
    """
    Start a verification job by determining input and output file paths.

    This function is called internally by `launch_job`
    and should not be called directly.

    :param verification_run: The VerificationRun object representing the job.
    """
    stdout_file = get_verification_stdout_file(verification_run)

    queue_job(
        verification_run,
        {
            "verification_config": create_verification_input(verification_run),
        },
        stdout_file,
    )


def launch_job(run: BaseRun, logging_config=None) -> Response | None:
    """
    Prepare the job, mark it SUBMITTED, and submit it to the configured backend.

    - Sets the submission timestamp and updates the job status to SUBMITTED.
    - Performs preprocessing for run types that require it.
    - Writes git info before execution.
    - Dispatches execution to Docker or Slurm.
    - If submission fails, marks the run as FAILED and records the error.

    :param run: Run object to submit.
    :param logging_config: Optional logging configuration for calibration-related jobs.
    :return: None if successful; a DRF Response if preprocessing returns an error response.
    :raises CerfException: If the run type is unsupported or job execution setup fails.
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
        run.save(update_fields=["submit_date", "status"])

    try:
        # Do pre-processing for certain jobs
        if isinstance(run, CalibrationRun):
            write_ngen_logging_file(run, logging_config)
            fatal, response = prepare_calibration_job(run)
            if response:
                if fatal:
                    failure_message = {
                        "validation_errors": response.data.get("validation_errors"),
                        "errors": response.data.get("errors"),
                    }

                    run.status = StatusEnum.FAILED.db_instance
                    run.failure_messages = json.dumps(failure_message)
                    run.save(update_fields=["status", "failure_messages"])
                return response

        elif isinstance(run, (ColdStartRun, ForecastRun)):
            write_ngen_logging_file(run, logging_config)
            _, _ = prepare_fcst_or_cold_start_job(run)

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
        elif isinstance(run, ColdStartRun):
            create_git_info(get_cold_start_git_info_file(run))

            run_cold_start_job(run)
        elif isinstance(run, ForecastRun):
            create_git_info(get_forecast_git_info_file(run))

            run_forecast_job(run)
        elif isinstance(run, HindcastRun):
            create_git_info(get_hindcast_git_info_file(run))

            run_hindcast_job(run)
        elif isinstance(run, VerificationRun):
            create_git_info(get_verification_git_info_file(run))

            run_verification_job(run)
        else:
            raise CerfException(f"Unsupported run type: {type(run).__name__}")

    except Exception as e:
        # Handle failures by marking the job as FAILED
        run.status = StatusEnum.FAILED.db_instance
        msg = f"Exception submitting {get_job_description(run)} - {str(e)}"
        logger.exception(msg)
        run.failure_messages = json.dumps({"message": msg})
        run.save(update_fields=["status", "failure_messages"])
        raise  # Re-raise the exception

    logger.info(f"{get_job_description(run)} successfully submitted.")
    return None


def create_git_info(git_info_file: str) -> None:
    logger.info(f"Writing git info to {git_info_file}")
    git_info_data = get_git_info_internal()
    os.makedirs(os.path.dirname(git_info_file), exist_ok=True)
    with open(git_info_file, "w") as f:
        f.write(json.dumps(git_info_data, indent=4))


def prepare_calibration_job(calibration_run: CalibrationRun) -> tuple[bool, Response | None]:
    """
    Prepare a CalibrationRun job by validating inputs, preprocessing data, and generating configuration files.

    This function performs:
    - Readiness validation using `ready_to_run`
    - Forcing/observational data subsetting
    - Input file generation using `create_input`

    This is only used internally by `launch_job` for CalibrationRun.

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
    assert config_file is not None

    job_description = get_job_description(calibration_run)
    try:
        logger.info(f'Final preparation to run Calibration Job {calibration_run.id}')

        logger.info(f'Running build_calib for {job_description} with config {config_file}')
        build_calib(config_file)
    except Exception as e:
        CalibrationRun.objects.filter(id=calibration_run.id).update(status=StatusEnum.FAILED.db_instance)
        msg = f'Exception during build_calib for {job_description} - {str(e)}'
        logger.exception(msg)
        raise CerfException(msg) from e

    logger.info(f'Return from build_calib for {job_description}')
    return False, None


def prepare_fcst_or_cold_start_job(run: ColdStartRun | ForecastRun | HindcastRun) -> tuple[bool, Response | None]:
    """
    Prepare a ColdStartRun, ForecastRun or Hindcast job by generating configuration files.

    This function:
    - Calls create_forecast_input(run) to generate the config
    - Uses get_validation_best_input_file() for the calibration baseline
    - Runs build_fcst() with use_cold_start=True if run is a ColdStartRun

    :param run: A ForecastRun or ColdStartRun instance.
    :return: (fatal_error: bool, Response) — If preparation is successful, returns (False, None).
             If errors occur, returns (True, error response) or (False, warning response).
    """
    job_description = get_job_description(run)

    try:
        config_file = create_forecast_input(run)
        valid_best = get_validation_best_input_file(run.calibration_run)

        if isinstance(run, ColdStartRun):
            run_name = os.path.basename(get_cold_start_dir(run))
            use_cold_start = True
            save_state = True
            saved_state = None
        else:  # ForecastRun
            run_name = os.path.basename(get_forecast_dir(cast(ForecastRun, run)))
            use_cold_start = False
            save_state = False
            saved_state = get_cold_start_state(run.cold_start_run) if run.cold_start_run else None

        logger.info(f'Running build_fcst for {job_description} '
                    f'with config: {config_file}, valid_best: {valid_best}, run_name: {run_name}, save_state: {save_state}, load_state_from: {saved_state}')

        build_fcst(config_file, valid_best, run_name, use_cold_start=use_cold_start, save_state=save_state, load_state_from=saved_state)
    except Exception as e:
        # Mark the run as failed
        run.__class__.objects.filter(id=run.id).update(status=StatusEnum.FAILED.db_instance)
        msg = f'Exception during build_fcst for {job_description} - {str(e)}'
        logger.exception(msg)
        raise CerfException(msg) from e

    logger.info(f'Return from build_fcst for {job_description}')
    return False, None


def create_and_submit_validation_control(calibration_run: CalibrationRun) -> None:
    """
    Create a validation run of type VALID_CONTROL and submit it.

    :param calibration_run: The CalibrationRun object for which the validation control run is created.
    """
    validation_run = create_validation_run_internal(calibration_run, None, validation_type=ValidationType.VALID_CONTROL)
    launch_job(validation_run)


def process_validation_output_and_maybe_create_best(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Process validation output and, if this was a VALID_CONTROL run,
    create and submit a follow-up VALID_BEST run after the current
    DB transaction commits.

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
        msg = f"Error processing validation output for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(validation_run, StatusEnum.FAILED, failure_messages)
        return  # Stop further processing if the job failed

    if failed_so_far:
        return

    # Only VALID_CONTROL can trigger a follow-up VALID_BEST run
    if validation_run.validation_type != ValidationType.VALID_CONTROL.value:
        return

    # We just ran Validation Control, so need to run Validation Best
    # Create the VALID_BEST run now, but don't attach the iteration yet.
    best_validation_run = create_validation_run_internal(
        validation_run.calibration_run, None, validation_type=ValidationType.VALID_BEST
    )

    # Run this AFTER the surrounding transaction commits,
    # so we see the final 'best_params' state (not the intermediate writes).
    def _finish():
        with transaction.atomic():
            iteration = (
                Iteration.objects
                .filter(calibration_run=validation_run.calibration_run, best_params=True)
                .get()
            )
            # Set the iteration containing the best values before we run it
            best_validation_run.iteration = iteration
            best_validation_run.save(update_fields=['iteration'])
            launch_job(best_validation_run)

    transaction.on_commit(_finish)


T = TypeVar("T", bound=BaseRun)


def run_generic_job_end_callback(
        run: T,
        status: SlurmCallbackStatusEnum,
        check_if_failed: Callable[[T, SlurmCallbackStatusEnum], bool],
        finalize_func: Callable[[T, bool], None],
) -> None:
    """
    Generic callback function for handling job completion.

    :param run: The job object (CalibrationRun, ValidationRun, ForecastRun,
        HindcastRun, ColdStartRun, or VerificationRun) representing the job.
    :param status: The job completion status reported by the execution backend.
        This can be:
        - A `SlurmCallbackStatusEnum` value (for Slurm callbacks)
        - A mapped `SlurmCallbackStatusEnum` value from Docker lifecycle events
    :param check_if_failed: Function that evaluates the terminal callback
        status and determines whether the job should be considered failed
        or cancelled.
    :param finalize_func: Function to execute run-type-specific finalization
        logic after status handling has completed.
    """
    job_description = get_job_description(run)

    try:
        logger.info(
            f"Job end callback received for {job_description} "
            f"with status {status}"
        )

        run.run_end = datetime.now(timezone.utc)
        run.save(update_fields=["run_end"])

        failed_so_far = check_if_failed(run, status)

        # Execute job-specific finalization after status handling.
        finalize_func(run, failed_so_far)

    except Exception as e:
        msg = f"Exception occurred during job end callback for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {"message": msg}

        try:
            set_job_status(run, StatusEnum.FAILED, failure_messages)
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
    - True if the job encountered a failure or was cancelled.
    - False if the job has completed successfully so far.
    """
    job_description = get_job_description(run)

    try:
        if failed_so_far:
            # Must have been a cal-mgr/ngen failure
            if run.status == StatusEnum.CANCELLED.db_instance:
                failure_messages = {
                    "message": "Calibration job was cancelled by the user before completion."
                }
                set_job_status(run, None, failure_messages)
            else:
                failure_messages = {
                    "message": "The ngen or cal-mgr job failed. See logs for further details."
                }
                set_job_status(run, None, failure_messages)

        # Process the calibration output regardless of failure/cancel
        read_calibration_output(run, failed_so_far)  # Process and store the output of the calibration job.
        if not failed_so_far:
            set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.

    except Exception as e:
        # Catch the exception and mark the job as FAILED
        msg = f"Error processing calibration output for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(run, StatusEnum.FAILED, failure_messages)
        return  # Stop further processing if the job failed

    if failed_so_far:
        # Don’t continue to validation jobs if calibration was failed/cancelled
        return
    # If processing succeeded, continue with the next step
    try:
        create_and_submit_validation_control(run)  # Trigger the creation of validation jobs.
    except Exception as e:
        msg = f"Error creating and submitting validation control run for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(run, StatusEnum.FAILED, failure_messages)


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


def finalize_cold_start_after_callback(run: ColdStartRun, failed_so_far: bool) -> None:
    """
    Finalizes a cold start job after it has completed.

    :param run: The ColdStartRun object representing the cold start job.
    - Processes the output of the Cold Start job.
    - Marks the cold start job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_cold_start_output(run, failed_so_far)
    if failed_so_far:
        return

    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.

    # A new cold start may have at most one dependent run associated with it:
    # either one ForecastRun, one HindcastRun, or neither.
    forecast_run = ForecastRun.objects.filter(cold_start_run=run).first()
    hindcast_run = HindcastRun.objects.filter(cold_start_run=run).first()

    if forecast_run and hindcast_run:
        raise ValueError(
            f"ColdStartRun {run.pk} has both a ForecastRun and HindcastRun dependent on it."
        )

    dependent_run = forecast_run or hindcast_run
    if dependent_run:
        launch_job(dependent_run)


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


def finalize_hindcast_after_callback(run: HindcastRun, failed_so_far: bool) -> None:
    """
    Finalizes a hindcast job after it has completed.

    :param run: The HindcastRun object representing the hindcast job.
    - Processes the output of the hindcast job.
    - Marks the hindcast job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_hindcast_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def finalize_verification_after_callback(run: VerificationRun, failed_so_far: bool) -> None:
    """
    Finalizes a verification job after it has completed.

    :param run: The VerificationRun object representing the verification job.
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


def _get_fs_and_scheme(path_or_url: str):
    """Return (fs, scheme) for local or remote directory."""
    parsed = urlparse(path_or_url)
    scheme = parsed.scheme or "file"
    fs = fsspec.filesystem(scheme)
    return fs, scheme


def _list_dir_files(path_or_url: str) -> list[str]:
    """
    Return a list of full paths (local or remote URLs) for regular files in a directory/prefix.
    - Local: uses os.listdir / os.path.isfile
    - Remote: uses fsspec.ls(detail=True) and filters for files.
      Ensures each returned item is a fully-qualified URL (e.g., s3://bucket/key),
      because some backends (notably s3fs) return names like 'bucket/key' without a scheme.
    """
    parsed = urlparse(path_or_url)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        base = parsed.path or path_or_url
        return [
            os.path.join(base, name)
            for name in os.listdir(base)
            if os.path.isfile(os.path.join(base, name))
        ]

    fs = fsspec.filesystem(scheme)
    entries = fs.ls(path_or_url, detail=True)
    out: list[str] = []
    for e in entries:
        if e.get("type") != "file":
            continue
        name = e.get("name") or ""
        # If the backend returned a scheme-less "bucket/key", add the scheme.
        if not urlparse(name).scheme:
            name = f"{scheme}://{name}"
        out.append(name)
    return out


def _detect_first_column_name(fs: fsspec.AbstractFileSystem, url_or_path: str) -> str:
    """Open the CSV and read only the header to discover the first column name."""
    # text mode is fine; pandas reads just the header with nrows=0
    with fs.open(url_or_path, "rt") as fh:
        header_df = pd.read_csv(fh, delimiter=",", nrows=0)
    if header_df.columns.empty:
        raise ValueError(f"No columns found in {url_or_path}")
    return str(header_df.columns[0])


def subset_directory_by_time_range(
        run: CalibrationRun,
        input_directory: str,
        output_directory: str,
        date_time_range: DateTimeRange,
        max_workers: int = 4
) -> None:
    """
    Subsets the files in a directory/prefix based on a provided time range and saves
    the filtered files into an output directory, processing files in parallel.

    - Works with local dirs and S3 prefixes (s3://bucket/prefix).
    - Streams each input file directly from S3; does not download all upfront.

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
    start_time = time.perf_counter()
    os.makedirs(output_directory, exist_ok=True)

    files_in = _list_dir_files(input_directory)
    file_pairs = [
        (src, os.path.join(output_directory, os.path.basename(urlparse(src).path)))
        for src in files_in
    ]

    logger.info(f"Starting subsetting for {len(file_pairs)} files in {input_directory} "
                f"with max_workers={max_workers} for Calibration Job {run.id}")

    def _process(one: tuple[str, str]) -> None:
        src, dst = one
        subset_by_time_range(run, src, dst, date_time_range)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_process, file_pairs))

    elapsed = time.perf_counter() - start_time
    logger.info(f"Finished subsetting directory {input_directory} in {elapsed:.2f}s "
                f"for Calibration Job {run.id}")


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

    - Supports local files and S3 URLs.
    - Opens remote files directly via fsspec (streams line-by-line, no staging to disk).
    - Assumes the first column is the datetime column.
    - Converts all datetimes to UTC for filtering, then writes them back as naive timestamps
      to match the original format.
    - Stops reading early once the file is past the requested time range (since input is sorted).

    :param run: The CalibrationRun instance (used for logging context only).
    :param input_file: Path or URL to the input CSV file.
    :param output_file: Path to the output CSV file.
    :param date_time_range: DateTimeRange object specifying the time range for filtering.
    """
    file_basename = os.path.basename(urlparse(input_file).path)

    logger.info(f"Subsetting file {input_file} -> {output_file} with range "
                f"{date_time_range} for Calibration Job {run.id}")

    # Dynamically determine the best chunksize for performance
    chunk_size = get_performance_chunksize(input_file)
    logger.info(f"Using optimized chunksize={chunk_size} for {file_basename} for Calibration Job {run.id}")

    # Ensure the output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Convert DateTimeRange boundaries to UTC Timestamps
    start = date_time_range.start_datetime
    end = date_time_range.end_datetime
    assert start is not None and end is not None

    start_dt = pd.to_datetime(start, utc=True)
    end_dt = pd.to_datetime(end, utc=True)

    fs_in, _scheme = _get_fs_and_scheme(input_file)

    # Discover the first column name by reading just the header
    first_col = _detect_first_column_name(fs_in, input_file)

    # Now stream the file in chunks and filter
    with fs_in.open(input_file, "rt") as in_fh, open(output_file, "w") as out_fh:
        write_header = True
        # Iterator over chunks; parse the first column as dates
        reader = pd.read_csv(
            in_fh,
            delimiter=",",
            parse_dates=[0],
            chunksize=chunk_size
        )

        current_line_start = 1
        for chunk in reader:
            current_line_end = current_line_start + len(chunk) - 1

            # Normalize datetime column name
            if first_col not in chunk.columns:
                logger.error(f"Expected datetime column '{first_col}' not found in "
                             f"{file_basename} for Calibration Job {run.id}")
                raise KeyError(f"Expected datetime column '{first_col}' not found")

            chunk.rename(columns={first_col: "dateTime"}, inplace=True)

            # Ensure proper datetime dtype
            chunk["dateTime"] = pd.to_datetime(chunk["dateTime"], errors="coerce")
            if chunk["dateTime"].isna().any():
                logger.error(f"Invalid datetime values in lines {current_line_start}-{current_line_end} "
                             f"for {input_file} (Calibration Job {run.id})")
                raise ValueError("Invalid datetime values encountered")

            # Standardize to UTC
            if chunk["dateTime"].dt.tz is None:
                chunk["dateTime"] = chunk["dateTime"].dt.tz_localize("UTC")
            else:
                chunk["dateTime"] = chunk["dateTime"].dt.tz_convert("UTC")

            # Chunk-level range for fast skip/early stop
            cmin, cmax = chunk["dateTime"].min(), chunk["dateTime"].max()
            logger.debug(f"Chunk range {cmin}..{cmax} "
                         f"(lines {current_line_start}-{current_line_end}) for {input_file}")

            if cmax < start_dt:
                # Entire chunk is before the window → skip
                current_line_start += len(chunk)
                continue
            if cmin > end_dt:
                # Entire chunk is after the window → stop early
                break

            # Filter rows inside the requested time window
            keep = chunk[(chunk["dateTime"] >= start_dt) & (chunk["dateTime"] <= end_dt)].copy()
            if keep.empty:
                current_line_start += len(chunk)
                continue

            # Convert back to naive timestamps to match original format
            keep["dateTime"] = keep["dateTime"].dt.tz_convert(None)
            keep.rename(columns={"dateTime": first_col}, inplace=True)

            # Append to output file
            keep.to_csv(out_fh, index=False, header=write_header, mode="a")
            write_header = False

            current_line_start = current_line_end + 1

    logger.info(f"Finished subsetting file {input_file} -> {output_file} for Calibration Job {run.id}")


def get_performance_chunksize(file_path: str) -> int:
    """
    Dynamically determines an optimal chunksize for high-performance processing
    using a **single data row** to estimate row size (rows are uniform).

    Works for local files and remote URLs (e.g., s3://bucket/key) via fsspec.

    :param file_path: Path to the input CSV file.
    :return: Optimal chunksize for pandas.read_csv()
    """
    parsed = urlparse(file_path)
    scheme = parsed.scheme or "file"
    fs = fsspec.filesystem(scheme)

    # File size in bytes
    if scheme == "file":
        total_size = os.path.getsize(parsed.path or file_path)
    else:
        info = fs.info(file_path)
        total_size = int(info.get("size", 0))

    # Read a tiny sample (exactly 1 data row) to approximate row size
    if scheme == "file":
        with open(parsed.path or file_path, "rt") as fh:
            sample_df = pd.read_csv(fh, nrows=1)
    else:
        with fs.open(file_path, "rt") as fh:
            sample_df = pd.read_csv(fh, nrows=1)

    # Size of one data row (ignore header)
    if sample_df.empty:
        # Fallback if file is empty or malformed; keep it conservative
        return 10_000

    # Size of one data row (header excluded already)
    row_size_bytes = sample_df.memory_usage(deep=True).sum()
    if row_size_bytes <= 0:
        return 10_000

    # Estimate total rows and pick a fraction based on file size
    est_rows = max(1, int(total_size / row_size_bytes))

    if total_size < 50_000_000:  # < 50MB
        target_fraction = 0.05  # ~5%
    elif total_size < 200_000_000:  # 50–200MB
        target_fraction = 0.03  # ~3%
    else:
        target_fraction = 0.01  # ~1%

    optimal = int(est_rows * target_fraction)
    return max(10_000, min(optimal, 100_000))


_CALLBACK_MAP: dict[type[BaseRun], Callable[..., None]] = {
    CalibrationRun: finalize_calibration_after_callback,
    ValidationRun: finalize_validation_after_callback,
    ColdStartRun: finalize_cold_start_after_callback,
    ForecastRun: finalize_forecast_after_callback,
    HindcastRun: finalize_hindcast_after_callback,
    VerificationRun: finalize_verification_after_callback,
}


def persist_slurm_job_id(run: BaseRun, slurm_job_id: int) -> None:
    """
    Persist the Slurm job ID for an asynchronously acknowledged submission.

    Rules:
    - If no slurm_job_id is stored yet, save it.
    - If the same slurm_job_id is already stored, treat as idempotent/no-op.
    - If a different slurm_job_id is already stored, raise an error.
    """
    job_description = get_job_description(run)

    if run.slurm_job_id is None:
        run.slurm_job_id = slurm_job_id
        run.save(update_fields=["slurm_job_id"])
        logger.info(
            f"{job_description} submission acknowledged with slurm_job_id={slurm_job_id}"
        )
        return

    if run.slurm_job_id == slurm_job_id:
        logger.info(
            f"{job_description} received duplicate submission acknowledgement "
            f"for slurm_job_id={slurm_job_id}"
        )
        return

    raise ValueError(
        f"{job_description} already has slurm_job_id={run.slurm_job_id}, "
        f"but callback reported slurm_job_id={slurm_job_id}"
    )


def check_slurm_callback_failed(
        run: BaseRun,
        status: SlurmCallbackStatusEnum,
) -> bool:
    """
    Return True if the callback reports a terminal failure/cancel state.
    """
    if status == SlurmCallbackStatusEnum.DONE:
        return False

    if status == SlurmCallbackStatusEnum.CANCELED:
        run.status = StatusEnum.CANCELLED.db_instance
        run.save(update_fields=["status"])
        return True

    if status == SlurmCallbackStatusEnum.FAILED:
        run.status = StatusEnum.FAILED.db_instance
        run.save(update_fields=["status"])
        return True

    raise ValueError(f"Unsupported terminal callback status: {status}")


def run_calibration_job_callback(run: CalibrationRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_calibration_after_callback,
    )


def run_validation_job_callback(run: ValidationRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_validation_after_callback,
    )


def run_cold_start_job_callback(run: ColdStartRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_cold_start_after_callback,
    )


def run_forecast_job_callback(run: ForecastRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_forecast_after_callback,
    )


def run_hindcast_job_callback(run: HindcastRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_hindcast_after_callback,
    )


def run_verification_job_callback(run: VerificationRun, status: SlurmCallbackStatusEnum) -> None:
    run_generic_job_end_callback(
        run,
        status,
        check_slurm_callback_failed,
        finalize_verification_after_callback,
    )


def finalize_job_after_terminal_callback(
        model_class: type[BaseRun],
        run_id: int,
        callback_func: Callable[[BaseRun, SlurmCallbackStatusEnum], None],
        job_status: SlurmCallbackStatusEnum,
) -> None:
    """
    Finalize a terminal Slurm callback outside the callback request thread.

    Returning the callback request allows the Slurm batch script to exit. This
    worker then waits for finalized SlurmDB accounting data and performs the
    normal end-of-job processing. The run is marked DONE only after that
    processing completes successfully.

    This work runs in the current Django process and is not durable. If the
    server restarts during finalization, the run remains nonterminal and some
    performance or output data may be incomplete.

    :param model_class: Concrete run model associated with the callback.
    :param run_id: Database ID of the run to finalize.
    :param callback_func: Existing run-specific terminal callback function.
    :param job_status: Terminal status reported by the execution backend.
    """
    # Do not reuse Django database connections across worker threads.
    close_old_connections()

    try:
        run = (
            model_class.objects
            .select_related("status")
            .get(id=run_id)
        )

        callback_func(run, job_status)

    except Exception:
        logger.exception(
            "Background job finalization failed for model=%s run_id=%s status=%s",
            model_class.__name__,
            run_id,
            job_status,
        )
    finally:
        close_old_connections()


def handle_job_event(
        job_type: str,
        run_id: int,
        job_status: SlurmCallbackStatusEnum,
        slurm_job_id: int | None = None,
) -> None:
    """
    Shared lifecycle handler used by:

    - Slurm callback endpoints
    - Docker executor

    Handles state transitions and prevents duplicate/out-of-order callbacks.
    """

    # Map each runtime job type to its model and terminal callback handler.
    callback_map = {
        "calibration": (CalibrationRun, run_calibration_job_callback),
        "validation": (ValidationRun, run_validation_job_callback),
        "cold_start": (ColdStartRun, run_cold_start_job_callback),
        "forecast": (ForecastRun, run_forecast_job_callback),
        "hindcast": (HindcastRun, run_hindcast_job_callback),
        "verification": (VerificationRun, run_verification_job_callback),
    }

    try:
        model_class, callback_func = callback_map[job_type]
    except KeyError:
        raise ValueError(f"Unsupported job_type: {job_type}")

    # STARTING is valid only from SUBMITTED; terminal callbacks are accepted from
    # SUBMITTED as well as RUNNING to tolerate jobs that finish before STARTING is observed.
    expected_statuses = (
        [StatusEnum.SUBMITTED]
        if job_status == SlurmCallbackStatusEnum.STARTING
        else [StatusEnum.SUBMITTED, StatusEnum.RUNNING]
    )

    expected_db_statuses = [
        s.db_instance
        for s in expected_statuses
    ]

    # Only load runs that are still eligible for this callback transition.
    run = (
        model_class.objects
        .select_related("status")
        .filter(
            id=run_id,
            status__in=expected_db_statuses,
        )
        .first()
    )

    # Duplicate or late callbacks should not re-run finalization.
    if run is None:
        logger.warning(
            "Ignoring callback for job_type=%s run_id=%s status=%s "
            "(run already transitioned)",
            job_type,
            run_id,
            job_status,
        )
        return

    # Persist the scheduler JobID as soon as it is reported.
    if slurm_job_id is not None:
        persist_slurm_job_id(run, slurm_job_id)

    # STARTING only updates the run state and start timestamp.
    if job_status == SlurmCallbackStatusEnum.STARTING:
        logger.info(
            "%s is starting",
            get_job_description(run),
        )

        run.status = StatusEnum.RUNNING.db_instance
        run.run_start = datetime.now(timezone.utc)

        run.save(
            update_fields=[
                "status",
                "run_start",
            ]
        )
        return

    logger.info(
        "%s is ending with status=%s",
        get_job_description(run),
        job_status,
    )

    # In Slurm mode, return the callback request promptly so the batch script can
    # exit and SlurmDB can publish final accounting data.
    if JOB_EXECUTION_MODE == JobExecutionMode.SLURM:
        # The terminal callback is sent from inside the Slurm batch script.
        # Django must return from the request before the script can exit and
        # Slurm can mark the overall job, including its extern step, complete.
        #
        # Finalization therefore runs asynchronously in this Django process:
        #   1. Return the terminal callback response.
        #   2. Allow the Slurm batch script and job to finish.
        #   3. Wait for finalized accounting data to appear in SlurmDB.
        #   4. Create performance metrics and complete end-of-job processing.
        logger.info(
            "%s received terminal Slurm callback; queuing end-of-job processing",
            get_job_description(run),
        )

        # Finalization runs in a worker thread and waits for finalized accounting.
        _JOB_FINALIZATION_EXECUTOR.submit(
            finalize_job_after_terminal_callback,
            model_class,
            run.id,
            callback_func,
            job_status,
        )
    else:
        # Docker and SLURM_MOCK do not need to wait for SlurmDB accounting.
        callback_func(run, job_status)
