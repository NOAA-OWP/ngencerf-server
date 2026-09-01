"""
Module providing CLI functionality for interacting with ngen calibration job endpoints.
Supports operations like submitting/deleting/cancelling jobs, and
importing/exporting configurations.
"""
import itertools
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from typing import Callable, Any

import requests
import tabulate

from ngencerf.cli_config import get_ngencerf_base_url
from ngencerf.cli_legacy_conversion import convert_legacy_job_data, save_converted_job_data
from ngencerf.cli_util import check_http_error


def _get_bundled_cli_git_info() -> dict:
    """
    Return CLI git info bundled into the PyInstaller executable.
    """
    base_path = str(getattr(sys, "_MEIPASS", os.path.dirname(__file__)))
    git_info_path = os.path.join(base_path, "ngencerf", "git_info.json")

    try:
        with open(git_info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {
            "ngencerf-cli": {
                "commit_hash": "unknown",
                "branch": "<unknown>",
                "tags": "",
                "build_date": "unknown",
                "commit_date": "unknown",
                "author": "unknown",
                "message": f"Unable to read embedded CLI git info: {e}",
            }
        }


def post_with_spinner_and_retry(message: str, endpoint: str, **kwargs) -> tuple[requests.Response | dict | None, bool]:
    """
    POST to an API endpoint with spinner, automatic token refresh/relogin, and retry support.

    Key Features:
    - Always injects a *fresh* Authorization header (ACCESS_TOKEN from environment) on each attempt.
    - Automatically retries once if a 401 Unauthorized occurs and token refresh/login succeeds.
    - Rewinds any file handles before retrying to allow clean re-upload.
    - Gracefully handles KeyboardInterrupt (Ctrl-C) to stop spinner without tracebacks.
    - Supports both standard JSON responses and streamed binary downloads.

    Behavior Summary:
    - On first attempt:
        - Shows spinner while waiting.
        - If success:
            - Returns (Response, True) for `stream=True`, or (parsed_json, True) for normal requests.
        - If 401: triggers token refresh or login, then retries once.
    - On retry:
        - Rebuilds headers and file handles.
        - If retry succeeds → returns the same as above.
        - If still fails → prints error via check_http_error() and returns (None, False).

    :param message: Message to display while waiting.
    :param endpoint: API endpoint path relative to the configured ngenCerf base URL.
    :param kwargs: Forwarded to `requests.post` (headers, json, files, stream, etc.)
    :return: (Response|dict|None, bool)
             - Response (if streaming), dict (if JSON), or None (if failed).
             - Success flag True if request ultimately succeeded.
    """

    def _rewind_files(files_obj):
        # Ensures any open file objects are rewound to the start before retrying,
        # so file uploads (e.g., .gpkg, .csv) can be resent cleanly.
        # Handles both dict and list formats produced by `requests`.
        if isinstance(files_obj, dict):
            for v in files_obj.values():
                try:
                    # (filename, fileobj) tuple
                    if isinstance(v, tuple) and len(v) >= 2 and hasattr(v[1], "seek"):
                        v[1].seek(0)
                    elif hasattr(v, "seek"):
                        v.seek(0)
                except Exception:
                    pass
        elif isinstance(files_obj, list):
            for item in files_obj:
                try:
                    # ("files", (filename, fileobj)) or similar
                    if isinstance(item, tuple) and len(item) >= 2:
                        inner = item[1]
                        if isinstance(inner, tuple) and len(inner) >= 2 and hasattr(inner[1], "seek"):
                            inner[1].seek(0)
                except Exception:
                    pass

    def _make_post() -> requests.Response | None:
        # Prepare kwargs for each attempt — never reuse mutated objects.
        req_kwargs = dict(kwargs)

        # Inject a new Authorization header for each retry attempt.
        hdrs = dict(req_kwargs.get("headers") or {})
        # Always overwrite Authorization header with current token
        hdrs["Authorization"] = f"Bearer {os.environ.get('ACCESS_TOKEN', '')}"
        req_kwargs["headers"] = hdrs

        # Rewind files to start (important for retries with uploads)
        if "files" in req_kwargs and req_kwargs["files"]:
            _rewind_files(req_kwargs["files"])

        request_base_url = get_ngencerf_base_url()
        try:
            return requests.post(f"{request_base_url}{endpoint}", **req_kwargs)
        except requests.exceptions.RequestException as e:
            print(f"\nError: Could not connect to server at {request_base_url}.")
            print(f"Details: {e}")
            return None

    def _with_spinner(msg: str, fn: Callable[[], requests.Response | None]) -> requests.Response | None:
        # Wrapper that runs a function while showing an animated spinner.
        # Always stops spinner, even on Ctrl-C or exception.
        sp = Spinner(msg)
        sp.start()
        try:
            return fn()
        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")
            return None
        finally:
            if sp.running:
                sp.stop()

    is_stream = bool(kwargs.get("stream"))

    # ───────────────────────────────
    # 1. First attempt (with spinner)
    # ───────────────────────────────
    base_url = get_ngencerf_base_url()
    first_resp = _with_spinner(f"{message} | Server: {base_url}...", _make_post)
    if first_resp is None:
        return None, False

    # If the first attempt succeeded and it’s a stream (ZIP download, etc.), return raw response.
    if is_stream and first_resp.ok:
        # Success on first try → return raw Response for streaming
        return first_resp, True

    # ───────────────────────────────
    # 2. Handle response and decide on retry
    # ───────────────────────────────
    parsed_or_none, ok = check_http_error(
        first_resp.status_code,
        first_resp.text,
        first_resp.url,
        first_resp.headers.get("Content-Type")
    )

    # Retry only if 401 and refresh/login succeeded
    if not ok and first_resp.status_code == 401:
        print("Retrying request after authentication recovery.")
        retry_resp = _with_spinner(f"Retrying: {message} | Server: {base_url}...", _make_post)
        if retry_resp is None:
            return None, False

        # ───────────────────────────────────────────
        # Handle retry result (whether success or error)
        # ───────────────────────────────────────────
        if retry_resp.ok:
            if is_stream:
                return retry_resp, True
            try:
                return retry_resp.json(), True
            except Exception:
                # Handle rare case: 200 OK but empty body (no JSON)
                return None, True

        else:
            # If retry still fails, handle error exactly like the first attempt.
            # This ensures 400/500 responses after token refresh are visible to the user.
            _ = check_http_error(
                retry_resp.status_code,
                retry_resp.text,
                retry_resp.url,
                retry_resp.headers.get("Content-Type")
            )
            return None, False

    # If still failed, abort cleanly
    if not ok:
        return None, False

    # ───────────────────────────────
    # 3. Successful JSON/non-stream response
    # ───────────────────────────────
    return parsed_or_none, True


def about(output_path: str | None = None) -> int:
    """
    Fetch and display git information from the calibration server in a formatted manner.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    # Resolve and validate output path before contacting the server
    final_path = resolve_output_path(output_path, "about_ngencerf.json")

    response_json, success = post_with_spinner_and_retry(
        "Fetching ngenCerf about information",
        "/calibration/get_git_info/",
        headers={"Content-Type": "application/json"}
    )
    if not success or not isinstance(response_json, dict):
        return 1

    cli_git_info = _get_transformed_bundled_cli_git_info()

    if "git_info" not in response_json or not isinstance(response_json["git_info"], dict):
        response_json["git_info"] = {}

    response_json["git_info"].update(cli_git_info)

    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(response_json, f, indent=2)

    print(f"ngenCerf 'about' info saved to {final_path}")
    return 0


def version() -> int:
    """
    Display local CLI version/build information without contacting the server.

    Returns:
        int: Exit code 0.
    """
    cli_git_info = _get_transformed_bundled_cli_git_info()

    print(json.dumps(cli_git_info, indent=2))
    return 0


def download_zip(calibration_run_id: int, output_path: str | None = None) -> int:
    """
    Downloads the ZIP archive for a calibration run from the server.

    :param calibration_run_id: ID of the calibration run to download.
    :param output_path: Path to save the ZIP file or directory.  If not provided, defaults to the current working directory.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Downloading ZIP for calibration run: {calibration_run_id}")

    # Resolve and validate path early
    default_name = f"calibration_job_{calibration_run_id}.zip"
    final_path = resolve_output_path(output_path, default_name)

    payload = {"calibration_run_id": calibration_run_id}
    resp, success = post_with_spinner_and_retry(
        "Downloading ZIP",
        "/calibration/get_calibration_job_zip/",
        headers={},  # must remain blank to allow auto-injection
        json=payload,
        stream=True,
    )
    if not success or not isinstance(resp, requests.Response):
        return 1

    # If server sends Content-Disposition, honor the filename
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        name = cd.split("filename=", 1)[1].strip().strip('"')
        final_path = resolve_output_path(output_path, name or default_name)

    try:
        with open(final_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        print(f"Failed to write ZIP to {final_path}: {e}")
        return 1

    print(f"Downloaded ZIP to: {final_path}")
    return 0


def run_job(calibration_run_id: int) -> int:
    """
    Submits a calibration run for execution.

    :param calibration_run_id: ID of the calibration run.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Submitting calibration run job {calibration_run_id}")
    payload = {"calibration_run_id": calibration_run_id}
    response_json, success = post_with_spinner_and_retry(
        "Submitting job",
        "/calibration/run_calibration/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1
    if message := response_json.get("message"):
        print(message)
    warnings = response_json.get("warnings")
    if isinstance(warnings, list):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")

    return 0


def job_status(calibration_run_id: int) -> int:
    """
    Display status for a calibration job and related jobs.

    :param calibration_run_id: ID of the calibration run.
    :returns: 0 on success, 1 on failure.
    """
    payload = {"calibration_run_id": calibration_run_id}
    response_json, success = post_with_spinner_and_retry(
        "Getting job status",
        "/calibration/get_status/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1

    # Display top-level fields first (excluding validations and forecasts)
    print("\nCalibration Job Info:")
    top_level = {
        k: v for k, v in response_json.items()
        if k not in ("validations", "forecasts")
    }
    print(json.dumps(top_level, indent=2))

    # Display validations, if present
    validations = response_json.get("validations")
    if isinstance(validations, list):
        print("\nValidations:")
        for v in validations:
            print(json.dumps(v, indent=2))

    # Display forecasts, if present
    forecasts = response_json.get("forecasts")
    if isinstance(forecasts, list):
        print("\nForecasts:")
        for f in forecasts:
            print(json.dumps(f, indent=2))

    return 0


def delete_job(calibration_run_ids: list[int] | str) -> int:
    """
    Deletes one or more calibration runs, with confirmation and
    automatic pre-display for single-job deletions.
    Accepts either:
      - A list of job IDs
      - A Markdown file generated by list_jobs() (extracts Run IDs automatically)

    :param calibration_run_ids: A list of one or more calibration run IDs or path to a Markdown file.
    :returns: 0 on success, 1 on failure.
    """
    return _process_job_action(
        "Deleting",
        "/calibration/delete_jobs/",
        calibration_run_ids,
        require_confirmation=True,
        confirm_keyword="delete",
        pre_display_func=handle_export_display,  # show job before deletion
    )


def archive_job(calibration_run_ids: list[int] | str) -> int:
    """
    Archives one or more calibration runs.
    Accepts either:
      - A list of job IDs
      - A Markdown file generated by list_jobs() (extracts Run IDs automatically)

    :param calibration_run_ids: A list of one or more calibration run IDs or path to a Markdown file.
    :return: 0 on success, 1 on failure.
    """
    return _process_job_action(
        "Archiving",
        "/calibration/archive_jobs/",
        calibration_run_ids,
        payload_extras={"archive": True},
    )


def unarchive_job(calibration_run_ids: list[int] | str) -> int:
    """
    Unarchives one or more calibration runs.
    Accepts either:
      - A list of job IDs
      - A Markdown file generated by list_jobs() (extracts Run IDs automatically)

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    return _process_job_action(
        "Unarchiving",
        "/calibration/archive_jobs/",
        calibration_run_ids,
        payload_extras={"archive": False},
    )


def lock_job(calibration_run_ids: list[int] | str) -> int:
    """
    Locks one or more calibration runs.
        Accepts either:
      - A list of job IDs
      - A Markdown file generated by list_jobs() (extracts Run IDs automatically)

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    return _process_job_action(
        "Locking",
        "/calibration/lock_jobs/",
        calibration_run_ids,
        payload_extras={"lock": True},
    )


def unlock_job(calibration_run_ids: list[int] | str) -> int:
    """
    Unlocks one or more calibration runs.
      - A list of job IDs
      - A Markdown file generated by list_jobs() (extracts Run IDs automatically)

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    return _process_job_action(
        "Unlocking",
        "/calibration/lock_jobs/",
        calibration_run_ids,
        payload_extras={"lock": False},
    )


def cancel_job(calibration_run_id: int) -> int:
    """
    Cancels a running calibration job.

    :param calibration_run_id: ID of the calibration run
    :return: 0 on success, 1 on failure
    """
    print(f"Cancelling calibration run job {calibration_run_id}")
    payload = {"calibration_run_id": calibration_run_id}
    response_json, success = post_with_spinner_and_retry(
        "Cancelling job",
        "/calibration/cancel_job/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1
    if message := response_json.get("message"):
        print(message)
    warnings = response_json.get("warnings")
    if isinstance(warnings, list):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")

    return 0


def list_jobs(output_path: str | None = None, filters: dict | None = None, sort: dict | None = None) -> int:
    """
    Lists calibration jobs from the server with optional filtering and sorting,
    and saves the results to a Markdown file.

    This function powers the `ngencerf jobs` CLI command.
    It builds the payload for `/calibration/get_calibration_jobs/`
    from the provided filters and sort dictionaries,
    sends the API request, and writes the formatted job table to disk.

    Example payload:
        {
            "filters": {
                "gage_id": "01544887",
                "status": ["Done", "Failed"],
                "module_filter": {
                    "operator": "and",
                    "modules": ["CFE-X", "Noah-OWP-Modular"]
                },
                "include_archived": false
            },
            "sort": { "field": "submit_date", "direction": "desc" }
        }

    :param output_path: Optional path to save the job list as a Markdown file.
    :param filters: Parsed filter dictionary (from YAML/JSON or CLI flags).
    :param sort: Parsed sort dictionary (from YAML/JSON or CLI flags).
    :return: 0 on success, 1 on failure.
    """
    # ───────────────────────────────
    # Resolve and validate output path
    # ───────────────────────────────
    final_path = resolve_output_path(
        output_path,
        f"calibration_jobs_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
    )

    # ───────────────────────────────
    # Construct payload
    # ───────────────────────────────
    payload: dict[str, Any] = {"include_modules": True}
    if filters:
        payload["filters"] = filters
    if sort:
        payload["sort"] = sort

    print(f"\nFetching job list with filters:\n{json.dumps(filters or {}, indent=2)}")
    if sort:
        print(f"Sorting:\n{json.dumps(sort, indent=2)}")

    # ─────────────────────────────────────────────
    # Perform API call
    # ─────────────────────────────────────────────
    response_json, success = post_with_spinner_and_retry(
        "Fetching job list",
        "/calibration/get_calibration_jobs/",
        headers={"Content-Type": "application/json"},
        json=payload
    )
    if not success or not isinstance(response_json, dict):
        return 1

    jobs = response_json.get("jobs", [])
    if not isinstance(jobs, list) or not jobs:
        print("No jobs found.")
        return 0

    # ─────────────────────────────────────────────
    # Build Markdown table
    # ─────────────────────────────────────────────
    rows = []
    for job in jobs:
        rows.append([
            job.get("calibration_run_id"),
            job.get("gage_id") or "-",
            job.get("status") or "-",
            (job.get("calibration_start_period") or "-").replace("T", " ").split(".")[0],
            (job.get("calibration_end_period") or "-").replace("T", " ").split(".")[0],
            job.get("job_name") or "-",
            job.get("objective_function") or "-",
            job.get("optimization_algorithm") or "-",
            (job.get("created_at") or "-").replace("T", " ").split(".")[0],
            (job.get("last_updated_on") or "-").replace("T", " ").split(".")[0],
            (job.get("submit_date") or "-").replace("T", " ").split(".")[0],
            "yes" if job.get("is_archived") else "no",
            "yes" if job.get("is_locked") else "no",
            ", ".join(job.get("modules", []))
        ])

    headers = [
        "Run ID", "Gage", "Status", "Start", "End",
        "Formulation", "Objective", "Optimization",
        "Created", "Last Updated", "Submitted",
        "Archived", "Locked", "Modules"
    ]

    markdown_table = tabulate.tabulate(rows, headers=headers, tablefmt="github")

    with open(final_path, "w", encoding="utf-8") as f:
        f.write(markdown_table)

    print(f"Saved {len(rows)} jobs to {final_path}")
    return 0


def update_and_get_gage_status(gage_id: str, is_active: bool | None = None) -> int:
    """
    Update (or query) the cached gage status through the API.

    :param gage_id: The gage ID
    :param is_active: Desired state (True/False) or None to just query
    :return: 0 on success, 1 on failure
    """
    payload: dict[str, str | bool] = {"gage_id": gage_id}
    if is_active is not None:
        payload["is_active"] = is_active

    response_json, success = post_with_spinner_and_retry(
        "Updating gage status",
        "/calibration/update_and_get_gage_status/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1

    message = response_json.get("message", "")
    gage_id = response_json.get("gage_id")
    is_active = response_json.get("is_active")

    print(message or f"Gage {gage_id} is {'active' if is_active else 'not active'}")
    return 0


def _submit_job_data(
        job_file: str,
        action: str,
        calibration_run_id: int | None = None,
        run_after_import: bool | None = None
) -> int:
    """
    Submits job data to the import or update endpoint.

    Legacy job files containing top-level calibration_times and
    validation_times, but no time_controls, are converted to the current
    format before being sent to the server. The converted JSON is also
    saved beside the original file.

    :param job_file: Path to the JSON file
    :param action: Description of the action being performed
    :param calibration_run_id: Optional calibration_run_id for update
    :param run_after_import: Optional override for the run_after_import field
    :return: 0 on success, 1 on failure
    """
    # Load the JSON file
    try:
        with open(job_file, "r", encoding="utf-8") as f:
            job_data = json.load(f)
    except FileNotFoundError:
        print(f"{job_file} does not exist")
        return 1
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON file {job_file}: {e}")
        return 1

    if not isinstance(job_data, dict):
        print(
            f"Error: The root value in {job_file} must be a JSON object"
        )
        return 1

    # Convert legacy time fields to the current import format.
    try:
        job_data, conversion_messages = convert_legacy_job_data(
            job_data
        )
    except ValueError as e:
        print(f"Error converting legacy job file: {e}")
        return 1

    if conversion_messages:
        print(
            "Legacy calibration job format detected. "
            "The file will be converted before import:"
        )
        for message in conversion_messages:
            print(f"  - {message}")

    # Override the run_after_import field if specified
    if run_after_import is not None:
        print(f"Overriding run_after_import: {run_after_import}")
        job_data["run_after_import"] = run_after_import

    # Save the transformed JSON beside the original legacy file.
    if conversion_messages:
        try:
            converted_path = save_converted_job_data(
                job_file,
                job_data,
            )
        except OSError as e:
            print(f"Error saving converted job file: {e}")
            return 1

        print(f"Converted job saved to: {converted_path}\n")

    # Build the payload
    payload: dict[str, Any] = {"data": job_data}
    if calibration_run_id is not None:
        payload["calibration_run_id"] = calibration_run_id

    response_json, success = post_with_spinner_and_retry(
        f"{action} job",
        "/calibration/import/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1

    # Print top-level message
    if message := response_json.get("message"):
        print(message)

    # Collect errors into one list
    combined_errors = []
    combined_warnings = []
    info_messages = []

    # Nested messages block
    messages = response_json.get("messages")
    if isinstance(messages, dict):
        errors = messages.get("errors", [])
        if isinstance(errors, list):
            combined_errors.extend(errors)

        eds_errors = messages.get("eds_errors", [])
        if isinstance(eds_errors, list):
            combined_errors.extend(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in eds_errors
            )

        warnings = messages.get("warnings", [])
        if isinstance(warnings, list):
            combined_warnings.extend(warnings)

        info = messages.get("info", [])
        if isinstance(info, list):
            info_messages.extend(info)

    # Top-level blocks
    errors = response_json.get("errors")
    if isinstance(errors, list):
        combined_errors.extend(errors)

    warnings = response_json.get("warnings")
    if isinstance(warnings, list):
        combined_warnings.extend(warnings)

    # Print all collected errors and warnings
    if combined_errors:
        print("Errors:")
        for error in combined_errors:
            print("  ", error)

    if combined_warnings:
        print("Warnings:")
        for warning in combined_warnings:
            print("  ", warning)

    if info_messages:
        print("Info:")
        for message in info_messages:
            print("  ", message)

    return 0


def import_job(job_file: str, run_after_import: bool | None = None) -> int:
    """
    Imports a new job definition from a JSON file.

    :param job_file: Path to the JSON file
    :param run_after_import: Optional override for the run_after_import field
    :return: 0 on success, 1 on failure
    """
    print(f"Importing job from: {job_file}")
    return _submit_job_data(job_file, action='Importing', run_after_import=run_after_import)


def update_job(calibration_run_id: int, job_file: str, run_after_update: bool | None = None) -> int:
    """
    Updates an existing calibration job using a JSON file.

    :param calibration_run_id: ID of the calibration run
    :param job_file: Path to the JSON file
    :param run_after_update: Optional override for the run_after_import field
    :return: 0 on success, 1 on failure
    """
    print(f"Updating job {calibration_run_id} from: {job_file}")
    return _submit_job_data(job_file, action='Updating', calibration_run_id=calibration_run_id, run_after_import=run_after_update)


def handle_export_display(calibration_run_id: int, output_path: str | None = None, display: bool = False) -> int:
    """
    Exports a calibration job to a file or displays it.

    :param calibration_run_id: ID of the calibration run to export
    :param output_path: Path to save the export file. If not provided, defaults to the current working directory.
    :param display: Whether to print the job to the console
    :return: 0 on success, 1 on failure
    """
    # Resolve and validate output file early
    final_path = resolve_output_path(output_path, f"export_{calibration_run_id}.json")

    payload = {"calibration_run_id": calibration_run_id}
    response_json, success = post_with_spinner_and_retry(
        "Fetching job",
        "/calibration/export/",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1

    if display:
        _pretty_print_job(calibration_run_id, response_json)

    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(response_json, f, indent=2)

    print(f"Job {calibration_run_id} exported to {final_path}")
    return 0


def generate_regionalization_files(calibration_run_ids: list[int] | str, output_path: str | None = None) -> int:
    """
    Triggers ZIP file generation for regionalization and saves contents to output_path.

    :param calibration_run_ids: List of calibration run IDs or a path to a file containing them.
    :param output_path: Directory to unzip files into. If None, current working directory is used.
    :return: 0 on success, 1 on failure.
    """
    # Allow file input
    if isinstance(calibration_run_ids, str):
        try:
            with open(calibration_run_ids, "r") as f:
                contents = f.read()
            # Support space/comma/line-separated values
            calibration_run_ids = [int(x) for x in contents.replace(",", " ").split()]
        except Exception as e:
            print(f"Failed to read calibration run IDs from file: {e}")
            return 1

    print(f"Generating regionalization files for calibration run jobs {calibration_run_ids}")
    payload = {"calibration_run_ids": calibration_run_ids}

    # Pre-resolve final output directory before any network calls
    final_dir = os.path.dirname(resolve_output_path(output_path, "regionalization_files.zip"))

    # Create a temporary directory for downloading the ZIP
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "regionalization_files.zip")

        # Perform request (with automatic refresh/retry)
        resp, success = post_with_spinner_and_retry(
            "Downloading regionalization ZIP",
            "/calibration/get_regionalization_files_zip/",
            headers={},  # No static Authorization header
            json=payload,
            stream=True,
        )
        if not success or not isinstance(resp, requests.Response):
            return 1

        # Save ZIP to temp path
        try:
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            print(f"Failed to write ZIP file: {e}")
            return 1

        # Extract ZIP contents to output directory
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(final_dir)
        except zipfile.BadZipFile:
            print("Error: The downloaded file is not a valid ZIP archive.")
            return 1

        print(f"Unzipped regionalization files to: {final_dir}")
        return 0


def _pretty_print_job(calibration_run_id: int, data: dict) -> None:
    """
    Prints selected fields from the exported calibration job in a structured format.

    :param calibration_run_id: ID of the calibration run.
    :param data: Exported job data.
    """

    def fmt(dt: str | None) -> str:
        """
        Formats an ISO timestamp string in GMT (UTC) to 'YYYY-MM-DD HH:MM'.

        Handles timestamps ending in Z or containing a UTC offset.

        :param dt: ISO timestamp string.
        :return: Formatted timestamp, or "-" when unset.
        """
        if not dt:
            return "-"

        normalized = dt.replace("Z", "+00:00")

        try:
            return datetime.fromisoformat(normalized).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return dt  # fallback: return original if parsing fails

    def fmt_months(value: int | None) -> str:
        """
        Format a duration expressed in months.

        :param value: Number of months.
        :return: Formatted month duration, or "-" when unset.
        """
        if value is None:
            return "-"

        return f"{value} month{'s' if value != 1 else ''}"

    metadata = data.get("metadata", {})
    calibration_times = metadata.get("calibration_times", {})
    validation_times = metadata.get("validation_times", {})
    time_controls = data.get("time_controls", {})

    print()
    print(
        f"Calibration Job ID "
        f"{metadata.get('source_calibration_run_id', calibration_run_id)}"
    )
    print(f"Status: {metadata.get('source_status')}")
    print(f"Job Data directory: {metadata.get('job_data_dir')}")
    print(f"Gage: {data.get('gage_id')}")
    print(f"Catchments: {metadata.get('num_catchments', '-')}")
    print(f"Forcing Source: {data.get('forcing_source')}")
    print(f"Observational Source: {data.get('observational_source')}")
    print(f"Geopackage Source: {data.get('geopackage_source')}")
    print(data.get("description", "").strip())
    print()

    print(f"Job Name: {data.get('job_name')}")
    print(f"Modules: {', '.join(data.get('modules', []))}")
    print()

    print(f"{'Calibration Run':<50}{'Validation Run'}")
    print(
        f"{'Sim Start:':<25}"
        f"{fmt(calibration_times.get('simulation_start_time')):<25}"
        f"Sim Start: {fmt(validation_times.get('simulation_start_time'))}"
    )
    print(
        f"{'Sim End:':<25}"
        f"{fmt(calibration_times.get('simulation_end_time')):<25}"
        f"Sim End:   {fmt(validation_times.get('simulation_end_time'))}"
    )
    print(
        f"{'Calib Start:':<25}"
        f"{fmt(calibration_times.get('calibration_start_time')):<25}"
        f"Val Start: {fmt(validation_times.get('validation_start_time'))}"
    )
    print(
        f"{'Calib End:':<25}"
        f"{fmt(calibration_times.get('calibration_end_time')):<25}"
        f"Val End:   {fmt(validation_times.get('validation_end_time'))}"
    )
    print()

    validation_after = time_controls.get(
        "validation_window_after_calibration"
    )

    if validation_after is True:
        validation_position = "After calibration"
    elif validation_after is False:
        validation_position = "Before calibration"
    else:
        validation_position = "-"

    print("Time Controls:")
    print(
        f"  Simulation Start: "
        f"{fmt(time_controls.get('simulation_start_time'))}"
    )
    print(
        f"  Warmup Duration: "
        f"{fmt_months(time_controls.get('warmup_duration'))}"
    )
    print(
        f"  Calibration Duration: "
        f"{fmt_months(time_controls.get('calibration_duration'))}"
    )
    print(
        f"  Validation Window Gap: "
        f"{fmt_months(time_controls.get('validation_window_gap'))}"
    )
    print(f"  Validation Window: {validation_position}")
    print(
        f"  Validation Duration: "
        f"{fmt_months(time_controls.get('validation_duration'))}"
    )
    print()

    print(f"Optimization Algorithm: {data.get('optimization')}")
    print(f"Objective Function: {data.get('objective_function')}")
    print(
        f"Plot Generation Frequency: "
        f"{data.get('save_plot_iteration_frequency')}"
    )
    print()

    print(f"Tuning Parameters: {len(data.get('parameters', []))}")
    print(f"Calibration Stop Criteria: {data.get('stop_criteria')}")
    print()


def resolve_output_path(output_path: str | None, default_filename: str) -> str:
    """
    Resolves the final output path for a file, handling directory, relative, and full file paths.
    Verifies that the resulting directory is writable by the current user before returning it.

    :param output_path: The provided output path, which can be a directory, relative file name, or full file path.
    :param default_filename: The default filename to use if output_path is a directory or filename without a path.
    :return: The resolved full file path.
    :raises SystemExit: If the output directory is not writable.
    """
    # Determine the base directory
    if output_path is None or output_path == "__DEFAULT__":
        base_dir = os.getcwd()
        output_path = os.path.join(base_dir, default_filename)
    else:
        # Expand user and environment variables
        output_path = os.path.expanduser(os.path.expandvars(output_path))

        # If output_path is a directory, append the default filename
        if os.path.isdir(output_path) or output_path.endswith(os.sep):
            os.makedirs(output_path, exist_ok=True)
            output_path = os.path.join(output_path, default_filename)
        else:
            # If output_path is just a filename, prepend current working directory
            dir_name = os.path.dirname(output_path)
            if not dir_name:
                output_path = os.path.join(os.getcwd(), output_path)
            else:
                # Ensure the directory exists for the specified file path
                os.makedirs(dir_name, exist_ok=True)

    if os.path.exists(output_path):
        # File exists → check if user can write to it
        if not os.access(output_path, os.W_OK):
            print(f"Error: File '{output_path}' is not writable by the current user.")
            sys.exit(1)
    else:
        # File doesn't exist → check parent directory instead
        parent_dir = os.path.dirname(output_path) or os.getcwd()
        if not os.access(parent_dir, os.W_OK):
            print(f"Error: Directory '{parent_dir}' is not writable by the current user.")
            sys.exit(1)

    return output_path


def _normalize_job_ids(input_value: list[int] | str) -> list[int] | None:
    """
    Normalizes input into a list of job IDs.
    Accepts either:
      - a list of integers
      - a numeric string (e.g. "123")
      - a Markdown file path (.md) created by list_jobs()

    :param input_value: List of IDs, a single numeric string, or path to a Markdown file (.md)
    :return: List of integer job IDs, or None if invalid/empty
    """
    # Single numeric string → treat as single job ID
    if isinstance(input_value, str):
        if input_value.isdigit():
            return [int(input_value)]

        if not os.path.isfile(input_value):
            print(f"File not found: {input_value}")
            return None

        if input_value.endswith(".md"):
            ids = extract_job_ids_from_markdown(input_value)
            if ids:
                print(f"Loaded {len(ids)} job IDs from {input_value}")
                return ids
            print(f"No job IDs found in {input_value}")
            return None

        # Handle file types other than markdown as error
        print(f"Unsupported file type: {input_value} (expected .md from list_jobs)")
        return None

    # List of ints or numeric strings
    return [int(i) for i in input_value]


def _process_job_action(
        action_name: str,
        endpoint: str,
        calibration_run_ids: list[int] | str,
        payload_extras: dict | None = None,
        *,
        require_confirmation: bool = False,
        confirm_keyword: str = "delete",
        pre_display_func: Callable[..., int] | None = None,
) -> int:
    """
    Common handler for job actions (delete, archive, lock, unlock).

    :param action_name: Verb describing the action (e.g., "Deleting", "Archiving").
    :param endpoint: API endpoint path (e.g., "/calibration/delete_jobs/").
    :param calibration_run_ids: List of job IDs or path to Markdown file.
    :param payload_extras: Optional additional payload keys (e.g., {"lock": True}).
    :param require_confirmation: If True, prompt user before proceeding.
    :param confirm_keyword: Keyword the user must type to confirm.
    :param pre_display_func: Optional callable to display job details before action (for single jobs).
    :return: 0 on success, 1 on failure.
    """
    calibration_run_ids = _normalize_job_ids(calibration_run_ids)
    if not calibration_run_ids:
        print("No job IDs provided.")
        return 1

    # ───── Optional pre-display for single-job operations ─────
    if pre_display_func and len(calibration_run_ids) == 1:
        print("\nFetching job details for confirmation...\n")
        pre_display_func(calibration_run_ids[0], display=True)

    # ───── Optional confirmation ─────
    if require_confirmation:
        try:
            confirmation = input(
                f"\nType '{confirm_keyword}' to confirm the permanent {action_name.lower()} of "
                f"{len(calibration_run_ids)} job(s): {', '.join(map(str, calibration_run_ids))}\n> "
            ).strip()
            if confirmation.lower() != confirm_keyword.lower():
                print(f"\n{action_name} aborted. No jobs were modified.")
                return 1
        except KeyboardInterrupt:
            print(f"\n\n{action_name} aborted. No jobs were modified.")
            return 1

    print(f"\n{action_name} calibration run jobs {calibration_run_ids}")

    # ───── Build payload ─────
    payload = {"calibration_run_ids": calibration_run_ids}
    if payload_extras:
        payload.update(payload_extras)

    # ───── Execute API call ─────
    response_json, success = post_with_spinner_and_retry(
        f"{action_name} jobs",
        endpoint,
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    if not success or not isinstance(response_json, dict):
        return 1

    # ───── Print results ─────
    jobs = response_json.get("jobs", [])
    if isinstance(jobs, list):
        for job in jobs:
            print(job.get("message", f"Job {job['calibration_run_id']} processed."))
    return 0


def extract_job_ids_from_markdown(file_path: str) -> list[int]:
    """
    Extracts calibration_run_ids from a Markdown table produced by list_jobs().
    The first column is assumed to contain the Run ID, but parsing is flexible:
    - Ignores header and divider lines
    - Accepts varying spacing or indentation
    - Stops at any non-table content

    :param file_path: Path to the Markdown file created by list_jobs()
    :return: List of integer job IDs
    """
    job_ids = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("|-") or stripped.startswith("| Run ID"):
                continue

            # Extract fields between pipes
            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if parts and parts[0].isdigit():
                job_ids.append(int(parts[0]))

    return job_ids


class Spinner:
    def __init__(self, message="Processing"):
        self.spinner = itertools.cycle(["|", "/", "-", "\\"])
        self.running = False
        self.thread = None
        self.message = message

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin)
        self.thread.start()

    def _spin(self):
        print(self.message, end=" ", flush=True)
        while self.running:
            sys.stdout.write(next(self.spinner))
            sys.stdout.flush()
            time.sleep(0.1)
            sys.stdout.write("\b")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        sys.stdout.write(" \n")
        sys.stdout.flush()


def _transform_git_info_component(component_git_info: dict) -> dict[str, str]:
    """
    Transform raw Git metadata into the same display format used by the server.

    Rules:
      - Always include release, build_date, and commit_hash.
      - If tags is non-empty, use tags as release.
      - If tags is empty, use dev (<branch>) as release and include
        commit_date, author, and message when available.

    :param component_git_info: Raw Git metadata for one component.
    :return: Transformed Git metadata for display/output.
    """
    transformed: dict[str, str] = {}

    tags = component_git_info.get("tags", "").strip()

    if tags:
        transformed["release"] = tags
    else:
        transformed["release"] = f"dev ({component_git_info.get('branch', '<unknown>')})"

    transformed["build_date"] = component_git_info.get("build_date", "")
    transformed["commit_hash"] = component_git_info.get("commit_hash", "")

    if not tags:
        if "commit_date" in component_git_info:
            transformed["commit_date"] = component_git_info.get("commit_date", "")
        if "author" in component_git_info:
            transformed["author"] = component_git_info.get("author", "")
        if "message" in component_git_info:
            transformed["message"] = component_git_info.get("message", "")

    return transformed


def _transform_git_info(git_info: dict) -> dict[str, dict[str, str]]:
    """
    Transform all bundled CLI Git metadata into the same display format used
    by the server.

    :param git_info: Raw Git metadata keyed by component name.
    :return: Transformed Git metadata keyed by component name.
    """
    return {
        key: _transform_git_info_component(value)
        for key, value in git_info.items()
        if isinstance(value, dict)
    }


def _get_transformed_bundled_cli_git_info() -> dict[str, dict[str, str]]:
    """
    Return transformed CLI Git metadata bundled into the PyInstaller executable.
    """
    return _transform_git_info(_get_bundled_cli_git_info())
