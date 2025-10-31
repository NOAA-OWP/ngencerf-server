"""
Module providing CLI functionality for interacting with ngen calibration job endpoints.
Supports operations like uploading data, submitting/deleting/cancelling jobs, and
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
from contextlib import ExitStack
from datetime import datetime

import requests
import tabulate

from ngencerf.cli_util import check_http_error

API_BASE = "http://localhost:8000"


def get_auth_headers() -> dict[str, str]:
    """
    Returns authentication headers using the ACCESS_TOKEN environment variable.

    :returns: Dictionary containing the Authorization header.
    """
    return {
        "Authorization": f"Bearer {os.environ.get('ACCESS_TOKEN', '')}",
    }


def post_with_spinner_and_retry(message: str, endpoint: str, **kwargs) -> tuple[requests.Response | dict | None, bool]:
    """
    POST to an API endpoint with spinner, automatic token refresh/relogin, and retry support.
    Ensures each attempt uses the freshest ACCESS_TOKEN and rewinds file streams if present.
    Gracefully handles KeyboardInterrupt (Ctrl-C) to avoid ugly tracebacks.

    Behavior:
    - If stream=True and first attempt is 200: returns (requests.Response, True) for caller to iter_content().
    - If retry is needed and succeeds:
        - non-stream: returns parsed JSON (dict), True
        - stream: issues one more request (with spinner) and returns that new Response, True
    - On error: prints structured messages via check_http_error and returns (None, False).
    :param message: Message to display while waiting.
    :param endpoint: API endpoint (path relative to API_BASE).
    :param kwargs: Forwarded to `requests.post` (headers, json, files, stream, etc.)
    :return: A tuple of:
             - `requests.Response` if streaming, or `dict` if JSON, or `None` if failure.
             - `bool` indicating overall success.
    """

    def _rewind_files(files_obj):
        # Rewind any file-like objects so retries resend from the start
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

    def _make_post():
        # Make a fresh copy of kwargs for each attempt
        req_kwargs = dict(kwargs)

        # Refresh headers on every attempt; preserve other headers
        hdrs = dict(req_kwargs.get("headers") or {})
        hdrs["Authorization"] = f"Bearer {os.environ.get('ACCESS_TOKEN', '')}"
        req_kwargs["headers"] = hdrs

        # Rewind file handles if present (so retries resend from the start)
        if "files" in req_kwargs and req_kwargs["files"] is not None:
            _rewind_files(req_kwargs["files"])

        try:
            return requests.post(f"{API_BASE}{endpoint}", **req_kwargs)
        except requests.exceptions.RequestException as e:
            print(f"\nError: Could not connect to server at {API_BASE}.")
            print(f"Details: {e}")
            return None

    def _with_spinner(msg: str, fn):
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
    first_resp = _with_spinner(message, _make_post)
    if first_resp is None:
        return None, False

    if is_stream and first_resp.ok:
        # Success on first try → return raw Response for streaming
        return first_resp, True

    # ───────────────────────────────
    # 2. Handle response and decide on retry
    # ───────────────────────────────
    parsed_or_none, ok = check_http_error(
        first_resp.status_code,
        first_resp.text,
        first_resp.headers.get("Content-Type")
    )

    # Retry only if 401 and refresh/login succeeded
    if not ok and first_resp.status_code == 401:
        print("Retrying request after authentication recovery...")
        retry_resp = _with_spinner(f"Retrying: {message}...", _make_post)
        if retry_resp is None:
            return None, False

        if retry_resp.ok:
            if is_stream:
                return retry_resp, True
            try:
                return retry_resp.json(), True
            except Exception:
                return None, True

    if not ok:
        return None, False

    # 3) On success-after-retry:
    #    - non-stream: we already have parsed JSON (dict) from check_http_error → return it.
    #    - stream: we must obtain a fresh streaming Response for the caller to iter_content().
    if not is_stream:
        return parsed_or_none, True

    # stream=True and retry succeeded → open a fresh streaming connection (with spinner) and return it
    final_stream_resp = _with_spinner(f"Retrying: {message}...", _make_post)
    if final_stream_resp is None:
        return None, False
    if not final_stream_resp.ok:
        # If server still responds with an error here, print via check_http_error once more (no further retries).
        _ = check_http_error(
            final_stream_resp.status_code,
            final_stream_resp.text,
            final_stream_resp.headers.get("Content-Type"),
            retry_func=None
        )
        return None, False

    return final_stream_resp, True


def about(output_path: str | None = None) -> int:
    """
    Fetch and display git information from the calibration server in a formatted manner.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    # Resolve and validate output path before contacting the server
    final_path = resolve_output_path(output_path, "about_ngencerf.json")

    response_json, success = post_with_spinner_and_retry(
        "Sending request to server...",
        "/calibration/get_git_info/",
        headers=get_auth_headers()
    )
    if not success:
        return 1

    if response_json and (git_info := response_json.get("git_info")):
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(git_info, f, indent=2)

    print(f"ngenCerf 'about' info saved to {final_path}")
    return 0


def upload_geopackage_data(geopackage_file: str, calibration_run_id: int) -> int:
    """
    Uploads a geopackage file for a given calibration run.

    :param geopackage_file: Path to the .gpkg file.
    :param calibration_run_id: ID of the calibration run.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Uploading geopackage: {geopackage_file} for calibration_run_id: {calibration_run_id}")
    with open(geopackage_file, "rb") as f:
        response_json, success = post_with_spinner_and_retry(
            "Uploading geopackage...",
            "/calibration/upload_geopackage_data/",
            headers=get_auth_headers(),
            files={"geopackage_file": f},
            data={"calibration_run_id": calibration_run_id, "return_geopackage_url": "false"},
        )
    if not success:
        return 1
    if message := response_json.get("message"):
        print(message)
    if warnings := response_json.get("warnings"):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")
    return 0


def upload_observational_data(observational_file: str, calibration_run_id: int) -> int:
    """
    Uploads observational data (CSV) for a given calibration run.

    :param observational_file: Path to observational CSV.
    :param calibration_run_id: ID of the calibration run.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Uploading observational data: {observational_file} for calibration_run_id: {calibration_run_id}")
    with open(observational_file, "rb") as f:
        response_json, success = post_with_spinner_and_retry(
            "Uploading observational data...",
            "/calibration/upload_observational_data/",
            headers=get_auth_headers(),
            files={"observational_file": f},
            data={"calibration_run_id": calibration_run_id},
        )
    if not success:
        return 1
    if message := response_json.get("message"):
        print(message)
    if warnings := response_json.get("warnings"):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")
    return 0


def upload_forcing_data(forcing_dir: str, calibration_run_id: int) -> int:
    """
    Uploads all files in a directory as forcing data for a given calibration run.

    :param forcing_dir: Path to directory containing forcing files.
    :param calibration_run_id: ID of the calibration run.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Uploading forcing data from directory: '{forcing_dir}' for calibration_run_id: {calibration_run_id}")

    # noinspection PyAbstractClass
    with ExitStack() as stack:
        # Collect files from directory
        files = [
            ('files', (fname, stack.enter_context(open(os.path.join(forcing_dir, fname), 'rb'))))
            for fname in sorted(os.listdir(forcing_dir))
            if os.path.isfile(os.path.join(forcing_dir, fname))
        ]

        if not files:
            print("No forcing data files found to upload.")
            return 1

        response_json, success = post_with_spinner_and_retry(
            "Uploading forcing data...",
            "/calibration/upload_forcing_data/",
            headers=get_auth_headers(),
            files=files,
            data={"calibration_run_id": calibration_run_id},
        )
    if not success:
        return 1
    if message := response_json.get("message"):
        print(message)
    if warnings := response_json.get("warnings"):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")
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
        "Downloading zip...",
        "/calibration/get_calibration_job_zip/",
        headers=get_auth_headers(),
        json=payload,
        stream=True,
    )
    if not success:
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
        "Submitting job...",
        "/calibration/run_calibration/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1
    if message := response_json.get("message"):
        print(message)
    if warnings := response_json.get("warnings"):
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
        "Getting job status...",
        "/calibration/get_status/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1

    # Display top-level fields first (excluding validations and forecasts)
    print("\nCalibration Job Info:")
    top_level = {
        k: v for k, v in response_json.items()
        if k not in ("validations", "forecasts")
    }
    print(json.dumps(top_level, indent=2))

    # Display validations, if present
    if validations := response_json.get("validations"):
        print("\nValidations:")
        for v in validations:
            print(json.dumps(v, indent=2))

    # Display forecasts, if present
    if forecasts := response_json.get("forecasts"):
        print("\nForecasts:")
        for f in forecasts:
            print(json.dumps(f, indent=2))

    return 0


def delete_job(calibration_run_ids: list[int]) -> int:
    """
    Deletes one or more calibration runs, with confirmation.

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    if len(calibration_run_ids) == 1:
        # Display job details before deletion
        print("\nFetching job details for confirmation...\n")

        # Display the job details
        handle_export_display(calibration_run_ids[0], display=True)

    try:
        # Confirm deletion
        confirmation = input(f"\nType 'delete' to confirm the permanent deletion of calibration jobs {calibration_run_ids}: ").strip()
        if confirmation.lower() != "delete":
            print("\nDeletion aborted. The calibration jobs were not deleted.")
            return 1
    except KeyboardInterrupt:
        print("\n\nDeletion aborted. The calibration jobs were not deleted.")
        return 1

    # Proceed with deletion
    print(f"\nDeleting calibration run jobs {calibration_run_ids}")
    payload = {"calibration_run_ids": calibration_run_ids}
    response_json, success = post_with_spinner_and_retry(
        "Deleting jobs...",
        "/calibration/delete_jobs/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1

    for job in response_json.get("jobs", []):
        print(job.get("message", f"Job {job['calibration_run_id']} processed."))
    return 0


def archive_job(calibration_run_ids: list[int]) -> int:
    """
    Archives one or more calibration runs.

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Archiving calibration run jobs {calibration_run_ids}")
    payload = {"calibration_run_ids": calibration_run_ids, "archive": True}
    response_json, success = post_with_spinner_and_retry(
        "Archiving jobs...",
        "/calibration/archive_jobs/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1

    for job in response_json.get("jobs", []):
        print(job.get("message", f"Job {job['calibration_run_id']} archived."))
    return 0


def unarchive_job(calibration_run_ids: list[int]) -> int:
    """
    Unarchives one or more calibration runs.

    :param calibration_run_ids: A list of one or more calibration run IDs.
    :returns: 0 on success, 1 on failure.
    """
    print(f"Unarchiving calibration run jobs {calibration_run_ids}")
    payload = {"calibration_run_ids": calibration_run_ids, "archive": False}
    response_json, success = post_with_spinner_and_retry(
        "Unarchiving jobs...",
        "/calibration/archive_jobs/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1
    for job in response_json.get("jobs", []):
        print(job.get("message", f"Job {job['calibration_run_id']} unarchived."))
    return 0


def cancel_job(calibration_run_id: int) -> int:
    """
    Cancels a running calibration job.

    :param calibration_run_id: ID of the calibration run
    :return: 0 on success, 1 on failure
    """
    print(f"Cancelling calibration run job {calibration_run_id}")
    payload = {"calibration_run_id": calibration_run_id}
    response_json, success = post_with_spinner_and_retry(
        "Cancelling job...",
        "/calibration/cancel_job/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1
    if message := response_json.get("message"):
        print(message)
    if warnings := response_json.get("warnings"):
        print("Warnings:")
        for w in warnings:
            print(f"   {w}")
    return 0


def list_jobs(output_path: str | None = None) -> int:
    """
    Lists all calibration jobs and saves them to a markdown file.

    :param output_path: Path to save the job list (optional)
    :return: 0 on success, 1 on failure
    """
    # Resolve and validate path early
    final_path = resolve_output_path(
        output_path,
        f"calibration_jobs_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
    )

    response_json, success = post_with_spinner_and_retry(
        "Fetching job list...",
        "/calibration/get_calibration_jobs/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
    )
    if not success:
        return 1
    if not response_json:
        return 0

    jobs = response_json.get("jobs", [])
    if not jobs:
        print("No jobs found.")
        return 0

    rows = []
    for job in jobs:
        rows.append([
            job.get("calibration_run_id"),
            job.get("gage_id") or "-",
            job.get("status") or "-",
            (job.get("calibration_start_period") or "-").replace("T", " ").split(".")[0],
            (job.get("calibration_end_period") or "-").replace("T", " ").split(".")[0],
            job.get("formulation_name") or "-",
            job.get("objective_function") or "-",
            job.get("optimization_algorithm") or "-",
            (job.get("created_at") or "-").replace("T", " ").split(".")[0],
            ", ".join(job.get("modules", []))
        ])

    headers = [
        "Run ID", "Gage", "Status", "Start", "End",
        "Formulation", "Objective", "Optimization", "Created", "Modules"
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
        "Updating gage status...",
        "/calibration/update_and_get_gage_status/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1

    message = response_json.get("message", "")
    gage_id = response_json.get("gage_id")
    is_active = response_json.get("is_active")

    print(message or f"Gage {gage_id} is {'active' if is_active else 'not active'}")
    return 0


def _submit_job_data(job_file: str, action: str, calibration_run_id: int | None = None, run_after_import: bool | None = None) -> int:
    """
    Submits job data to the import or update endpoint.

    :param job_file: Path to the JSON file
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

    # Override the run_after_import field if specified
    if run_after_import is not None:
        print(f"Overriding run_after_import: {run_after_import}")
        job_data["run_after_import"] = run_after_import

    # Build the payload
    payload = {"data": job_data}
    if calibration_run_id is not None:
        payload["calibration_run_id"] = calibration_run_id

    response_json, success = post_with_spinner_and_retry(
        f"{action} job...",
        "/calibration/import/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success:
        return 1

    # Print top-level message
    if message := response_json.get("message"):
        print(message)

    # Collect errors into one list
    combined_errors = []
    combined_warnings = []

    # Nested messages block
    if messages := response_json.get("messages"):
        combined_errors.extend(messages.get("errors", []))
        combined_errors.extend(e.get("message", str(e)) for e in messages.get("eds_errors", []))
        combined_warnings.extend(messages.get("warnings", []))

    # Top-level blocks
    if errors := response_json.get("errors"):
        combined_errors.extend(errors)
    if warnings := response_json.get("warnings"):
        combined_warnings.extend(warnings)

    # Print all collected errors and warnings
    if combined_errors:
        print("Errors:")
        for e in combined_errors:
            print('  ', e)

    if combined_warnings:
        print("Warnings:")
        for w in combined_warnings:
            print('  ', w)

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
        "Fetching job...",
        "/calibration/export/",
        headers={**get_auth_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    if not success or not response_json:
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

        def make_request():
            return requests.post(
                f"{API_BASE}/calibration/get_regionalization_files_zip/",
                headers=get_auth_headers(),
                json=payload,
                stream=True,
            )

        # Run initial request with spinner + retry
        response_json, success = post_with_spinner_and_retry(
            "Downloading regionalization ZIP...",
            "/calibration/get_regionalization_files_zip/",
            headers=get_auth_headers(),
            json=payload,
            stream=True,
        )
        if not success:
            return 1

        # Actually stream the file (second request, like your original)
        resp = make_request()
        if not resp.ok:
            print(f"Download failed with status code {resp.status_code}")
            check_http_error(resp.status_code, resp.text)
            return 1

        # Save ZIP to temp path
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

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

    :param calibration_run_id: ID of the calibration run
    :param data: Exported job data
    """

    def fmt(dt: str | None) -> str:
        """
        Formats an ISO timestamp string in GMT (UTC) to 'YYYY-MM-DD HH:MM'.
        Handles optional 'Z' or '+00:00' suffixes.

        :param dt: ISO timestamp string
        :return: Formatted timestamp
        """
        if not dt:
            return "-"
        dt = dt.replace("Z", "").split("+")[0]  # strip 'Z' or '+00:00'
        try:
            return datetime.fromisoformat(dt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return dt  # fallback: return original if parsing fails

    cal_times = data.get("calibration_times", {})
    val_times = data.get("validation_times", {})
    metadata = data.get("metadata", {})

    print()
    print(f"Calibration Job ID {metadata.get('source_calibration_run_id', calibration_run_id)}")
    print(f"Status: {metadata.get('source_status')}")
    print(f"Job Data directory: {metadata.get('job_data_dir')}")
    print(f"Gage: {data.get('gage_id')}")
    print(f"Catchments: {metadata.get('num_catchments', '-')}")
    print(f"Forcing Source: {data.get('forcing_source')}")
    print(f"Observational Source: {data.get('observational_source')}")
    print(f"Geopackage Source: {data.get('geopackage_source')}")
    print(data.get("description", "").strip())
    print()

    print(f"Formulation Name: {data.get('formulation_name')}")
    print(f"Modules: {', '.join(data.get('modules', []))}")
    print()

    print(f"{'Calibration Run':<50}{'Validation Run'}")
    print(f"{'Sim Start:':<25}{fmt(cal_times.get('simulation_start_time')):<25}Sim Start: {fmt(val_times.get('simulation_start_time'))}")
    print(f"{'Sim End:':<25}{fmt(cal_times.get('simulation_end_time')):<25}Sim End:   {fmt(val_times.get('simulation_end_time'))}")
    print(f"{'Calib Start:':<25}{fmt(cal_times.get('calibration_start_time')):<25}Val Start: {fmt(val_times.get('validation_start_time'))}")
    print(f"{'Calib End:':<25}{fmt(cal_times.get('calibration_end_time')):<25}Val End:   {fmt(val_times.get('validation_end_time'))}")
    print()

    print(f"Optimization Algorithm: {data.get('optimization')}")
    print(f"Objective Function: {data.get('objective_function')}")
    print(f"Plot Generation Frequency: {data.get('save_plot_iteration_frequency')}")
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


class Spinner:
    def __init__(self, message="Processing..."):
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
