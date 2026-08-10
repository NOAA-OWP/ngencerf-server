import hashlib
import logging
import os
import shutil
import subprocess
import shlex
import uuid

from django.conf import settings
from django.core.cache import cache

from calibration.enums_vanilla import NgenEnvironmentEnum

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
    Copies a file from a Docker image using a temporary container and ensures cleanup.
      - Automatically appends a unique suffix to avoid name collisions.
      - Best-effort stale container cleanup for the base prefix.
      - Preserves all existing logging and structure.

    :param image_name: Name of the Docker image.
    :param container_name: Temporary container name.
    :param src_path: Path to the file inside the container.
    :param dest_path: Destination path on the host system.
    :return: True if the copy succeeds, False otherwise.
    """
    success = False  # Default to failure

    # Add a guaranteed-unique suffix to avoid container name collisions
    unique_name = f"{container_name}_{uuid.uuid4().hex[:8]}"

    # Clean stale containers based on prefix (best effort)
    # IMPORTANT: match the UUID-suffixed names
    try:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={container_name}_", "-q"],
            capture_output=True, text=True
        )
        stale_ids = [c.strip() for c in ps.stdout.splitlines() if c.strip()]
        for cid in stale_ids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
    except Exception as e:
        logger.warning(f"Failed to clean stale containers for prefix {container_name}: {e}")

    # Basic sanity checks
    if shutil.which("docker") is None:
        logger.error("Docker binary not found on PATH.")
        return False

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
            subprocess.run(["docker", "rm", "-f", unique_name], capture_output=True, text=True)
        except Exception:
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
        # Step 3: Remove the temporary container (always runs, even if copy fails)
        rm_cmd = ["docker", "rm", "-f", unique_name]
        rm_response = subprocess.run(rm_cmd, capture_output=True, text=True)
        if rm_response.returncode != 0:
            logger.warning(
                "Failed to remove temporary container "
                f"(exit={rm_response.returncode}).\n"
                f"Command: {' '.join(rm_cmd)}\n"
                f"STDOUT:\n{_indent_output((rm_response.stdout or '').strip())}\n"
                f"STDERR:\n{_indent_output((rm_response.stderr or '').strip())}"
            )
        else:
            logger.debug(f"Removed temporary container {unique_name}")

    return success


def copy_file_from_singularity_image(image_path: str, src_path: str, dest_path: str) -> bool:
    """
    Copies a file from a Singularity image (.sif) using `singularity exec`.

    :param image_path: Path to the Singularity image file (.sif)
    :param src_path: Path to the file inside the container
    :param dest_path: Destination path on the host system
    :return: True if the copy succeeds, False otherwise
    """
    success = False  # Default to failure

    logger.info(f'Copy file {src_path} from image {image_path}')

    # Quick checks that commonly cause exit=1
    if shutil.which("singularity") is None:
        logger.error("singularity binary not found on PATH.")
        return False

    # Check if the path exists
    if not os.path.exists(image_path):
        # If it's a symlink, check whether it's broken
        if os.path.islink(image_path):
            target = os.readlink(image_path)
            logger.error(f"Image path {image_path} is a symlink to {target}, but the target does not exist.")
        else:
            logger.error(f"Image {image_path} does not exist.")
        return False

    dest_parent = os.path.dirname(dest_path) or "."
    if not os.path.isdir(dest_parent):
        logger.error(f"Destination directory does not exist on host: {dest_parent}")
        return False

    # IMPORTANT: Bind the host dest directory at the same absolute path so `cp` can write to it.
    # We also run through /bin/sh -lc so we can use simple quoting safely.
    quoted_src = shlex.quote(src_path)
    quoted_dst = shlex.quote(dest_path)
    shell_cmd = f"cp {quoted_src} {quoted_dst}"

    copy_cmd = [
        "singularity", "exec",
        "--bind", f"{dest_parent}:{dest_parent}",
        image_path,
        "/bin/sh", "-lc", shell_cmd,
    ]
    logger.debug(copy_cmd)
    try:
        res = subprocess.run(copy_cmd, check=True, capture_output=True, text=True)
        if res.stdout:
            logger.debug(f"[singularity exec cp stdout]\n{_indent_output(res.stdout.strip())}")
        if res.stderr:
            # Some singularity builds are chatty on stderr; still capture it
            logger.debug(f"[singularity exec cp stderr]\n{_indent_output(res.stderr.strip())}")
        logger.info(f"Successfully copied {src_path} from {image_path} to {dest_path}")
        success = True
    except subprocess.CalledProcessError as e:
        # Provide maximum context for troubleshooting bind vs. path vs. perms
        logger.error(
            "Error copying file from Singularity image "
            f"(exit={e.returncode}).\n"
            f"Command: {' '.join(copy_cmd)}\n"
            f"STDOUT:\n{_indent_output((e.stdout or '').strip())}\n"
            f"STDERR:\n{_indent_output((e.stderr or '').strip())}"
        )

        logger.error(
            "If STDERR shows 'No such file or directory' for the destination, "
            "ensure the host path is bind-mounted into the container context. "
            "If it shows 'No such file or directory' for the source, verify the path inside the image. "
            "If 'Permission denied', check file/dir permissions and container user."
        )
    return success


def generate_cache_key(image_name: str, container_name: str, container_file_name: str, local_file_name: str) -> str:
    """
    Generates a unique cache key for the file copy operation based on input parameters.

    :param image_name: Name of the image.
    :param container_name: Name of the container.
    :param container_file_name: Path to the file inside the container.
    :param local_file_name: Local destination file name.
    :return: A unique cache key string.
    """
    key_string = f"{container_name}:{container_file_name}:{image_name}:{local_file_name}"
    return "copy_file:" + hashlib.md5(key_string.encode()).hexdigest()


def copy_file_from_image(image_name: str, container_name: str, container_file_name: str, local_file_name: str) -> bool:
    """
    Copies a file from an image (Docker or Singularity) based on the current environment,
    with caching for successful operations.

    The function first checks if the copy operation was successfully cached.
    If not cached, it performs the copy operation and caches the success result.

    :param image_name: Name of the image.
    :param container_name: Name of the container (used for Docker).
    :param container_file_name: Path to the file inside the container.
    :param local_file_name: Local destination file name.
    :return: True if the file copy was successful, False otherwise.
    """
    cache_key = generate_cache_key(image_name, container_name, container_file_name, local_file_name)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # Return cached success status

    # Perform the copy operation based on the environment.
    if settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        success = copy_file_from_singularity_image(
            os.path.join(settings.SINGULARITY_DIR, f'{image_name}.sif'),
            container_file_name,
            local_file_name
        )
    else:
        success = copy_file_from_docker_image(
            image_name,
            container_name,
            container_file_name,
            local_file_name
        )
    # Only cache if the operation was successful
    if success:
        cache.set(cache_key, success, timeout=0)

    return success
