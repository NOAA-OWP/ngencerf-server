import json
import logging
import os
import shutil
from functools import cache

from django.conf import settings

from calibration.enums_vanilla import NgenEnvironmentEnum
from calibration.util.container_util import copy_file_from_image, copy_file_from_docker_image
from calibration.util.file_util import copy_file

logger = logging.getLogger(__name__)


def get_git_info_internal() -> dict[str, dict[str, str]]:
    """
    Gather Git information from multiple sources, merge the JSON files, and transform each component
    so that only the desired fields are retained. The transformation rules are:
      - Always include 'commit_hash' and 'build_date'.
      - If 'tags' is non-empty, include it (renamed to 'release').
      - If 'tags' is empty, include 'branch', 'author', 'message', and 'commit_date'.

    The function performs the following steps:
      1. Clears and recreates a temporary directory (git_info) in BASE_DIR.
      2. Copies the local 'ngencerf-server_git_info.json' into this directory.
      3. For each defined image (ngen, nwm-cal-mgr, ngen-bmi-forcing, nwm-fcst-mgr), it copies its
         'git_info.json' from Docker (or Singularity) into the directory.
      4. Iterates over all JSON files in the directory and merges their contents into a single dict.
      5. Transforms each component in the merged data using transform_component().

    :return: A dictionary containing the merged and transformed Git information with the same
             top-level keys as the original merged data.
    """
    git_info_directory = os.path.join(settings.BASE_DIR, 'git_info')
    if os.path.exists(git_info_directory):
        shutil.rmtree(git_info_directory)

    os.mkdir(git_info_directory)

    # Copy our local git_info.json into the shared directory.
    src_git_info = os.path.join(settings.BASE_DIR, 'ngencerf-server_git_info.json')
    dest_git_info = os.path.join(git_info_directory, 'ngencerf-server_git_info.json')
    if os.path.exists(src_git_info):
        copy_file(src_git_info, dest_git_info)

    # Copy our local nwm-msw-mgr_git_info.json into the shared directory.
    src_git_info = os.path.join(settings.BASE_DIR, 'nwm-msw-mgr_git_info.json')
    dest_git_info = os.path.join(git_info_directory, 'nwm-msw-mgr_git_info_git_info.json')
    if os.path.exists(src_git_info):
        copy_file(src_git_info, dest_git_info)

    # For each image, copy its git_info.json into the shared directory.

    # Get ngen-bmi-forcing, ngen and cal-mgr git_info files from nwm-cal-mgr container
    image_name = 'nwm-cal-mgr'
    container_name = f'{image_name}_temp_container'
    for git_info_file in ('ngen-bmi-forcing_git_info.json', 'ngen_git_info.json', 'nwm-cal-mgr_git_info.json'):
        container_file_name = os.path.join(settings.REPO_ROOT, git_info_file)
        local_file_name = os.path.join(git_info_directory, git_info_file)
        copy_file_from_image(image_name, container_name, container_file_name, local_file_name)

    image_name = 'nwm-fcst-mgr'
    container_name = f'{image_name}_temp_container'
    container_file_name = os.path.join(settings.REPO_ROOT, f"{image_name}_git_info.json")
    local_file_name = os.path.join(git_info_directory, f"{image_name}_git_info.json")
    copy_file_from_image(image_name, container_name, container_file_name, local_file_name)

    image_name = 'nwm-verf'
    container_name = f'{image_name}_temp_container'
    container_file_name = os.path.join(settings.REPO_ROOT, f"{image_name}_git_info.json")
    local_file_name = os.path.join(git_info_directory, f"{image_name}_git_info.json")
    copy_file_from_image(image_name, container_name, container_file_name, local_file_name)

    if settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        image_name = f'ghcr.io/ngwpc/ngencerf-ui:{settings.NGENCERF_UI_TAG}'
        container_name = 'ngencerf-ui_temp_container'
        container_file_name = "/var/www/ngencerf/nuxt-app/ngencerf-ui_git_info.json"
        local_file_name = os.path.join(git_info_directory, "ngencerf-ui_git_info.json")  # ← FIX HERE
        # This will always be from docker
        copy_file_from_docker_image(image_name, container_name, container_file_name, local_file_name)
    else:
        ui_directory = os.path.join(os.path.dirname(settings.BASE_DIR), 'ngencerf-ui')
        git_info = os.path.join(ui_directory, 'ngencerf-ui_git_info.json')
        try:
            copy_file(git_info, os.path.join(git_info_directory, os.path.basename(git_info)))
        except FileNotFoundError:
            logger.warning(f'File {git_info} not found.')

    merged_data: dict[str, dict[str, str]] = {}
    # Iterate over all JSON files in the directory and merge them.
    for filename in os.listdir(git_info_directory):
        if filename.endswith('.json'):
            filepath = os.path.join(git_info_directory, filename)
            logging.info(f'Merging {filepath}')
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    # Merge data; top-level keys should be unique.
                    merged_data.update(data)
            except FileNotFoundError:
                logger.warning(f'File "{filepath}" not found')
                return {}
            except json.decoder.JSONDecodeError as e:
                logger.warning(f"Error reading {filepath}: {e}")
                return {}

    # Transform each component in merged_data so that only the desired fields are retained.
    transformed_data = {key: transform_component(value) for key, value in merged_data.items()}

    return transformed_data


def transform_component(component_git_info) -> dict[str, str]:
    """
    Transform a single component dictionary to include only selected Git fields in a specific order:
      - Always include 'release', 'build_date', and 'commit_hash' (in that order).
      - If 'tags' is empty, also include 'commit_date', 'author', and 'message' (in that order) if they exist.

    :param component_git_info: A dictionary containing Git information for a component.
    :return: A new dictionary with only the desired fields.
    """
    new_comp: dict[str, str] = {}

    tags = component_git_info.get("tags", "").strip()
    if tags == "":
        # If tags is empty, include branch, author, message, and commit_date.
        branch = f"dev ({component_git_info.get('branch', '<unknown>')})"
        new_comp["release"] = branch
    else:
        new_comp["release"] = tags

    # Insert keys in the desired order: build_date, then commit_hash.
    new_comp["build_date"] = component_git_info.get("build_date", "")
    new_comp["commit_hash"] = component_git_info.get("commit_hash", "")

    # If tags is empty, add commit_date, author, and message in order, if they exist.
    if tags == "":
        if "commit_date" in component_git_info:
            new_comp["commit_date"] = component_git_info.get("commit_date", "")
        if "author" in component_git_info:
            new_comp["author"] = component_git_info.get("author", "")
        if "message" in component_git_info:
            new_comp["message"] = component_git_info.get("message", "")

    return new_comp


def recursive_print(d: dict, indent: int = 0) -> None:
    """
    Recursively print all key/value pairs from a dictionary.

    For each key-value pair:
      - If the value is a dictionary, print the key on one line and then recurse into that dictionary.
      - If the value is a list, print the key on one line and then iterate through the list;
        for each element that is a dictionary, recurse into it; otherwise print the element on a separate line.
      - Otherwise (if the value is a string or other non-dict, non-list), print the key and value on one line.

    :param d: The dictionary to print.
    :param indent: The current indentation level (number of spaces).
    """
    for key, value in d.items():
        if isinstance(value, dict):
            logger.info(" " * indent + f"{key}:")
            recursive_print(value, indent + 2)
        elif isinstance(value, list):
            logger.info(" " * indent + f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    recursive_print(item, indent + 2)
                else:
                    logger.info(" " * (indent + 2) + str(item))
        else:
            logger.info(" " * indent + f"{key}: {value}")


GIT_INFO_FILE = 'ngencerf-server_git_info.json'


@cache
def load_git_info(git_info_file: str) -> dict[str, dict[str, str]] | None:
    """
    Load and transform Git information from a JSON file.

    This function reads Git metadata from the specified JSON file and applies a transformation
    to each top-level component to retain only relevant fields.

    Steps performed:
      1. Attempt to open and parse the JSON file.
      2. If the file does not exist, log a warning and return None.
      3. If the JSON content is malformed, log an error and return None.
      4. If the parsed content is empty, log an error and return None.
      5. Transform each component in the parsed JSON using `transform_component()`.
      6. Return the transformed Git information as a dictionary, or None on failure.

    :param git_info_file: Path to the JSON file containing Git information.
    :return: A dictionary with transformed Git metadata, or None if an error occurs.
    """
    try:
        with open(git_info_file, 'r') as f:
            git_info = json.load(f)
    except FileNotFoundError:
        logger.warning(f'{git_info_file} not found')
        return None
    except json.decoder.JSONDecodeError as e:
        logger.warning(f"Error reading {git_info_file}: {e}")
        return None

    if not git_info:
        logger.error(f"Failed to retrieve git information from {git_info_file}.")
        return None

    # Transform each top-level component without removing the keys.
    transformed_git_info = {key: transform_component(value) for key, value in git_info.items()}

    return transformed_git_info


def print_git_info(git_info_file: str) -> None:
    """
    Read the specified git_info JSON file, transform its contents, and log all key/value pairs recursively.

    The output will print top-level keys.

    :param git_info_file: Path to the JSON file containing Git information.
    """
    git_info = load_git_info(git_info_file)

    if git_info:
        recursive_print(git_info)


def print_git_info_all() -> None:
    """
    Convenience function to print Git information from multiple JSON files.
    """

    print_git_info(GIT_INFO_FILE)
    logger.info(' ')
