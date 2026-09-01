"""
Docker execution backend.

Responsibilities:

- Building Docker commands
- Launching subprocesses
- Tracking running jobs
- Docker container cancellation
- Sending lifecycle events

Deployment usage:

- Primarily used for local development and testing
- Jobs execute directly from the Django server process

This module answers:

    "How does Docker execute this job?"
"""

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable

from calibration.enums import SlurmCallbackStatusEnum
from calibration.run_util.job_lifecycle import handle_job_event
from calibration.run_util.job_runtime_mapping import get_job_runtime_details
from cerfServer.settings import DOCKER_RUNTIME_INFO

logger = logging.getLogger(__name__)

pool: ThreadPoolExecutor = ThreadPoolExecutor()
job_registry: dict[str, subprocess.Popen] = {}


def get_registry_key(job_type: str, run_id: int) -> str:
    """
    Return the internal registry key used to track a running job process.

    This value is also used as the Docker container name when running in
    DOCKER mode.

    NOTE:
    This key is specific to the Docker execution path and is not used for
    Slurm jobs, which are tracked by slurm_job_id instead.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :return: Registry key / Docker container name
    """
    return f"{job_type}_{run_id}"


def build_docker_command(
        job_type: str,
        run_id: int,
        payload: dict[str, Any]
) -> list[str]:
    """
    Build the full Docker command used to launch a job.

    This selects the configured runtime command template, derives the script
    name from the job type, assigns a deterministic container name, and appends
    the job-specific arguments expected by the container entrypoint.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :param payload: Job-specific execution payload
    :return: Full command as a list suitable for subprocess.Popen
    :raises ValueError: If job_type is unsupported
    """
    script_name, payload_values, _, stdout_file, _, _ = get_job_runtime_details(
        job_type,
        payload,
    )
    template = DOCKER_RUNTIME_INFO[script_name]
    container_name = get_registry_key(job_type, run_id)

    spawn_command = template.format(name=container_name).split()

    return spawn_command + [script_name] + payload_values + [stdout_file]


def docker_job_done_callback(
        job_type: str,
        run_id: int,
) -> Callable[[Future], None]:
    """
    Return the completion callback for a running Docker job.

    The returned callback:
    - removes the job from the in-memory registry
    - inspects the process exit code
    - sends the corresponding terminal lifecycle event to Django

    Exit-code handling:
    - 0   -> DONE
    - < 0 -> CANCELED
    - > 0 -> FAILED

    If callback processing itself fails, a best-effort FAILED event is sent.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :return: Completion callback for the submitted Future
    """

    def _callback(future: Future) -> None:
        registry_key = get_registry_key(job_type, run_id)
        job_registry.pop(registry_key, None)

        try:
            exit_code = future.result()

            if exit_code == 0:
                status = SlurmCallbackStatusEnum.DONE
            elif exit_code < 0:
                status = SlurmCallbackStatusEnum.CANCELED
            else:
                status = SlurmCallbackStatusEnum.FAILED

            handle_job_event(job_type, run_id, status)

        except Exception:
            logger.exception(
                "DOCKER job completion callback failed for job_type=%s run_id=%s",
                job_type,
                run_id,
            )
            handle_job_event(
                job_type,
                run_id,
                SlurmCallbackStatusEnum.FAILED,
            )

    return _callback


def run_docker_job(job_type: str, run_id: int, payload: dict[str, Any]) -> None:
    """
    Launch a job in Docker and register lifecycle handling.

    This function:
    - builds the Docker command
    - starts the subprocess
    - stores the process in the in-memory registry
    - marks the run as STARTING/RUNNING through handle_job_event()
    - waits for completion in a background thread
    - sends the terminal event through handle_job_event()

    Docker lifecycle events are handled directly in-process, not through
    HTTP callback endpoints.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :param payload: Job-specific execution payload
    """
    command = build_docker_command(job_type, run_id, payload)

    logger.info(
        "Launching DOCKER job for job_type=%s run_id=%s command=%s",
        job_type,
        run_id,
        command,
    )

    env = os.environ.copy()
    env["WGRIB2"] = os.path.expanduser("~/miniconda3/envs/NextGen_Forcings_Engine/bin/wgrib2")

    process = subprocess.Popen(command, env=env)

    registry_key = get_registry_key(job_type, run_id)
    job_registry[registry_key] = process

    handle_job_event(job_type, run_id, SlurmCallbackStatusEnum.STARTING)

    future = pool.submit(process.wait)
    future.add_done_callback(
        docker_job_done_callback(job_type, run_id)
    )


def cancel_docker_job(job_type: str, run_id: int) -> bool:
    """
    Attempt to cancel a running Docker job.

    Cancellation is performed with `docker kill <container_name>`, where the
    container name matches the internal registry key.

    On successful kill:
    - remove the job from the in-memory registry
    - send a CANCELED lifecycle event

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :return: True if the container was killed successfully, otherwise False
    """
    registry_key = get_registry_key(job_type, run_id)
    container_name = registry_key

    logger.info(
        "Cancelling DOCKER job for job_type=%s run_id=%s container_name=%s",
        job_type,
        run_id,
        container_name,
    )

    result = subprocess.run(
        ["docker", "kill", container_name],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        logger.info(
            "Container %s killed successfully for job_type=%s run_id=%s",
            container_name,
            job_type,
            run_id,
        )

        job_registry.pop(registry_key, None)

        # Do not let the view mark CANCELLED directly; lifecycle handling
        # also sets run_end and executes finalization logic.
        handle_job_event(
            job_type,
            run_id,
            SlurmCallbackStatusEnum.CANCELED,
        )

        return True

    logger.error(
        "docker kill failed for job_type=%s run_id=%s container_name=%s: %s",
        job_type,
        run_id,
        container_name,
        result.stderr.strip(),
    )

    return False
