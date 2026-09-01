"""
Shared runtime argument mapping used by execution backends.

Responsibilities:

- Mapping job types to runtime script names
- Building normalized runtime arguments
- Resolving common execution metadata

Returned runtime metadata includes:

- script_name
- payload_values
- input/output files
- node_type
- nprocs

Execution backends use these mappings consistently:

- Docker
    - Development execution path
    - Runs containers directly from Django

- Slurm
    - AWS/HPC execution path
    - Runs jobs through the scheduler

This module answers:

    "What runtime arguments are required for this job?"

This module does not:

- Execute jobs
- Manage lifecycle state
- Submit jobs
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_script_name(job_type: str, payload: dict[str, Any]) -> str:
    """
    Return the runtime script name for a job.

    Most jobs map directly from job_type to script name.
    Validation iteration jobs use validation_iteration.

    :param job_type: Normalized job type string
    :param payload: Job-specific execution payload
    :return: Script name to pass into the runtime container
    """
    if (
            job_type == "validation"
            and payload.get("worker_name")
            and payload.get("iteration_num") is not None
    ):
        return "validation_iteration"

    return job_type


def get_job_runtime_details(
        job_type: str,
        payload: dict[str, Any],
) -> tuple[str, list[str], str, str, str | None, int | str]:
    """
    Return common runtime details used by both Docker and Slurm executors.

    :return: script_name, payload_values, input_file, stdout_file, node_type, nprocs
    """
    script_name = get_script_name(job_type, payload)
    node_type = payload.get("node_type")
    nprocs = payload.get("nprocs", "1")

    if job_type == "calibration":
        return (
            script_name,
            [payload["input_file"]],
            payload["input_file"],
            payload["output_file"],
            node_type,
            nprocs,
        )

    if job_type == "validation":
        if script_name == "validation_iteration":
            payload_values = [
                payload["input_file"],
                payload["worker_name"],
                str(payload["iteration_num"]),
            ]
        else:
            payload_values = [payload["input_file"]]

        return (
            script_name,
            payload_values,
            payload["input_file"],
            payload["output_file"],
            node_type,
            nprocs,
        )

    if job_type in ["cold_start", "forecast"]:
        return (
            script_name,
            [
                payload["validation_yaml"],
                payload["realization_file"],
            ],
            payload["validation_yaml"],
            payload["stdout_file"],
            None,
            "1",
        )

    if job_type == "hindcast":
        return (
            script_name,
            [
                payload["validation_yaml"],
                payload["config_file"],
                payload["run_name"],
                str(payload["interval_cycle"]),
                str(payload["num_iterations"]),
                payload["use_state"],
            ],
            payload["validation_yaml"],
            payload["stdout_file"],
            None,
            "1",
        )

    if job_type == "verification":
        return (
            script_name,
            [payload["verification_config"]],
            payload["verification_config"],
            payload["stdout_file"],
            None,
            "1",
        )

    raise ValueError(f"Unsupported job_type: {job_type}")
