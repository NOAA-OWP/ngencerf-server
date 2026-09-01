"""
Job execution routing for Django.

Responsibilities:

- Mapping runs to JobType values
- Building normalized execution payloads
- Selecting execution backends
- Dispatching submission requests
- Dispatching cancellation requests

Execution backends determine how jobs are launched:

- Docker
    - Used primarily for local development and testing
    - Runs jobs directly as Docker containers from the Django server

- Slurm
    - Used for deployed environments (AWS PCS / HPC)
    - Django submits jobs to the Slurm scheduler and execution occurs
      asynchronously on compute resources

This module answers:

    "Which execution backend should run this job?"

This module does not:

- Manage lifecycle state
- Execute jobs directly
- Perform post-processing
"""

import logging
from typing import Any

from django.contrib.auth import get_user_model

from calibration.enums import JobType
from calibration.enums_vanilla import JobExecutionMode
from calibration.models import CalibrationRun, ValidationRun, ForecastRun, ColdStartRun, VerificationRun, HindcastRun
from calibration.models.base_run import BaseRun
from calibration.views.common import generate_custom_token, TOKEN_SLURM_SCOPE
from cerfServer.settings import JOB_EXECUTION_MODE

logger = logging.getLogger(__name__)

User = get_user_model()


def submit_job_request(
        run: BaseRun,
        arguments: dict[str, str],
        stdout_file: str,
) -> None:
    job_type = get_job_type(run)
    payload = build_job_submit_payload(run, arguments, stdout_file, job_type)

    if JOB_EXECUTION_MODE in {
        JobExecutionMode.SLURM,
        JobExecutionMode.SLURM_MOCK
    }:
        payload["auth_token"] = generate_custom_token(
            get_run_owner(run),
            TOKEN_SLURM_SCOPE,
        )

        from calibration.run_util.job_executor_slurm import submit_slurm_job

        slurm_job_id = submit_slurm_job(
            job_type.value,
            run.id,
            payload
        )
        run.slurm_job_id = slurm_job_id
        run.save(update_fields=["slurm_job_id"])
        return

    if JOB_EXECUTION_MODE == JobExecutionMode.DOCKER:
        from calibration.run_util.job_executor_docker import run_docker_job

        run_docker_job(job_type.value, run.id, payload)
        return

    raise ValueError(f"Unsupported JOB_EXECUTION_MODE: {JOB_EXECUTION_MODE}")


def cancel_job_request(run: BaseRun) -> bool:
    job_type = get_job_type(run)

    if JOB_EXECUTION_MODE == JobExecutionMode.DOCKER:
        from calibration.run_util.job_executor_docker import cancel_docker_job
        return cancel_docker_job(job_type.value, run.id)

    if JOB_EXECUTION_MODE == JobExecutionMode.SLURM:
        from calibration.run_util.job_executor_slurm import cancel_slurm_job

        if run.slurm_job_id is None:
            raise ValueError(f"Cannot cancel {job_type.value} run {run.id} without slurm_job_id")

        return cancel_slurm_job(job_type.value, run.id, run.slurm_job_id)

    if JOB_EXECUTION_MODE == JobExecutionMode.SLURM_MOCK:
        logger.warning(
            "SLURM_MOCK mode enabled; treating cancel request as successful "
            "without calling scancel for job_type=%s run_id=%s slurm_job_id=%s",
            job_type.value,
            run.id,
            run.slurm_job_id,
        )
        return True

    raise ValueError(f"Unsupported JOB_EXECUTION_MODE: {JOB_EXECUTION_MODE}")


def build_job_submit_payload(
        run: BaseRun,
        arguments: dict[str, str],
        stdout_file: str,
        job_type: JobType | None = None,
) -> dict[str, Any]:
    """
    Build the job-specific execution payload

    This helper only builds normalized execution data. The configured backend
    decides how to execute it.

    :param run: Run instance
    :param arguments: Job-specific arguments
    :param stdout_file: Output file path
    :param job_type: Optional precomputed JobType
    :return: Payload dict
    :raises ValueError: If run type is unsupported
    """
    job_type = job_type or get_job_type(run)

    if job_type == JobType.CALIBRATION:
        calibration_run = run
        assert isinstance(calibration_run, CalibrationRun)
        return {
            "input_file": arguments["input_file"],
            "output_file": stdout_file,
            "nprocs": arguments["nprocs"],
            "node_type": calibration_run.node_type,
        }

    if job_type == JobType.VALIDATION:
        validation_run = run
        assert isinstance(validation_run, ValidationRun)
        return {
            "validation_type": validation_run.validation_type,
            "input_file": arguments["input_file"],
            "output_file": stdout_file,
            "nprocs": arguments["nprocs"],
            "node_type": validation_run.calibration_run.node_type,
            "worker_name": arguments.get("worker_name"),
            "iteration_num": arguments.get("iteration_num"),
        }

    if job_type == JobType.COLD_START:
        return {
            "validation_yaml": arguments["validation_yaml"],
            "realization_file": arguments["realization_file"],
            "stdout_file": stdout_file,
        }

    if job_type == JobType.FORECAST:
        return {
            "validation_yaml": arguments["validation_yaml"],
            "realization_file": arguments["realization_file"],
            "stdout_file": stdout_file,
        }

    if job_type == JobType.HINDCAST:
        return {
            "validation_yaml": arguments["validation_yaml"],
            "config_file": arguments["config_file"],
            "run_name": arguments["run_name"],
            "interval_cycle": arguments["interval_cycle"],
            "num_iterations": arguments["num_iterations"],
            "use_state": arguments["use_state"],
            "stdout_file": stdout_file,
        }

    if job_type == JobType.VERIFICATION:
        return {
            "verification_config": arguments["verification_config"],
            "stdout_file": stdout_file,
        }

    raise ValueError(f"Unsupported job type: {job_type}")


def get_job_type(run: BaseRun) -> JobType:
    """
    Map a run instance to its corresponding JobType enum.

    This ensures consistent job_type values across:
    - payload construction
    - Docker/Slurm backend dispatch
    - lifecycle handling

    :param run: Run instance
    :return: JobType enum
    :raises ValueError: If run type is unsupported
    """
    if isinstance(run, CalibrationRun):
        return JobType.CALIBRATION
    if isinstance(run, ValidationRun):
        return JobType.VALIDATION
    if isinstance(run, ColdStartRun):
        return JobType.COLD_START
    if isinstance(run, ForecastRun):
        return JobType.FORECAST
    if isinstance(run, HindcastRun):
        return JobType.HINDCAST
    if isinstance(run, VerificationRun):
        return JobType.VERIFICATION

    raise ValueError(f"Unsupported run type: {type(run).__name__}")


def get_run_owner(run: BaseRun) -> User:
    """
    Return the owner associated with a run.

    - CalibrationRun: owner is stored directly on the model.
    - ValidationRun, ColdStartRun, ForecastRun, HindcastRun: owner is resolved via calibration_run.
    - VerificationRun: owner is resolved via hindcast → calibration_run.

    :param run: A BaseRun instance.
    :return: The owner of the associated CalibrationRun.
    :raises AttributeError: If the run type is unsupported or ownership cannot be resolved.
    """
    if isinstance(run, CalibrationRun):
        return run.owner

    if isinstance(run, (ValidationRun, ColdStartRun, ForecastRun, HindcastRun)):
        return run.calibration_run.owner

    if isinstance(run, VerificationRun):
        return run.hindcast_run.calibration_run.owner

    raise AttributeError(f"Cannot determine owner for run of type {type(run).__name__}")
