"""
Slurm execution backend.

Responsibilities:

- Building Singularity commands
- Generating Slurm batch scripts
- Submitting, cancelling, and querying jobs over the Slurm REST API (slurmrestd)
- Sending lifecycle callbacks to Django

Deployment usage:

- Used primarily in deployed AWS PCS / HPC environments
- Submits to slurmrestd over HTTP with a JWT instead of the sbatch/scancel/squeue
  CLIs, so the Django runtime needs no Slurm client binaries or munge
- Requires network access to slurmrestd (port 6820) and read access to the
  cluster's JWT signing key in AWS Secrets Manager
- Requires shared filesystem access between Django and Slurm compute nodes
- Job execution occurs asynchronously on Slurm compute resources

This module answers:

    "How does Slurm execute and report this job?"
"""

import base64
import logging
import os
import subprocess
import time
from functools import lru_cache
from typing import Any

import boto3
import jwt
import requests
from django.conf import settings

from calibration.enums import SlurmCallbackStatusEnum
from calibration.enums_vanilla import JobExecutionMode
from calibration.run_util.job_lifecycle import handle_job_event
from calibration.run_util.job_runtime_mapping import get_job_runtime_details
from calibration.views.common import map_path_to_host

logger = logging.getLogger(__name__)

# Timeout (seconds) for every slurmrestd HTTP request.
_REQUEST_TIMEOUT = 30

# Slurm job states that mean the job has stopped running (no longer active).
_SLURM_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
})


def ensure_file_owned(file_path: str) -> dict[str, bool | str]:
    """
    Ensure the current user can write to the target file path.

    # TODO This was needed on Parallel Works.  Not sure if it will be a problem on other environments

    Creates the parent directory if needed, changes ownership of the parent
    directory to the current user/group, and creates the file if it does not
    already exist.

    :param file_path: Local filesystem path to prepare
    :return: Result dictionary containing success flag and message
    """
    try:
        current_uid = os.getuid()
        current_gid = os.getgid()
        file_path_dir = os.path.dirname(file_path)
        subprocess.run(f"sudo mkdir -p {file_path_dir}", shell=True, check=True)
        command_chown = f"sudo chown {current_uid}:{current_gid} {file_path_dir}"
        subprocess.run(command_chown, shell=True, check=True)
        subprocess.run(f"touch {file_path}", shell=True, check=True)
        logger.info(f"Ownership granted to file {file_path}")
        return {"success": True, "message": f"Access granted to {file_path}"}
    except subprocess.CalledProcessError as e:
        logger.exception(f"Failed to change ownership of file {file_path}")
        return {"success": False, "message": str(e)}


def build_slurm_command(
        job_type: str,
        payload: dict[str, Any],
) -> tuple[str, str, str, str | None, int | str]:
    """
    Build the Singularity command and Slurm submission metadata.

    Slurm uses the same runtime argument mapping as Docker, but wraps those
    arguments in a Singularity command instead of a Docker command.

    :param job_type: Normalized job type string.
    :param payload: Job-specific execution payload.
    :return: Tuple of singularity_run_cmd, input_file, stdout_file, node_type, and nprocs.
    :raises KeyError: If required payload fields are missing.
    :raises ValueError: If job_type is unsupported.
    """
    script_name, payload_values, input_file, stdout_file, node_type, nprocs = get_job_runtime_details(
        job_type,
        payload
    )

    template = settings.SINGULARITY_RUNTIME_INFO[script_name]
    singularity_run_cmd = " ".join([template, script_name, *payload_values])

    return singularity_run_cmd, input_file, stdout_file, node_type, nprocs


def get_callback_run_id_field(job_type: str) -> str:
    """
    Return the callback request field name that identifies the run.

    Each Slurm callback endpoint expects a job-specific run id field, such as
    calibration_run_id or forecast_run_id.

    :param job_type: Normalized job type string.
    :return: Callback payload field name containing the run id.
    :raises ValueError: If job_type is unsupported.
    """
    callback_id_fields = {
        "calibration": "calibration_run_id",
        "validation": "validation_run_id",
        "cold_start": "cold_start_run_id",
        "forecast": "forecast_run_id",
        "hindcast": "hindcast_run_id",
        "verification": "verification_run_id",
    }

    try:
        return callback_id_fields[job_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported job_type for callback: {job_type}") from exc


def get_callback_url(job_type: str) -> str:
    """
    Build the Django callback URL for a Slurm job type.

    The URL is derived from the configured server base URL plus the
    job-specific callback endpoint path.

    :param job_type: Normalized job type string.
    :return: Fully qualified callback URL for the job type.
    :raises ValueError: If job_type is unsupported.
    """
    callback_paths = {
        "calibration": "/calibration/calibration_job_slurm_callback/",
        "validation": "/calibration/validation_job_slurm_callback/",
        "cold_start": "/calibration/cold_start_job_slurm_callback/",
        "forecast": "/calibration/forecast_job_slurm_callback/",
        "hindcast": "/calibration/hindcast_job_slurm_callback/",
        "verification": "/calibration/verification_job_slurm_callback/",
    }

    try:
        callback_path = callback_paths[job_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported job_type for callback: {job_type}") from exc

    return settings.NGENCERF_BASE_URL.rstrip("/") + callback_path


def write_slurm_script(
        run_id: int,
        job_type: str,
        input_file: str,
        output_file: str,
        singularity_run_cmd: str,
        auth_token: str,
        nprocs: int | str = 1,
) -> str:
    """
    Write the Slurm batch script used to execute a job.

    The generated script:
    - notifies Django when Slurm starts the job
    - repairs ownership and permissions under the job directory
    - runs the Singularity command under Slurm CPU affinity
    - notifies Django with the terminal job status

    :param run_id: Run identifier.
    :param job_type: Normalized job type string.
    :param input_file: Input file path under CONTAINER_DATA_ROOT (the path the
        Django server sees). Translated to the compute-node path for the script.
    :param output_file: Stdout file path under CONTAINER_DATA_ROOT (the path the
        Django server sees). Translated to the compute-node path for the script.
    :param singularity_run_cmd: Full Singularity command to execute.
    :param auth_token: Per-job callback token generated by Django and embedded
        into the Slurm script for authenticated callback requests.
    :param nprocs: Number of CPUs requested for the job.
    :return: Path to the generated Slurm script.
    """

    # Django writes this script from the web tier, so it is built on the
    # container path Django can write (CONTAINER_DATA_ROOT). Its contents
    # reference the compute-node (host) paths via map_path_to_host, because the
    # script and the job execute on the compute node. map_path_to_host is a
    # no-op when HOST_DATA_ROOT == CONTAINER_DATA_ROOT (local / SLURM_MOCK).
    output_file_host = map_path_to_host(output_file)
    input_file_host = map_path_to_host(input_file)

    # Use the stdout file basename to place the generated Slurm script beside the job log
    job_script = output_file.rsplit(".", 1)[0] + ".slurm.sh"

    # The run's Output dir is on the shared filesystem; ensure it exists on the
    # container path before Django writes the script into it. exist_ok makes this
    # a no-op if the prepare step already created it. Slurm also writes the job
    # stdout here, so the directory must exist before submission either way.
    os.makedirs(os.path.dirname(job_script), exist_ok=True)

    # Repair permissions from the run directory level, not only the specific input file directory
    job_dir = os.path.dirname(os.path.dirname(input_file_host))

    callback_url = get_callback_url(job_type)
    callback_run_id_field = get_callback_run_id_field(job_type)

    # Token is generated by Django per submission and embedded into this job script.
    callback_token = auth_token

    # Ensure script/log files are writable by the current user
    # ensure_file_owned(job_script)
    # ensure_file_owned(output_file_host)

    with open(job_script, "w") as script:
        script.write("#!/bin/bash\n")
        script.write(f"#SBATCH --job-name={job_type}-{run_id}\n")
        script.write("#SBATCH --nodes=1\n")
        # Retain this directive for direct sbatch compatibility and to make the
        # generated script self-describing. The slurmrestd submission path also
        # explicitly sets requeue=False in _submit_via_slurmrestd().
        script.write("#SBATCH --no-requeue\n")
        script.write("#SBATCH --ntasks=1\n")
        script.write(f"#SBATCH --cpus-per-task={nprocs}\n")
        script.write(f"#SBATCH --output={output_file_host}\n")
        script.write("\n")
        script.write("set +e\n\n")

        script.write(f'CALLBACK_URL="{callback_url}"\n')
        script.write(f'CALLBACK_TOKEN="{callback_token}"\n')
        script.write(f'CALLBACK_RUN_ID_FIELD="{callback_run_id_field}"\n')
        script.write(f'RUN_ID="{run_id}"\n\n')

        script.write(
            """notify_job_event() {
    local job_status="$1"

    curl -fsS -X POST "$CALLBACK_URL" \\
        -H "Authorization: Bearer $CALLBACK_TOKEN" \\
        -H "Content-Type: application/json" \\
        --data "{
            \\"${CALLBACK_RUN_ID_FIELD}\\": ${RUN_ID},
            \\"job_status\\": \\"${job_status}\\",
            \\"slurm_job_id\\": ${SLURM_JOB_ID}
        }"

    if [ $? -ne 0 ]; then
        echo "WARNING: Failed to notify Django callback endpoint for status ${job_status}"
    fi
}

"""
        )

        script.write('echo "Running Slurm Job $SLURM_JOB_ID"\n\n')

        current_uid = os.getuid()
        current_gid = os.getgid()

        # Number of parallel workers used by xargs during permission repair.
        # Falls back to 8 if nproc is unavailable on the compute node.
        script.write('p="$(command -v nproc >/dev/null 2>&1 && nproc || echo 8)"\n')

        # Some run files may be owned by root or another account after prior
        # containerized steps. Reset ownership back to the current runner user.
        script.write(
            f'sudo find -L "{job_dir}" \\( ! -uid {current_uid} -o ! -gid {current_gid} \\) ! -type l -print0 '
            f'| sudo xargs -0 -r -P"$p" chown {current_uid}:{current_gid}\n\n'
        )

        # Ensure files are readable/writable and directories remain traversable.
        # a+rwX applies execute only where appropriate (dirs / existing executables).
        script.write(
            f'sudo find -L "{job_dir}" ! -type l '
            f'\\( ! -perm -u+r -o ! -perm -u+w -o \\( -xtype d ! -perm -u+x \\) \\) -print0 '
            f'| sudo xargs -0 -r -P"$p" chmod a+rwX\n\n'
        )

        script.write("notify_job_event STARTING\n\n")

        script.write("# Extract the exact CPUs Slurm assigned to this job\n")
        # Read the CPU affinity mask assigned by Slurm and convert it to a
        # comma-separated CPU list for taskset.
        script.write(
            'CPUSET=$(python3 -c "import os; '
            'print(*sorted(os.sched_getaffinity(0)), sep=\',\')")\n'
        )
        script.write('echo "Job isolated to CPUs: $CPUSET"\n\n')

        # Pass through OpenMPI overrides inside the Singularity container. The PCS
        # Slurm job runs as root (the work tree is chown'd to 0:0 above), so OpenMPI's
        # run-as-root guard would abort mpirun; allow it explicitly.
        script.write("export SINGULARITYENV_OMPI_MCA_rmaps_base_oversubscribe=1\n")
        script.write("export SINGULARITYENV_OMPI_ALLOW_RUN_AS_ROOT=1\n")
        script.write("export SINGULARITYENV_OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1\n")

        # Keep Slurm out of the container's MPI launch. PCS keeps its Slurm
        # binaries (srun) on the host under /opt/aws/pcs/..., not inside the SIF,
        # so OpenMPI's Slurm launcher cannot find srun and aborts. Tell OpenMPI to
        # skip both the Slurm allocation reader (ras) and the Slurm launcher (plm)
        # so mpirun forks its ranks locally on the single node we already pin with
        # taskset. The container never uses Slurm; the calling script owns core
        # placement (taskset + cpus-per-task), matching how jobs ran on PW.
        script.write("export SINGULARITYENV_OMPI_MCA_ras='^slurm'\n")
        script.write("export SINGULARITYENV_OMPI_MCA_plm=rsh\n\n")

        # Per-job scratch dir on the shared filesystem (EFS). Everything that
        # writes to /tmp inside the container lands here: ngen-forcing's hardcoded
        # /tmp cache files, the PMIx dstore (OpenMPI's on-disk key/value store),
        # and HDF5 swap. The dir lives under HOST_DATA_ROOT so it is also visible
        # inside the container at ${CONTAINER_DATA_ROOT}/scratch/<label> via the
        # existing data-dir bind, and SINGULARITY_BIND additionally maps it onto
        # the container's /tmp. The label carries job_type + run_id for
        # greppability; SLURM_JOB_ID makes it unique.
        #
        # /var/tmp and the home directory get the same treatment: the runtime's
        # default mounts also pass the node's own /var/tmp and /root through to
        # the container, so anything writing there lands on the node's local
        # root volume instead of EFS and can fill it, which takes the node (and
        # every job on it) down. Both are redirected to subdirs of the same
        # scratch dir: SINGULARITY_BIND covers /var/tmp, and SINGULARITY_HOME
        # mounts .home at /root while keeping HOME=/root for the workload. The
        # workload images bake nothing into /root at runtime (their Dockerfile
        # /root/.cache references are build-time cache mounts), so masking it is
        # safe. The EXIT trap and the orphan sweep clean both with everything
        # else.
        scratch_root = os.path.join(settings.HOST_DATA_ROOT, "scratch")
        host_scratch = os.path.join(
            scratch_root, f"{job_type}-{run_id}-${{SLURM_JOB_ID}}"
        )
        script.write(f'mkdir -p "{scratch_root}"\n')
        # Opportunistic sweep of scratch dirs orphaned by SIGKILL / node crashes
        # (the EXIT trap below covers normal exits and SIGTERM). Self-guards: if
        # squeue is unavailable or returns nothing, skip rather than delete every
        # dir.
        script.write(
            f"(active=$(squeue -h -o '%i' 2>/dev/null | sort -u); "
            f'[ -n "$active" ] || exit 0; '
            f'for d in "{scratch_root}"/*; do '
            f'[ -d "$d" ] || continue; '
            f'jobid="${{d##*-}}"; '
            f'echo "$active" | grep -qx "$jobid" || rm -rf "$d"; '
            f"done) || true\n"
        )
        script.write(f'export TMPDIR="{host_scratch}"\n')
        script.write('export SINGULARITY_BIND="$TMPDIR:/tmp,$TMPDIR/.vartmp:/var/tmp"\n')
        script.write('export SINGULARITY_HOME="$TMPDIR/.home:/root"\n')
        script.write("export SINGULARITYENV_TMPDIR=/tmp\n")
        script.write('mkdir -p "$TMPDIR/.vartmp" "$TMPDIR/.home"\n')
        script.write("trap 'rm -rf \"$TMPDIR\"' EXIT\n\n")

        # Force the workload to stay inside the CPUs Slurm granted this job.
        modified_singularity_run_cmd = f'taskset -c "${{CPUSET}}" {singularity_run_cmd}'
        script.write(f"{modified_singularity_run_cmd}\n")
        script.write("exit_code=$?\n\n")

        script.write('if [ "$exit_code" -eq 0 ]; then\n')
        script.write('    job_status="DONE"\n')
        script.write("else\n")
        script.write('    job_status="FAILED"\n')
        script.write("fi\n\n")

        script.write('echo "Job completed with status $job_status and exit_code=$exit_code"\n\n')

        script.write('notify_job_event "$job_status"\n')
        script.write("exit $exit_code\n")

    return job_script


@lru_cache(maxsize=1)
def _slurm_jwt_signing_key() -> bytes:
    """
    Fetch and decode the cluster's JWT signing key from AWS Secrets Manager.

    AWS PCS stores the key as a base64-encoded SecretString; it must be decoded
    to the raw bytes Slurm signs with. Cached for the life of the process because
    the key is stable for a given cluster.

    :return: Raw HS256 signing key bytes.
    :raises RuntimeError: If SLURM_JWT_SECRET_ARN is not configured.
    """
    if not settings.SLURM_JWT_SECRET_ARN:
        raise RuntimeError("SLURM_JWT_SECRET_ARN is not configured")

    secret_string = boto3.client("secretsmanager").get_secret_value(
        SecretId=settings.SLURM_JWT_SECRET_ARN
    )["SecretString"]

    return base64.b64decode(secret_string.strip())


def _slurm_jwt() -> str:
    """
    Mint a short-lived, enriched JWT for the Slurm REST API.

    AWS PCS rejects tokens that lack POSIX identity claims
    (disable_jwt_without_identity_claims), so the token carries uid/gid and the
    id{} object in addition to the username (sun) claim.

    :return: Signed HS256 JWT.
    """
    now = int(time.time())
    user = settings.SLURM_REST_USER
    gid = settings.SLURM_REST_GID
    home = "/root" if user == "root" else f"/home/{user}"

    payload = {
        "exp": now + settings.SLURM_REST_TOKEN_TTL_SECONDS,
        "iat": now,
        "sun": user,
        "uid": settings.SLURM_REST_UID,
        "gid": gid,
        "id": {
            "gecos": user,
            "dir": home,
            "gids": [gid],
            "shell": "/bin/bash",
        },
    }

    return jwt.encode(payload, _slurm_jwt_signing_key(), algorithm="HS256")


def _slurmrestd_base(namespace: str = "slurm") -> str:
    """
    Return the versioned slurmrestd base URL for a namespace.

    :param namespace: "slurm" (slurmctld) or "slurmdb" (accounting).
    :return: Base URL, e.g. http://10.0.0.10:6820/slurm/v0.0.43.
    :raises RuntimeError: If SLURM_REST_ENDPOINT is not configured.
    """
    if not settings.SLURM_REST_ENDPOINT:
        raise RuntimeError("SLURM_REST_ENDPOINT is not configured")

    base = settings.SLURM_REST_ENDPOINT.rstrip("/")
    return f"{base}/{namespace}/{settings.SLURM_API_VERSION}"


def _slurm_headers() -> dict[str, str]:
    """Build the auth + content headers for a slurmrestd request."""
    return {
        "Authorization": f"Bearer {_slurm_jwt()}",
        "Content-Type": "application/json",
    }


def _format_slurm_errors(errors: list[Any]) -> str:
    """Flatten a slurmrestd ``errors`` array into a single message."""
    messages = []
    for item in errors:
        if isinstance(item, dict):
            messages.append(item.get("description") or item.get("error") or str(item))
        else:
            messages.append(str(item))
    return "; ".join(messages) or "unknown slurmrestd error"


def _slurm_response_payload(
        response: requests.Response,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Validate a slurmrestd response and return its JSON body.

    slurmrestd can return HTTP 200 while still reporting a logical failure in the
    ``errors`` array, so both the HTTP status and ``errors`` are checked.

    :param response: The requests Response.
    :return: Tuple of (json_body, error_message). Exactly one is non-None.
    """
    if response.status_code >= 300:
        return None, f"HTTP {response.status_code}: {response.text.strip()[:500]}"

    try:
        data = response.json()
    except ValueError:
        return None, (
            f"non-JSON response (HTTP {response.status_code}): "
            f"{response.text.strip()[:500]}"
        )

    errors = data.get("errors") or []
    if errors:
        return None, _format_slurm_errors(errors)

    return data, None


def _submit_via_slurmrestd(
        job_script: str,
        *,
        name: str,
        partition: str | None,
        nprocs: int | str,
        standard_output: str,
        working_directory: str,
) -> tuple[str | None, str | None]:
    """
    Submit a Slurm batch job over the Slurm REST API (slurmrestd).

    Reads the batch script produced by write_slurm_script and POSTs it to the
    job/submit endpoint with an enriched JWT. The script body (including the
    Django callbacks it performs from the compute node) is identical to the
    sbatch path; only the submission transport differs. Job properties are sent
    explicitly in the ``job`` object so submission does not depend on slurmrestd
    parsing the script's embedded #SBATCH directives.

    :param job_script: Path to the generated Slurm script.
    :param name: Slurm job name.
    :param partition: Optional Slurm partition name.
    :param nprocs: CPUs to request for the job's single task.
    :param standard_output: Stdout file path for the job.
    :param working_directory: Existing directory the job runs from.
    :return: Tuple of slurm_job_id and error message.
    """
    try:
        with open(job_script) as handle:
            script_body = handle.read()

        job: dict[str, Any] = {
            "name": name,
            "current_working_directory": working_directory,
            "standard_output": standard_output,
            "environment": settings.SLURM_REST_JOB_ENVIRONMENT,
            "tasks": 1,
            # Do not rely on the script's #SBATCH --no-requeue directive when
            # submitting through slurmrestd. Without this explicit setting,
            # Slurm uses the cluster default JobRequeue=1.
            "requeue": False,
        }

        if partition:
            job["partition"] = partition

        try:
            cpus = int(nprocs)
            if cpus > 0:
                job["cpus_per_task"] = cpus
        except (TypeError, ValueError):
            pass

        url = f"{_slurmrestd_base()}/job/submit"
        logger.info("Submitting job '%s' to slurmrestd: %s", name, url)

        response = requests.post(
            url,
            headers=_slurm_headers(),
            json={"job": job, "script": script_body},
            timeout=_REQUEST_TIMEOUT,
        )

        data, error = _slurm_response_payload(response)
        if error:
            return None, f"slurmrestd rejected job '{name}': {error}"
        assert data is not None

        job_id = data.get("job_id")
        if not job_id:
            return None, f"slurmrestd did not return a job_id for '{name}': {data}"

        return str(job_id), None

    except Exception as e:
        error_msg = f"Failed to submit job script {job_script} via slurmrestd: {str(e)}"
        logger.exception(error_msg)
        return None, error_msg


def get_slurm_job_accounting(slurm_job_id: int) -> dict[str, Any] | None:
    """
    Retrieve accounting data for a completed Slurm job from SlurmDB.

    :param slurm_job_id: Slurm job identifier.
    :return: SlurmDB response payload, or None if the request fails or the
        accounting record is not available yet.
    """
    try:
        response = requests.get(
            f"{_slurmrestd_base('slurmdb')}/job/{slurm_job_id}",
            headers=_slurm_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as e:
        logger.warning(
            "SlurmDB accounting request failed for job %s: %s",
            slurm_job_id,
            e,
        )
        return None

    data, error = _slurm_response_payload(response)

    if error:
        logger.warning(
            "SlurmDB accounting request failed for job %s: %s",
            slurm_job_id,
            error,
        )
        return None

    return data


def submit_job(
        job_type: str,
        run_id: int,
        payload: dict[str, Any],
) -> str:
    """
    Submit a prepared job to Slurm.

    Resolves the Singularity command, converts container paths to host/shared
    filesystem paths, validates the selected Slurm partition, writes the Slurm
    script, submits it via the Slurm REST API, and returns the Slurm job id.

    In SLURM_MOCK mode, the script is written but submission is skipped.

    :param job_type: Normalized job type string.
    :param run_id: Run identifier.
    :param payload: Job-specific execution payload. Must include auth_token.
    :return: Slurm job id as a string. In SLURM_MOCK mode, returns "-1".
    :raises RuntimeError: If validation, script generation, or submission fails.
    :raises ValueError: If node_type is not an allowed Slurm partition.
    """
    logger.info("Starting Slurm job submission for job_type=%s run_id=%s", job_type, run_id)

    if not settings.HOST_DATA_ROOT:
        raise RuntimeError("HOST_DATA_ROOT is not configured")
    if not settings.CONTAINER_DATA_ROOT:
        raise RuntimeError("CONTAINER_DATA_ROOT is not configured")

    singularity_run_cmd, input_file, output_file, node_type, nprocs = build_slurm_command(
        job_type,
        payload,
    )

    # Django only sees the run data under CONTAINER_DATA_ROOT, so
    # validate the input against the container path it can actually read. The
    # compute-node (host) paths are produced via map_path_to_host at the point
    # each value is handed to Slurm.
    if not os.path.exists(input_file):
        error_msg = (
            f"Input file '{input_file}' does not exist under "
            f"{settings.CONTAINER_DATA_ROOT}."
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    if node_type and node_type not in settings.SLURM_PARTITIONS:
        raise ValueError(
            f"node_type {node_type} does not match any configured Slurm partitions: "
            f"{settings.SLURM_PARTITIONS}"
        )

    try:
        auth_token = payload.get("auth_token")

        if not isinstance(auth_token, str):
            raise RuntimeError(
                f"auth_token is required for Slurm callbacks "
                f"(job_type={job_type}, run_id={run_id})"
            )

        job_script = write_slurm_script(
            run_id=run_id,
            job_type=job_type,
            input_file=input_file,
            output_file=output_file,
            singularity_run_cmd=singularity_run_cmd,
            auth_token=auth_token,
            nprocs=nprocs,
        )

        logger.info("Job script written to: %s", job_script)

        if settings.JOB_EXECUTION_MODE == JobExecutionMode.SLURM_MOCK:
            logger.warning(
                "SLURM_MOCK mode enabled; skipping Slurm submission for "
                "job_type=%s run_id=%s. Generated script: %s",
                job_type,
                run_id,
                job_script,
            )
            return "-1"

        # slurmrestd runs the job on the cluster, so it needs the compute-node
        # (host) stdout path (no-op when HOST_DATA_ROOT == CONTAINER_DATA_ROOT).
        output_file_host = map_path_to_host(output_file)
        slurm_job_id, error = _submit_via_slurmrestd(
            job_script,
            name=f"{job_type}-{run_id}",
            partition=node_type,
            nprocs=nprocs,
            standard_output=output_file_host,
            working_directory=os.path.dirname(output_file_host),
        )
        if error:
            raise RuntimeError(error)
        assert slurm_job_id is not None

        return slurm_job_id

    except RuntimeError:
        raise
    except Exception as e:
        error_msg = f"Failed to submit Slurm job: {str(e)}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg)


def submit_slurm_job(job_type: str, run_id: int, payload: dict[str, Any]) -> int:
    """
    Submit a job through the Slurm execution path.

    Lifecycle flow:

    1. Django submits job and persists slurm_job_id.

    2. Generated Slurm script invokes callback endpoint with:
       STARTING

    3. Generated Slurm script invokes callback endpoint with:
       DONE / FAILED / CANCELED

    The callback endpoint updates lifecycle state and triggers
    post-processing.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :param payload: Job-specific execution payload
    """
    slurm_job_id_str = submit_job(job_type, run_id, payload)
    slurm_job_id = int(slurm_job_id_str)

    logger.info(
        "Submitted job_type=%s run_id=%s slurm_job_id=%s",
        job_type,
        run_id,
        slurm_job_id,
    )

    return slurm_job_id


def cancel_slurm_job(
        job_type: str,
        run_id: int,
        slurm_job_id: int,
) -> bool:
    """
    Cancel a job through the Slurm execution path.

    Sends a slurmrestd DELETE and a terminal CANCELED lifecycle event on success.

    A failed cancel request does not mean the job failed; it only means the
    cancellation request was not accepted.

    :param job_type: Normalized job type string
    :param run_id: Run identifier
    :param slurm_job_id: Slurm job identifier (required)
    :return: True if cancellation succeeded, False otherwise
    :raises ValueError: If slurm_job_id is None
    """
    if slurm_job_id is None:
        raise ValueError(
            f"slurm_job_id is required for Slurm cancellation "
            f"(job_type={job_type}, run_id={run_id})"
        )

    logger.info(
        "SLURM cancel request for job_type=%s run_id=%s slurm_job_id=%s",
        job_type,
        run_id,
        slurm_job_id,
    )

    # DELETE asks slurmrestd to cancel the job; like scancel it is asynchronous.
    try:
        response = requests.delete(
            f"{_slurmrestd_base()}/job/{slurm_job_id}",
            headers=_slurm_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        _, error = _slurm_response_payload(response)
    except Exception as e:
        error = str(e)

    if error:
        logger.error(
            "slurmrestd cancel failed for job_type=%s run_id=%s slurm_job_id=%s: %s",
            job_type,
            run_id,
            slurm_job_id,
            error,
        )
        return False

    handle_job_event(
        job_type=job_type,
        run_id=run_id,
        job_status=SlurmCallbackStatusEnum.CANCELED,
        slurm_job_id=slurm_job_id,
    )

    return True


def _normalize_state(raw_state: Any) -> str | None:
    """
    Reduce a slurmrestd job-state value to a single upper-case base state.

    Slurm 25.05 (v0.0.43) reports ``job_state`` as a list of flags such as
    ``["RUNNING"]`` or ``["CANCELLED"]``; the first element is the base state.

    :param raw_state: The raw job_state value (list or string).
    :return: Normalized state string, or None if unavailable.
    """
    if isinstance(raw_state, list):
        raw_state = raw_state[0] if raw_state else None

    if not raw_state:
        return None

    return str(raw_state).upper()


def _first_job_state(data: dict[str, Any]) -> str | None:
    """
    Extract the first job's state from a slurmrestd jobs response.

    Handles both the slurmctld shape (``jobs[0].job_state`` is a list) and the
    slurmdbd shape (state under ``jobs[0].state.current``).

    :param data: Parsed slurmrestd JSON body.
    :return: Normalized Slurm state, or None if no job/state is present.
    """
    jobs = data.get("jobs") or []
    if not jobs:
        return None

    job = jobs[0]
    raw_state = job.get("job_state")

    if raw_state is None:
        state_obj = job.get("state")
        if isinstance(state_obj, dict):
            raw_state = state_obj.get("current")
        else:
            raw_state = state_obj

    return _normalize_state(raw_state)


def _rest_job_state(slurm_id: int) -> str | None:
    """
    Query slurmctld (live) for a job's current state.

    Returns None if the job is no longer tracked by the controller (for example,
    it has aged out after completing) or the query fails.

    :param slurm_id: Slurm job id to query.
    :return: Normalized live Slurm state, or None.
    """
    try:
        response = requests.get(
            f"{_slurmrestd_base()}/job/{slurm_id}",
            headers=_slurm_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as e:
        logger.warning("slurmrestd job query failed for %s: %s", slurm_id, e)
        return None

    data, error = _slurm_response_payload(response)
    if error or not data:
        return None

    return _first_job_state(data)


def _rest_acct_state(slurm_id: int) -> str | None:
    """
    Query slurmdbd (accounting) for a job's recorded final state.

    Used as a fallback once a job has left the controller's live view.

    :param slurm_id: Slurm job id to query.
    :return: Normalized accounting Slurm state, or None.
    """
    try:
        response = requests.get(
            f"{_slurmrestd_base('slurmdb')}/job/{slurm_id}",
            headers=_slurm_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as e:
        logger.warning("slurmdbd job query failed for %s: %s", slurm_id, e)
        return None

    data, error = _slurm_response_payload(response)
    if error or not data:
        return None

    return _first_job_state(data)


def get_slurm_status(slurm_id: int) -> tuple[bool, str | None]:
    """
    Query Slurm directly for the current status of a job.

    Semantics:
    - If slurmctld no longer tracks the job, it is no longer active; fall back to
      slurmdbd accounting for the recorded final state.
    - If slurmctld reports COMPLETING, the job is in teardown/cleanup rather than
      normal execution. In that case, if accounting already reports a terminal
      state other than COMPLETED, treat the job as inactive and use that state.
      Otherwise, treat it as still active and allow time for callback/accounting
      to settle.
    - If slurmctld reports any other terminal state, the job is inactive.
    - For any other live state, treat the job as active.
    - If Slurm cannot be reached or returns unusable output, treat the job as not
      active with status UNKNOWN.

    :param slurm_id: Slurm job id to query.
    :return: Tuple (is_active, status_detail)
        - is_active: True if the job is considered active, False otherwise.
        - status_detail: A relevant Slurm status string, or UNKNOWN if indeterminate.
    """
    live_status = _rest_job_state(slurm_id)

    if not live_status:
        # No live record; rely on accounting for the terminal state.
        return False, _rest_acct_state(slurm_id) or "UNKNOWN"

    if live_status == "COMPLETING":
        acct_status = _rest_acct_state(slurm_id)
        if acct_status and acct_status != "COMPLETED":
            return False, acct_status
        return True, "COMPLETING"

    if live_status in _SLURM_TERMINAL_STATES:
        return False, live_status

    return True, live_status
