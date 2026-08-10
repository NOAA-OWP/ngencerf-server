import json
import logging
import os
import secrets
import threading
import time
import zipfile
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse
from django.urls import reverse
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.util.calibration_validators import CalibrationRunSerializer, GenericMessageWithIdResponseSerializer, ErrorResponseSerializer, \
    GetZipStatusSerializer, GetZipDownloadUrlResponseSerializer
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, get_user_email, validate_request, get_calibration_run, validate_response, get_elapsed_str, \
    ResponseError

logger = logging.getLogger(__name__)


_CLEANUP_LAST_RUN_KEY = "zip_cleanup_last_run"
_CLEANUP_LOCK_KEY = "zip_cleanup_lock"

downloadable_statuses = [s for s in StatusEnum if s not in {StatusEnum.READY, StatusEnum.SAVED, StatusEnum.SUBMITTED, StatusEnum.RUNNING}]


def get_zip_cache_key(calibration_run_id: int) -> str:
    """
    Returns the standardized cache key used to track zip job status for a given calibration run.

    - All zip-related endpoints use this key to read/write shared status in the Django cache.
    - The cache value is a dict with fields such as: status, path, started_at, download_name.

    :param calibration_run_id: CalibrationRun ID.
    :return: Cache key string used for this run's zip status.
    """
    return f'zip_status_{calibration_run_id}'


def get_zip_download_token_cache_key(token: str) -> str:
    return f'zip_download_token_{token}'


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GenericMessageWithIdResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Starts a background process to zip calibration job files. Use `get_zip_status` to track progress."
)
@api_view(['GET', 'POST'])
@handle_exceptions
def start_zip_for_calibration_job(request: Request) -> Response:
    """
    Starts a background job to create a ZIP for the calibration run's job_data_dir.

    - Sets a shared cache entry (status=pending) keyed by get_zip_cache_key(calibration_run_id).
    - Runs the zip build in a daemon thread and updates cache to status=done (or status=error).
    - The produced ZIP file is written to settings.ZIP_DIR and is later removed by cleanup_expired_zips().

    Cache fields written:
    - status: "pending" | "done" | "error"
    - path: absolute path to the built ZIP (done only)
    - started_at: ISO timestamp when the job began
    - download_name: canonical filename presented to the client (done only)

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: JSON message with calibration_run_id, or a formatted error Response.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    cache_key = get_zip_cache_key(calibration_run_id)

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return

    zip_status = cache.get(cache_key)
    if zip_status and zip_status.get('status') == 'pending':
        logger.info(f"Zip job already in progress for Calibration Job {calibration_run_id}")
        return Response({
            "message": "Zip job already in progress",
            "status": zip_status["status"],
            "calibration_run_id": calibration_run_id
        })

    # Mark status as pending (shared across workers)
    started_at = datetime.now().isoformat()
    cache.set(cache_key, {
        "status": "pending",
        "path": None,
        "started_at": started_at,
        "download_name": None,  # canonical download name (filled in when done)
    }, timeout=None)  # no timeout while building; "done" status gets a TTL

    # Launch zip process in background
    def zip_job():
        start_time = datetime.now()
        tmp_path = None

        try:
            job_data_dir = run.job_data_dir

            # Canonical download name (NO timestamp)
            zip_base_name = f"{os.path.basename(job_data_dir)}_{run.job_name}"
            download_name = f"{zip_base_name}.zip"

            # Unique on-disk filename includes timestamp to avoid collisions
            zip_filename = f"{zip_base_name}_{int(time.time())}.zip"
            zip_path = os.path.join(settings.ZIP_DIR, zip_filename)

            # Write to a temp file first, then atomically rename into place.
            tmp_path = f"{zip_path}.tmp"

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for root, _, files in os.walk(job_data_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.relpath(file_path, job_data_dir)
                        try:
                            zip_file.write(file_path, arc_name)
                        except FileNotFoundError:
                            logger.warning(f"File not found during zipping: {arc_name}")

            # Atomic replace: downloader will never see a partially-written zip.
            os.replace(tmp_path, zip_path)
            tmp_path = None  # prevent cleanup from deleting the final zip if names ever change

            # Mark the zip job as complete
            cache.set(cache_key, {
                "status": "done",
                "path": zip_path,
                "started_at": started_at,
                "download_name": download_name,
            }, timeout=3600)  # cache entry TTL; file TTL is controlled separately by ZIP_TTL_SECONDS

            duration = datetime.now() - start_time
            zip_size = os.path.getsize(zip_path)
            logger.info(
                f"Zip job completed for Calibration Job {run.id} in {duration.total_seconds():.2f} seconds "
                f"— size: {zip_size / 1024 / 1024:.2f} MB)"
            )

        except Exception as e:
            cache.set(cache_key, {
                "status": "error",
                "path": None,
                "started_at": started_at,
                "download_name": None,
            }, timeout=3600)  # Once it's done, don't leave it around forever
            duration = datetime.now() - start_time
            logger.exception(f"Failed to zip Calibration Job {run.id} after {duration.total_seconds():.2f} seconds: {e}")

        finally:
            # Best-effort cleanup of any temp file left behind
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception(f"Failed deleting temp zip: {tmp_path}")

    threading.Thread(target=zip_job, daemon=True).start()

    response = {"message": "Zip job started", "calibration_run_id": calibration_run_id}

    response_validator, error_response = validate_response(GenericMessageWithIdResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetZipStatusSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Polling endpoint that returns the current status of a calibration zip job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_zip_status(request: Request) -> Response:
    """
    Returns the current status of a background zip job started by start_zip_for_calibration_job().

    - Reads the shared cache entry keyed by get_zip_cache_key(calibration_run_id).
    - Does not start work; it only reports what is currently in cache.
    - Intended for polling until zip_status becomes "done" (or "error").

    Response fields:
    - calibration_run_id
    - zip_status: "pending" | "done" | "error"
    - path: ZIP path when done (used by download_calibration_zip)
    - started_at: ISO timestamp when the job began

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: JSON response with zip status information, or a formatted error Response.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_response = validate_request(CalibrationRunSerializer, data)
    if error_response:
        return error_response

    calibration_run_id = validator.get("calibration_run_id")
    cache_key = get_zip_cache_key(calibration_run_id)

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    zip_status = cache.get(cache_key)
    if not zip_status:
        return ResponseError(f"No zip job found for Calibration Job {calibration_run_id}")

    response = {
        "calibration_run_id": calibration_run_id,
        "zip_status": zip_status.get("status"),
        "path": zip_status.get("path"),
        "started_at": zip_status.get("started_at"),
    }

    response_validator, error_response = validate_response(
        GetZipStatusSerializer,
        response
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}'
        f'{get_elapsed_str(request)} - {json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def cleanup_expired_zips() -> None:
    """
    Opportunistically deletes expired ZIP artifacts created by zip endpoints.

    Why this exists:
    - ZIP files are not deleted immediately after returning FileResponse, because the server can finish
      "sending" while the client is still receiving bytes (and reverse proxies may buffer).
      Deleting too early can break large downloads.
    - Instead, ZIPs live on disk for settings.ZIP_TTL_SECONDS and are deleted lazily when any zip-related
      endpoint runs.

    Behavior:
    - Scans settings.ZIP_DIR for:
      - "*.zip" files older than (now - settings.ZIP_TTL_SECONDS)
      - "*.tmp" files older than (now - settings.ZIP_TTL_SECONDS) from interrupted builds
    - Throttled to run at most once every 5 minutes across all workers (shared cache timestamp).
    - Uses a shared cache lock so only one worker performs deletions at a time.

    :return: None
    """
    logger.debug(f"ZIP cleanup sweep: dir={settings.ZIP_DIR}, ttl={settings.ZIP_TTL_SECONDS}s")

    now = time.time()

    # Run at most every 5 minutes across all workers.
    last = cache.get(_CLEANUP_LAST_RUN_KEY)
    if last and (now - float(last)) < 300:
        return

    # Acquire a short-lived lock across workers to avoid multiple processes deleting simultaneously.
    if not cache.add(_CLEANUP_LOCK_KEY, "1", timeout=60):
        return

    try:
        # Record that cleanup ran (even if nothing is deleted) to prevent repeated scans.
        cache.set(_CLEANUP_LAST_RUN_KEY, now, timeout=24 * 3600)

        cutoff = now - settings.ZIP_TTL_SECONDS

        # If ZIP_DIR doesn't exist (misconfig or first-run), just no-op.
        if not os.path.isdir(settings.ZIP_DIR):
            return

        deleted_zip = 0
        deleted_tmp = 0

        for name in os.listdir(settings.ZIP_DIR):
            # Only manage artifacts created by this feature.
            is_zip = name.endswith(".zip")
            is_tmp = name.endswith(".tmp")
            if not (is_zip or is_tmp):
                continue

            path = os.path.join(settings.ZIP_DIR, name)

            # File might disappear between listdir() and stat() if another worker deletes it.
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue

            # Delete files older than the TTL.
            if st.st_mtime < cutoff:
                try:
                    os.remove(path)
                    if is_zip:
                        deleted_zip += 1
                    else:
                        deleted_tmp += 1
                except FileNotFoundError:
                    # Another worker/process deleted it after our stat().
                    pass
                except Exception:
                    logger.exception(f"Failed deleting expired artifact: {path}")

        if deleted_zip or deleted_tmp:
            logger.info(
                f"Lazy cleanup deleted {deleted_zip} expired zip(s) and {deleted_tmp} expired tmp file(s) from {settings.ZIP_DIR}"
            )

    finally:
        # Always release the lock.
        cache.delete(_CLEANUP_LOCK_KEY)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetZipDownloadUrlResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        ),
    },
    description="Return a short-lived download URL for a prepared ZIP (the download itself does not require Authorization)."
)
@api_view(["GET", "POST"])
@handle_exceptions
def get_calibration_zip_download_url(request: Request) -> Response:
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_response = validate_request(CalibrationRunSerializer, data)
    if error_response:
        return error_response

    calibration_run_id = validator.get("calibration_run_id")
    zip_cache_key = get_zip_cache_key(calibration_run_id)

    cleanup_expired_zips()

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return

    zip_status = cache.get(zip_cache_key)
    if not zip_status:
        return ResponseError(f"Zip job not found for Calibration Job {calibration_run_id}", http_status=status.HTTP_404_NOT_FOUND)

    if zip_status.get("status") != "done":
        return ResponseError(f"Zip file for Calibration Job {calibration_run_id} is not ready yet")

    zip_path = zip_status.get("path")
    if not zip_path or not os.path.exists(zip_path):
        return ResponseError(f"Zip file is missing for Calibration Job {calibration_run_id}")

    download_name = zip_status.get("download_name") or f"calibration_{calibration_run_id}.zip"

    # The UI never calls the download endpoint directly.
    # It calls this endpoint first to get a manufactured, short-lived URL containing a one-time token.
    token = mint_one_time_download_token(
        payload={
            "user_id": request.user.id,
            "zip_path": zip_path,
            "download_name": download_name,
            "calibration_run_id": calibration_run_id,
        },
        ttl_seconds=settings.ZIP_DOWNLOAD_URL_TTL_SECONDS,
    )

    # Build a URL relative to your API prefix (works behind nginx /api/)
    # If you mount Django under /api/, the UI can just use the returned string.
    download_url = reverse("downloadCalibrationZipToken") + f"?token={token}"

    response = {
        "calibration_run_id": calibration_run_id,
        "download_url": download_url,
        "expires_in_seconds": settings.ZIP_DOWNLOAD_URL_TTL_SECONDS,
    }

    response_validator, error_response = validate_response(GetZipDownloadUrlResponseSerializer, response)
    if error_response:
        return error_response

    return Response(response_validator.data)


@extend_schema(
    responses={
        200: OpenApiResponse(
            description="ZIP file stream (token-based download)"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Missing token"
        ),
        401: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Invalid or expired token"
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Zip file not found"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        ),
    },
    description=(
            "Token-based ZIP download (short-lived, single-use).\n\n"
            "- Does NOT require Authorization header\n"
            "- Streams ZIP file as an attachment\n"
            "- Token is invalidated after first use\n"
            "- Token expires after a short TTL\n"
            "- Intended to be called using the URL returned by get_calibration_zip_download_url"
    )
)
@api_view(["GET"])
@handle_exceptions
@permission_classes([AllowAny])
def download_calibration_zip_with_token(request: Request) -> FileResponse | Response:
    """
    INTERNAL DOWNLOAD ENDPOINT — NOT CALLED DIRECTLY BY THE UI.

    The UI must first call `get_calibration_zip_download_url`, which:
    - Validates the user and job state
    - Mints a short-lived, single-use token
    - Returns a manufactured download URL containing that token

    The browser then navigates to that returned URL, which resolves to this
    endpoint. This endpoint:
    - Does NOT require an Authorization header
    - Trusts the short-lived token + cache TTL + single-use delete for access control
    - Streams the ZIP file directly to the client

    Direct calls to this endpoint without a token are rejected.

    """
    token = request.query_params.get("token")
    if not token:
        return ResponseError("Missing token", http_status=status.HTTP_400_BAD_REQUEST)

    token_cache_key = get_zip_download_token_cache_key(token)
    token_data = cache.get(token_cache_key)
    if not token_data:
        return ResponseError("Invalid or expired token", http_status=status.HTTP_401_UNAUTHORIZED)

    # Make sure token remains valid.  Chrome may retry/resume large downloads
    cache.touch(token_cache_key, timeout=settings.ZIP_DOWNLOAD_URL_TTL_SECONDS)

    # token_data was written by get_calibration_zip_download_url()
    # It contains:
    #   - user_id            (for optional same-user enforcement)
    #   - zip_path          (absolute path to the prepared ZIP file)
    #   - download_name    (canonical filename presented to the client)
    #   - calibration_run_id (for logging and user-facing error messages)
    #
    # This endpoint only *consumes* that cached record. It does not
    # look up the run or ZIP status in the DB or zip_status cache.
    calibration_run_id = token_data.get("calibration_run_id")

    zip_path = token_data.get("zip_path")
    download_name = token_data.get("download_name") or (
        f"calibration_{calibration_run_id}.zip" if calibration_run_id else "download.zip"
    )

    cleanup_expired_zips()

    if not zip_path or not os.path.exists(zip_path):
        return ResponseError(
            f"Zip file is missing for Calibration Job {calibration_run_id}" if calibration_run_id else "Zip file is missing",
            http_status=status.HTTP_404_NOT_FOUND
        )

    # Mark as "in use" by bumping mtime. Cleanup uses mtime, so this postpones TTL deletion.
    try:
        os.utime(zip_path, None)
    except Exception:
        logger.debug(f"Failed to utime(zip_path): {zip_path}", exc_info=True)

    zip_size = os.path.getsize(zip_path)
    if calibration_run_id:
        logger.info(
            f"Serving Calibration zip for Job {calibration_run_id} — "
            f"size: {zip_size / 1024 / 1024:.2f} MB"
        )
    else:
        logger.info(f"Serving tokenized zip — size: {zip_size / 1024 / 1024:.2f} MB")

    zip_file = None
    try:
        zip_file = open(zip_path, "rb")
        response = FileResponse(zip_file, content_type="application/zip")

        # Add Content-Length so the browser knows exactly how many bytes to expect.
        # This can reduce Chrome "restart" behavior on large downloads.
        response["Content-Length"] = str(zip_size)

        apply_zip_download_headers(response, download_name, origin=request.headers.get("Origin"))

        logger.debug(
            f"Returning tokenized zip"
            f"{f' for Calibration Job {calibration_run_id}' if calibration_run_id else ''} "
            f"from {get_caller_name()}(){get_elapsed_str(request)}"
        )
        return response

    except Exception as e:
        if zip_file is not None:
            try:
                zip_file.close()
            except Exception:
                logger.debug("Failed to close file handle", exc_info=True)

        logger.exception(
            f"Failed to serve zip file"
            f"{f' for Calibration Job {calibration_run_id}' if calibration_run_id else ''}: {e}"
        )
        return ResponseError(
            f"Failed to read zip file for Calibration Job {calibration_run_id}" if calibration_run_id else "Failed to read zip file"
        )


@api_view(['GET', 'POST'])
@handle_exceptions
def get_calibration_job_zip(request: Request) -> FileResponse | Response:
    """
    Synchronous ZIP download endpoint (primarily for the CLI).

    - Builds a ZIP of the calibration run's job_data_dir on disk (not in memory).
    - Writes to a temporary file first and then atomically renames it into place.
    - Streams the completed ZIP back as a FileResponse attachment.

    Notes:
    - This endpoint does not use the background cache-based zip workflow.
    - The created ZIP artifact is left on disk and later removed by cleanup_expired_zips().

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: FileResponse streaming the ZIP, or a formatted error Response.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    job_data_dir = calibration_run.job_data_dir

    # Canonical download name (no timestamp)
    zip_base_name = f"{os.path.basename(job_data_dir)}_{calibration_run.job_name}"
    download_name = f"{zip_base_name}.zip"

    # Unique on-disk name (avoid collisions)
    zip_filename = f"{zip_base_name}_{int(time.time())}.zip"
    zip_path = os.path.join(settings.ZIP_DIR, zip_filename)

    # Write to a temp file first, then atomically rename into place.
    tmp_path = f"{zip_path}.tmp"

    # Build zip to temp path first, then atomically place the final file
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, _, files in os.walk(job_data_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, job_data_dir)
                    try:
                        zip_file.write(file_path, arc_name)
                    except FileNotFoundError:
                        logger.warning(f"File not found during zipping: {arc_name}")

        os.replace(tmp_path, zip_path)
        tmp_path = None

    finally:
        # Best-effort cleanup of temp zip if something failed mid-build
        if tmp_path:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception(f"Failed deleting temp zip: {tmp_path}")

    # Stream the finished zip back
    zip_file = None
    try:
        zip_file = open(zip_path, "rb")
        response = FileResponse(zip_file, content_type="application/zip")

        zip_size = os.path.getsize(zip_path)
        response["Content-Length"] = str(zip_size)

        apply_zip_download_headers(response, download_name)

        return response

    except Exception as e:
        if zip_file is not None:
            try:
                zip_file.close()
            except Exception:
                logger.debug("Failed to close file handle", exc_info=True)
        logger.exception(f"Failed to serve zip file for Calibration Job {calibration_run_id}: {e}")
        return ResponseError(f"Failed to read zip file for Calibration Job {calibration_run_id}")


def apply_zip_download_headers(response: FileResponse, download_name: str, origin: str | None = None) -> FileResponse:
    """
    Apply consistent headers for streaming a ZIP download.

    - Forces attachment download name.
    - Prevents any caching or storage by browsers and proxies.
    - Disables Nginx buffering to allow direct streaming of large files.
    - Prevents middleware/proxies from applying gzip or other content encodings.
    - Optionally sets CORS Access-Control-Allow-Origin for allowed origins.

    :param response: FileResponse already initialized with the ZIP file handle.
    :param download_name: Filename presented to the client.
    :param origin: Request Origin header value (optional).
    :return: The same response object (mutated).
    """
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response["Access-Control-Allow-Origin"] = origin

    response["Content-Disposition"] = f'attachment; filename="{download_name}"'

    # Do not allow browsers or proxies to store this response.
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"  # for older proxies

    # Tell Nginx NOT to buffer the file before sending it downstream.
    # This avoids Nginx holding a 1.5 GB file in memory/disk buffers, which can trigger timeouts or stall the transfer.
    response["X-Accel-Buffering"] = "no"
    response["Content-Encoding"] = "identity"  # Prevent response from being gzipped (especially ZIP files) by middleware

    return response


def mint_one_time_download_token(payload: dict, ttl_seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    cache.set(get_zip_download_token_cache_key(token), payload, timeout=ttl_seconds)
    return token
