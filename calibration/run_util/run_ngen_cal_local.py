import functools
import logging
import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from django.conf import settings

from calibration.enums import StatusEnum, ValidationType
from calibration.enums_vanilla import ScriptEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun, ColdStartRun
from calibration.models.base_run import BaseRun
from calibration.run_util.run_common import set_job_status, job_registry, get_job_registry_key, run_generic_job_end_callback, \
    finalize_calibration_after_callback, finalize_validation_after_callback, finalize_forecast_after_callback, finalize_cold_start_after_callback
from calibration.views.common import get_job_description
from cerfServer.settings import NGEN_CAL_VENV, NGEN_ENVIRONMENT, NgenEnvironmentEnum

logger = logging.getLogger(__name__)

# Create a global thread pool that will be reused across multiple execute() calls
pool: ThreadPoolExecutor = ThreadPoolExecutor()


def run_job_local(run: BaseRun, cmd_line_args: dict[str, str], stdout_file: str, simulate=False) -> None:
    """
    Executes a local job by determining the appropriate script command and callback based on the run type,
    and then running the job.

    This function determines the type of job based on the input `run` object,
    constructs the appropriate command and arguments, and then spawns the job
    process with a callback for completion.

    :param run: The BaseRun object representing the job (CalibrationRun, ValidationRun, etc.).
    :param cmd_line_args: A dictionary of command-line arguments for the job.
    :param stdout_file: The file path where the standard output of the job will be written.
    :param simulate: If True, simulates successful execution without running a real job.
    :raises ValueError: If the `run` type is unsupported or invalid.
    """
    venv = NGEN_CAL_VENV
    # Determine the script command and callback function
    if isinstance(run, CalibrationRun):
        script_cmd = ScriptEnum.CALIBRATION
        callback_function = run_calibration_job_callback_local
    elif isinstance(run, ValidationRun):
        script_cmd = (
            ScriptEnum.VALIDATION_ITERATION if run.validation_type == ValidationType.VALID_ITERATION.value else ScriptEnum.VALIDATION
        )
        callback_function = run_validation_job_callback_local
    elif isinstance(run, ColdStartRun):
        script_cmd = ScriptEnum.COLD_START
        callback_function = run_cold_start_job_callback_local
    elif isinstance(run, ForecastRun):
        script_cmd = ScriptEnum.FORECAST
        callback_function = run_forecast_job_callback_local
    else:
        raise ValueError(f"Unsupported run type: {type(run).__name__} (run id: {getattr(run, 'id', 'N/A')})")

    # Remove nprocs
    cmd_line_args.pop('nprocs', None)

    # Construct the shell script path based on the execution environment
    if NGEN_ENVIRONMENT == NgenEnvironmentEnum.LOCAL:
        spawn_command = [settings.RUNTIME_INFO.get(script_cmd)[1]]
        extra = [stdout_file, venv]
    elif NGEN_ENVIRONMENT == NgenEnvironmentEnum.DOCKER:
        container_name = get_job_registry_key(run)
        # Format the docker run command with the container name
        spawn_command = settings.RUNTIME_INFO.get(script_cmd)[0].format(name=container_name).split()
        extra = [stdout_file]  # Venv not required for Docker
    else:
        spawn_command = []
        extra = []

    args = spawn_command + [script_cmd.value] + list(cmd_line_args.values()) + extra

    # Bind the callback function for job
    job_callback = functools.partial(callback_function, run)

    # Execute the job
    logger.info(f"Executing {script_cmd.value} for {get_job_description(run)} in {NGEN_ENVIRONMENT} environment")
    logger.debug(f"Full command: {args}")
    spawn_job(run, args, callback_function=job_callback, simulate=simulate)


def check_local_for_failure(run: BaseRun, future: Future) -> bool:
    """
    Monitor the status of a locally executed job and update its status in the system.

    This function checks whether a local job completed successfully, failed, or was cancelled.
    It updates the status of the `run` object accordingly.

    :param run: The job object (CalibrationRun, ValidationRun, or ForecastRun) being monitored.
    :param future: The Future object representing the asynchronous process.
    :return: True if the job failed or was cancelled, False otherwise.
    """
    try:
        if future.exception() is not None:
            logger.error(f"Exception occurred in {get_job_description(run)}: {future.exception() or 'Unknown error'}")
            # Only mark FAILED if not already CANCELLED
            if run.status != StatusEnum.CANCELLED.db_instance:
                set_job_status(run, StatusEnum.FAILED)
            else:
                logger.info(f"{get_job_description(run)} already CANCELLED; preserving status despite exception")
            return True

        exit_code = future.result()

        # Treat common cancel exit codes as cancellation:
        # -15 SIGTERM, -9 SIGKILL, 143 = 128+15, 137 = 128+9
        cancelled_codes = (-15, -9, 143, 137)
        if exit_code in cancelled_codes:
            if run.status == StatusEnum.CANCELLED.db_instance:
                logger.info(f"{get_job_description(run)} was already marked CANCELLED (exit {exit_code})")
            else:
                logger.info(f"{get_job_description(run)} was cancelled (exit {exit_code})")
                set_job_status(run, StatusEnum.CANCELLED)
            return True

        if exit_code != 0:
            # Only downgrade to FAILED if not already CANCELLED
            if run.status != StatusEnum.CANCELLED.db_instance:
                logger.error(f"{get_job_description(run)} ending due to abnormal return code {exit_code}")
                set_job_status(run, StatusEnum.FAILED)
            else:
                logger.info(f"{get_job_description(run)} ended with nonzero code {exit_code}, but preserving CANCELLED status")
            return True

        return False

    except Exception as e:
        logger.exception(f"Error in callback for {get_job_description(run)}: {str(e)}")
        if run.status != StatusEnum.CANCELLED.db_instance:
            set_job_status(run, StatusEnum.FAILED)
        else:
            logger.info(f"{get_job_description(run)} already CANCELLED; preserving status despite callback error")
        return True


# Local callbacks
# These callbacks are used to handle job completion events for Calibration, Validation, and Forecast jobs
# executed in a local environment. They wrap the `run_generic_job_callback` function, providing
# environment-specific status checks (`check_local_status`) and job-specific finalization functions.

# Handles the completion of a calibration job in the local environment.
# - Uses `check_local_status` to validate the job's exit code.
# - Executes `finalize_calibration` to read job output, mark the job as DONE, and possibly create validation runs.
run_calibration_job_callback_local = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_local_for_failure, finalize_func=finalize_calibration_after_callback
)

# Handles the completion of a validation job in the local environment.
# - Uses `check_local_status` to validate the job's exit code.
# - Executes `finalize_validation` to process validation results and potentially mark the best validation run.
run_validation_job_callback_local = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_local_for_failure, finalize_func=finalize_validation_after_callback
)

# Handles the completion of a cold start job in the local environment.
# - Uses `check_local_status` to validate the job's exit code.
# - Executes `finalize_cold_start` to finalize the cold start job and mark it as DONE.
run_cold_start_job_callback_local = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_local_for_failure, finalize_func=finalize_cold_start_after_callback
)

# Handles the completion of a forecast job in the local environment.
# - Uses `check_local_status` to validate the job's exit code.
# - Executes `finalize_forecast` to finalize the forecast job and mark it as DONE.
run_forecast_job_callback_local = functools.partial(
    run_generic_job_end_callback, check_if_failed=check_local_for_failure, finalize_func=finalize_forecast_after_callback
)


#
# # Handles the completion of a forecast job in the local environment.
# # - Uses `check_local_status` to validate the job's exit code.
# # - Executes `finalize_forecast` to finalize the forecast job and mark it as DONE.
# run_forecast_forcing_download_job_callback_local = functools.partial(
#     run_generic_job_end_callback, check_if_failed=check_local_for_failure, finalize_func=finalize_forecast_forcing_download_after_callback
# )


def spawn_job(run: BaseRun, args: list[str], callback_function: Callable[[Future], None], simulate: bool = False) -> None:
    """
    Start a new process to execute the job and register it in the system.

    This function spawns a subprocess using the specified arguments,
    registers the job in the global job registry, and associates a callback
    function to handle job completion events.

    :param run: The BaseRun object representing the job (e.g., CalibrationRun, ValidationRun, etc.).
    :param args: A list of arguments to pass to the job script.
    :param callback_function: The callback function to invoke when the process completes.
    :param simulate: If True, simulates successful execution without running a real job.
    :raises Exception: If the subprocess fails to start in non-simulated mode.
    """
    job_description = get_job_description(run)

    if simulate:
        logger.info(f"Simulating job execution for: {job_description}")
        # Simulate a 10-second execution delay
        future = pool.submit(lambda: (
            time.sleep(10),  # Simulate a delay
            0  # Simulate a successful exit code
        )[1])
        future.add_done_callback(callback_function)
        return

    logger.info(f"Spawning process: {job_description} with {args}")

    try:
        run.status = StatusEnum.RUNNING.db_instance
        run.run_start = datetime.now(timezone.utc)
        run.save(update_fields=["status", "run_start"])

        # Prepare the environment for the subprocess needed for Forecast forcing downloading
        env = os.environ.copy()
        env["WGRIB2"] = os.path.expanduser("~/miniconda3/envs/NextGen_Forcings_Engine/bin/wgrib2")

        # Start the subprocess with the provided arguments and updated environment
        process = subprocess.Popen(args, env=env)

        # Submit the process to the thread pool executor
        future = pool.submit(process.wait)

        # Register the job for future reference
        job_registry[get_job_registry_key(run)] = process

        # Add a callback to be invoked when the process completes
        future.add_done_callback(callback_function)
    except Exception as e:
        logger.error(f"Failed to execute command: {str(e)}")
        raise
    logger.info(f'{job_description} is running in the background')


def cancel_local_job(run: BaseRun) -> bool:
    """
    Terminate a running job (LOCAL or DOCKER) and remove it from the job registry.

    This function attempts to gracefully terminate the process associated with the
    given `run` object. If successful, it removes the job from the global job registry.

    - LOCAL: kills the spawned process directly.
    - DOCKER: sends `docker kill <container_name>`. Containers use `--rm`, so they're auto-removed after exit.

    :param run: The CalibrationRun, ValidationRun, or ForecastRun object to cancel.
    :return: True if the job was successfully terminated, False otherwise.
    """
    job_description = get_job_description(run)
    logger.info(f"Cancelling {job_description}")

    key = get_job_registry_key(run)

    if NGEN_ENVIRONMENT == NgenEnvironmentEnum.DOCKER:
        container_name = key
        logger.info(f"Killing Docker container {container_name}")
        result = subprocess.run(["docker", "kill", container_name], check=False, capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"Container {container_name} killed successfully")
            job_registry.pop(key, None)
            return True
        else:
            logger.warning(f"Failed to kill container {container_name}: {result.stderr.strip()}")
            return False
    else:
        process = job_registry.get(key)
        if process:
            process.terminate()
            logger.info(f"{job_description} has been terminated.")
            job_registry.pop(key, None)
            return True
        else:
            logger.warning(f"No running job found for {job_description}")
            return False
