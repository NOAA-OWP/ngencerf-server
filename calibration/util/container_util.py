import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid

from django.conf import settings
from django.core.cache import cache

from calibration.enums_vanilla import JobExecutionMode
from calibration.views.called_from import get_caller_name

logger = logging.getLogger(__name__)


def _indent_output(output: str, indent: int = 2) -> str:
    """Indent each line of output by `indent` spaces."""
    if not output:
        return ""
    prefix = " " * indent
    return "\n".join(prefix + line for line in output.splitlines())


# noinspection PyTypeChecker
def copy_file_from_docker_image(image_name: str, container_name: str, src_path: str, dest_path: str) -> bool:
    """
    Copy a file from a Docker image using a temporary container.

    The temporary container receives a unique name to avoid collisions. Stale
    temporary containers using the same base-name prefix are removed on a
    best-effort basis, and the newly created container is removed after the copy
    operation whether the copy succeeds or fails.

    :param image_name: Name of the Docker image.
    :param container_name: Base name for the temporary container.
    :param src_path: Path to the file inside the container.
    :param dest_path: Destination path on the host.
    :return: True if the copy succeeds; otherwise False.
    """
    if shutil.which("docker") is None:
        logger.error("Docker binary not found on PATH.")
        return False

    success = False  # Default to failure

    # Add a guaranteed-unique suffix to avoid container name collisions
    unique_name = f"{container_name}_{uuid.uuid4().hex[:8]}"

    # Remove stale UUID-suffixed temporary containers on a best-effort basis.
    try:
        ps = subprocess.run(
            [
                "docker", "ps", "-a", "--filter", f"name={container_name}_", "-q"
            ],
            capture_output=True,
            text=True
        )
        stale_ids = [
            c.strip() for c in ps.stdout.splitlines() if c.strip()
        ]
        for container_id in stale_ids:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True
            )
    except OSError as e:
        logger.warning(f"Failed to clean stale containers for prefix {container_name}: {e}")

    dest_parent = os.path.dirname(dest_path) or "."
    if not os.path.isdir(dest_parent):
        logger.error(f"Destination directory does not exist: {dest_parent}")
        return False

    # Step 1: Create a temporary container
    create_cmd = ["docker", "create", "--name", unique_name, image_name]
    logger.debug(create_cmd)
    try:
        create_response = subprocess.run(create_cmd, check=True, capture_output=True, text=True)
        if create_response.stdout:
            container_id = create_response.stdout.strip()
            logger.info(f"Created temporary container {unique_name} (ID={container_id[:12]})")
        if create_response.stderr:
            logger.debug(f"[docker create stderr]\n{_indent_output(create_response.stderr.strip())}")

    except subprocess.CalledProcessError as e:
        logger.error(
            "Failed to create temporary container "
            f"(exit={e.returncode}).\n"
            f"Command: {' '.join(create_cmd)}\n"
            f"STDOUT:\n{_indent_output((e.stdout or '').strip())}\n"
            f"STDERR:\n{_indent_output((e.stderr or '').strip())}"
        )
        # Attempt best-effort cleanup in case the name was already taken
        try:
            subprocess.run(
                ["docker", "rm", "-f", unique_name],
                capture_output=True,
                text=True
            )
        except OSError:
            pass
        return False

    # Step 2: Copy the file from the container
    copy_cmd = ["docker", "cp", f"{unique_name}:{src_path}", dest_path]
    logger.debug(copy_cmd)
    try:
        copy_response = subprocess.run(copy_cmd, check=True, capture_output=True, text=True)
        if copy_response.stdout:
            logger.debug(f"[docker cp stdout]\n{_indent_output(copy_response.stdout.strip())}")
        if copy_response.stderr:
            # docker cp commonly prints nothing, but capture it if present
            logger.debug(f"[docker cp stderr]\n{_indent_output(copy_response.stderr.strip())}")

        logger.info(f"Successfully copied {src_path} to {dest_path}")
        success = True

    except subprocess.CalledProcessError as e:
        logger.error(
            "Error copying file from Docker container "
            f"(exit={e.returncode}).\n"
            f"Command: {' '.join(copy_cmd)}\n"
            f"STDOUT:\n{_indent_output((e.stdout or '').strip())}\n"
            f"STDERR:\n{_indent_output((e.stderr or '').strip())}"
        )

    finally:
        # Step 3: Remove the temporary container, even if the copy failed.
        rm_cmd = ["docker", "rm", "-f", unique_name]

        try:
            rm_response = subprocess.run(
                rm_cmd,
                capture_output=True,
                text=True
            )
        except OSError as e:
            logger.warning(
                f"Failed to run Docker cleanup command "
                f"{shlex.join(rm_cmd)}: {e}"
            )
        else:
            if rm_response.returncode != 0:
                logger.warning(
                    "Failed to remove temporary container "
                    f"(exit={rm_response.returncode}).\n"
                    f"Command: {shlex.join(rm_cmd)}\n"
                    f"STDOUT:\n"
                    f"{_indent_output((rm_response.stdout or '').strip())}\n"
                    f"STDERR:\n"
                    f"{_indent_output((rm_response.stderr or '').strip())}"
                )
            else:
                logger.debug(f"Removed temporary container {unique_name}")

    return success


def _find_sif_squashfs_offset(image_path: str) -> int | None:
    """
    Find the byte offset of the SquashFS filesystem embedded in a SIF image.

    ``singularity sif list`` reports each SIF data object's byte range. The
    SquashFS object's starting position can be passed directly to
    ``unsquashfs -o``, avoiding a full ``singularity sif dump`` of the
    filesystem object.

    :param image_path: Path to the SIF image.
    :return: Byte offset of the embedded SquashFS filesystem, or None if no
             SquashFS data object can be identified.
    """
    command = [
        "singularity",
        "sif",
        "list",
        image_path,
    ]
    logger.debug(f"{get_caller_name()} {command}")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        logger.error(
            f"{get_caller_name()} Failed to run "
            f"'singularity sif list' for {image_path}: {e}"
        )
        return None

    if result.returncode != 0:
        logger.error(
            f"{get_caller_name()} Failed to inspect SIF image "
            f"{image_path} (exit={result.returncode}).\n"
            f"Command: {shlex.join(command)}\n"
            f"STDOUT:\n{_indent_output(result.stdout)}\n"
            f"STDERR:\n{_indent_output(result.stderr)}"
        )
        return None

    logger.debug(
        f"{get_caller_name()} [singularity sif list stdout]\n"
        f"{_indent_output(result.stdout)}"
    )

    for line in result.stdout.splitlines():
        if "FS (Squashfs" not in line:
            continue

        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 5:
            logger.error(
                f"{get_caller_name()} Unexpected SquashFS descriptor line "
                f"in {image_path}: {line}"
            )
            return None

        position_range = fields[3]

        try:
            start_position = position_range.split("-", maxsplit=1)[0].strip()
            return int(start_position)
        except (IndexError, ValueError):
            logger.error(
                f"{get_caller_name()} Could not parse SquashFS position "
                f"'{position_range}' from {image_path}."
            )
            return None

    logger.error(
        f"{get_caller_name()} No SquashFS filesystem descriptor found "
        f"in SIF image {image_path}."
    )
    return None


def copy_files_from_singularity_image(
        image_path: str,
        files: dict[str, str],
) -> bool:
    """
    Copy multiple files directly from the SquashFS filesystem embedded in a
    Singularity SIF image.

    The SquashFS byte offset is obtained from ``singularity sif list`` and
    passed directly to ``unsquashfs -o``. This avoids dumping the complete
    SquashFS filesystem to a temporary file.

    Each requested file is first extracted to a temporary file in its
    destination directory. Destination files are replaced only after every
    extraction succeeds. Replacement is sequential and is not fully
    transactional if an individual ``os.replace`` operation fails.

    :param image_path: Path to the Singularity SIF image.
    :param files: Mapping of paths inside the image to local destination paths.
    :return: True if all requested files were copied successfully; otherwise
             False.
    """
    if not files:
        return True

    logger.info(
        f"{get_caller_name()} Copy {len(files)} file(s) from "
        f"Singularity image {image_path}"
    )

    if shutil.which("singularity") is None:
        logger.error(
            f"{get_caller_name()} The 'singularity' executable was not found."
        )
        return False

    if shutil.which("unsquashfs") is None:
        logger.error(
            f"{get_caller_name()} The 'unsquashfs' executable was not found."
        )
        return False

    if not os.path.isfile(image_path):
        logger.error(
            f"{get_caller_name()} Singularity image does not exist: "
            f"{image_path}"
        )
        return False

    for local_file_name in files.values():
        destination_directory = os.path.dirname(local_file_name)

        if destination_directory:
            try:
                os.makedirs(
                    destination_directory,
                    exist_ok=True,
                )
            except OSError as e:
                logger.error(
                    f"{get_caller_name()} Failed to create destination "
                    f"directory {destination_directory}: {e}"
                )
                return False

    squashfs_offset = _find_sif_squashfs_offset(image_path)
    if squashfs_offset is None:
        return False

    logger.debug(
        f"{get_caller_name()} Using SquashFS byte offset "
        f"{squashfs_offset} from image {image_path}"
    )

    temporary_files: dict[str, str] = {}

    try:
        for container_file_name, local_file_name in files.items():
            # unsquashfs expects a path relative to the SquashFS root.
            squashfs_file_name = container_file_name.lstrip("/")

            destination_directory = os.path.dirname(local_file_name) or "."

            file_descriptor, temporary_file_name = tempfile.mkstemp(
                prefix=f".{os.path.basename(local_file_name)}.",
                suffix=".tmp",
                dir=destination_directory,
            )
            os.close(file_descriptor)

            temporary_files[local_file_name] = temporary_file_name

            command = [
                "unsquashfs",
                "-o",
                str(squashfs_offset),
                "-cat",
                image_path,
                squashfs_file_name,
            ]
            logger.debug(f"{get_caller_name()} {command}")

            try:
                with open(temporary_file_name, "wb") as output_file:
                    result = subprocess.run(
                        command,
                        stdout=output_file,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
            except OSError as e:
                logger.error(
                    f"{get_caller_name()} Failed to extract "
                    f"{container_file_name} from {image_path}: {e}"
                )
                return False

            if result.returncode != 0:
                stderr = result.stderr.decode(
                    errors="replace",
                )

                logger.error(
                    f"{get_caller_name()} Failed to extract "
                    f"{container_file_name} from {image_path} "
                    f"(exit={result.returncode}).\n"
                    f"Command: {shlex.join(command)}\n"
                    f"STDERR:\n{_indent_output(stderr)}"
                )
                return False

            if os.path.getsize(temporary_file_name) == 0:
                logger.error(
                    f"{get_caller_name()} Extracted file is empty: "
                    f"{container_file_name} from {image_path}"
                )
                return False

        # All extractions succeeded. Replace the destination files.
        for local_file_name, temporary_file_name in temporary_files.items():
            os.replace(
                temporary_file_name,
                local_file_name,
            )

            logger.info(
                f"{get_caller_name()} Successfully copied file from "
                f"{image_path} to {local_file_name}"
            )

        return True

    except OSError as e:
        logger.error(
            f"{get_caller_name()} Failed while copying files from "
            f"{image_path}: {e}"
        )
        return False

    finally:
        for temporary_file_name in temporary_files.values():
            try:
                if os.path.exists(temporary_file_name):
                    os.remove(temporary_file_name)
            except OSError as e:
                logger.warning(
                    f"{get_caller_name()} Failed to remove temporary file "
                    f"{temporary_file_name}: {e}"
                )


def copy_file_from_singularity_image(
        image_path: str,
        src_path: str,
        dest_path: str,
) -> bool:
    """
    Copy one file from a Singularity image.

    This is a convenience wrapper around
    ``copy_files_from_singularity_image()``.

    :param image_path: Path to the Singularity image file (.sif).
    :param src_path: Path to the file inside the image.
    :param dest_path: Destination path on the host.
    :return: True if the file is copied successfully; otherwise False.
    """
    return copy_files_from_singularity_image(
        image_path,
        {src_path: dest_path},
    )


def generate_cache_key(
        image_name: str,
        container_name: str,
        container_file_name: str,
        local_file_name: str,
) -> str:
    """
    Generate a unique cache key for one image file-copy operation.

    :param image_name: Name of the image.
    :param container_name: Base container name used to distinguish copy operations.
    :param container_file_name: Path to the file inside the image.
    :param local_file_name: Local destination path.
    :return: Unique cache key.
    """
    key_string = (
        f"{container_name}:{container_file_name}:"
        f"{image_name}:{local_file_name}"
    )
    return "copy_file:" + hashlib.md5(key_string.encode()).hexdigest()


def copy_files_from_image(
        image_name: str,
        container_name: str,
        files: dict[str, str],
) -> bool:
    """
    Copy multiple files from a Docker or Singularity image.

    Successful individual file-copy operations are cached. A cached operation
    is reused only while its destination file still exists. This cache is
    separate from any higher-level cache of the data read from those files.

    In SLURM mode, uncached files are extracted directly from the SquashFS
    filesystem embedded in the SIF image.

    :param image_name: Docker image reference, or the base image name used to
                       construct the SIF filename in SLURM mode.
    :param container_name: Temporary container name used in Docker mode.
    :param files: Mapping of paths inside the image to local destination paths.
    :return: True if all requested files are available; otherwise False.
    """
    if not files:
        return True

    uncached_files: dict[str, str] = {}
    cache_keys: dict[str, str] = {}

    for container_file_name, local_file_name in files.items():
        cache_key = generate_cache_key(
            image_name,
            container_name,
            container_file_name,
            local_file_name,
        )
        cache_keys[container_file_name] = cache_key

        if cache.get(cache_key) is None or not os.path.isfile(local_file_name):
            uncached_files[container_file_name] = local_file_name

    if not uncached_files:
        return True

    if settings.JOB_EXECUTION_MODE == JobExecutionMode.SLURM:
        image_path = os.path.join(
            settings.SINGULARITY_DIR,
            f"{image_name}.sif",
        )

        success = copy_files_from_singularity_image(
            image_path,
            uncached_files,
        )

        if success:
            for container_file_name in uncached_files:
                cache.set(
                    cache_keys[container_file_name],
                    True,
                    timeout=None,
                )

        return success

    all_successful = True

    for container_file_name, local_file_name in uncached_files.items():
        success = copy_file_from_docker_image(
            image_name,
            container_name,
            container_file_name,
            local_file_name,
        )

        if success:
            cache.set(
                cache_keys[container_file_name],
                True,
                timeout=None,
            )
        else:
            all_successful = False

    return all_successful


def copy_file_from_image(
        image_name: str,
        container_name: str,
        container_file_name: str,
        local_file_name: str,
) -> bool:
    """
    Copy one file from a Docker or Singularity image.

    This is a convenience wrapper around ``copy_files_from_image()``.

    :param image_name: Name of the image.
    :param container_name: Temporary container name used for Docker.
    :param container_file_name: Path to the file inside the image.
    :param local_file_name: Local destination path.
    :return: True if the file is copied successfully; otherwise False.
    """
    return copy_files_from_image(
        image_name,
        container_name,
        {container_file_name: local_file_name},
    )
