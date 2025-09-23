"""
cloud_util.py

Filesystem abstraction utilities built on top of fsspec.

This module provides a unified interface for working with both local and cloud
storage (S3, GCS, Azure Blob/ADLS, etc.). It includes helpers for:

- Path normalization (`normalize_url`, `get_filesystem`):
  Ensures that bare paths are converted into proper URLs so that fsspec can
  operate consistently across providers.

- File operations (`open_file`, `path_exists`, `is_dir`, `list_files`):
  Cloud/local agnostic wrappers that mimic Python’s built-in file and os.path
  utilities but work transparently with remote storage.

- Bulk copy (`copy_tree`):
  Recursively copy entire directory trees. Uses provider-native server-side copy
  when possible (fast, no local I/O). Otherwise streams through this process
  with multi-threaded workers.

- Caching and localization (`localize_to_path`):
  Provides persistent or ephemeral caching for remote files. Remote objects can
  be downloaded once into /var/tmp/fsspec-cache and reused across multiple runs,
  avoiding redundant S3/GCS downloads. Cache entries are validated with provider
  metadata (etag/size/mtime). For one-shot ephemeral usage, files can be staged
  into a NamedTemporaryFile and removed after use.

⚠️ Cache persistence note:
  The cache under `/var/tmp/fsspec-cache` is never automatically cleaned up.
  It may grow indefinitely as new files are downloaded. However, the cache
  contents can be deleted at any time without harm; missing files will simply
  be re-fetched from the remote provider.

Typical usage:
  * Use `open_file` when you want to stream a file directly (no caching).
  * Use `localize_to_path` when the same remote file will be accessed multiple
    times within a workflow (e.g., geopackages or forcing CSVs).
  * Use `copy_tree` for bulk movement of files between providers or to local
    disk.

Environment:
  * Relies on fsspec’s standard authentication (AWS_*, GOOGLE_APPLICATION_CREDENTIALS,
    AZURE_*).
  * Cache is stored in /var/tmp by default, which typically survives reboots.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Iterator, Tuple
from urllib.parse import urlparse
from pathlib import Path

import fsspec
import botocore.exceptions

from calibration.views.called_from import called_from

logger = logging.getLogger(__name__)

# Persistent cache for localized cloud files.
# /var/tmp survives reboots; /tmp is usually wiped at boot.
CLOUD_CACHE_DIR = "/var/tmp/fsspec-cache"

# Regexes for detecting Windows local paths
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")  # UNC paths like \\server\share

_REMOTE_SCHEMES = {"s3", "gs", "gcs", "az", "abfs", "abfss"}


# You can pass auth via env (AWS_*, GOOGLE_APPLICATION_CREDENTIALS, AZURE_*),
# or via storage_options in get_filesystem(). Keep it simple here.

class S3CredentialsExpired(Exception):
    """Raised when AWS S3 credentials are expired."""
    pass


# ----------------------------------------------------------------------
# Filesystem utilities
# ----------------------------------------------------------------------

def get_filesystem(url: str) -> tuple[fsspec.AbstractFileSystem, str]:
    """
    Return an fsspec filesystem for the given URL and the normalized URL.

    - If passed a bare local path, we normalize to file://... so fsspec is happy.
    - The returned fs is created from the URL's scheme (s3, file, gs, az, ...).

    :param url: Full URL string for a resource (cloud or local).
    :return: (filesystem, normalized_url) tuple
    """
    url = normalize_url(url)
    parsed = urlparse(url)
    scheme = parsed.scheme or "file"
    # fsspec auto-picks backend by scheme
    fs = fsspec.filesystem(scheme)
    return fs, url


def _norm_prefix(url: str) -> tuple[str, str]:
    """
    Split a URL into (base, path) without trailing slashes in base.

    Example:
        s3://my-bucket/path/to/stuff -> ("s3://my-bucket", "path/to/stuff")

    :param url: Full URL string.
    :return: (base, path) where base includes scheme+netloc, and path is the remainder.
    """
    url = normalize_url(url)
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}".rstrip("/")
    path = p.path.lstrip("/")
    return base, path


def _same_provider(fs_a: fsspec.AbstractFileSystem,
                   fs_b: fsspec.AbstractFileSystem) -> bool:
    """
    Return True if both filesystem objects are of the same backend type.
    This is important because server-side copy is only possible when
    source and destination are managed by the same provider.
    """
    return type(fs_a) is type(fs_b)


def _server_side_cp_supported(fs: fsspec.AbstractFileSystem) -> bool:
    """
    Check if a filesystem supports a provider-native server-side copy.
    For example: S3, GCS, Azure may expose a 'cp_file' or 'copy' method.
    """
    return hasattr(fs, "cp_file") or hasattr(fs, "copy") or hasattr(fs, "cp")


def _cp_file_server_side(fs: fsspec.AbstractFileSystem, src: str, dest: str) -> None:
    """
    Attempt to perform a server-side copy using whichever method the
    backend exposes. Raises NotImplementedError if not supported.

    :param fs: The fsspec filesystem object.
    :param src: Source file URL.
    :param dest: Destination file URL.
    """
    if hasattr(fs, "cp_file"):
        return fs.cp_file(src, dest)
    if hasattr(fs, "copy"):
        return fs.copy(src, dest)
    if hasattr(fs, "cp"):
        return fs.cp(src, dest)
    raise NotImplementedError("No server-side copy method available for this backend")


def is_probably_local_path(p: str) -> bool:
    """
    Heuristic to decide if a path is local rather than a URL.

    - If it has a URL scheme (e.g. s3://), return False.
    - Windows drive letters (C:/...) or UNC paths (\\\\server\\share) return True.
    - Any other relative or absolute POSIX path returns True.
    """
    parsed = urlparse(p)
    if parsed.scheme:  # already looks like a URL
        return False
    # Windows drive or UNC counts as local
    if _DRIVE_RE.match(p) or _UNC_RE.match(p):
        return True
    # Plain relative/absolute posix path -> local
    return True  # default: assume local


def normalize_url(p: str) -> str:
    """
    Ensure a path is expressed as a proper URL.

    - If it looks like a local path, expand and absolutize it,
      then prefix with file:// (POSIX) or file:///C:/... (Windows).
    - If it's already a URL (has a scheme), return unchanged.
    """
    if is_probably_local_path(p):
        # Expand ~ and make absolute; fsspec file:// likes absolute paths
        ap = os.path.abspath(os.path.expanduser(p))
        # On Windows, file URLs need forward slashes and an extra slash before drive
        if os.name == "nt":
            ap = ap.replace("\\", "/")
            return f"file:///{ap}"  # Windows: file:///C:/path
        return f"file://{ap if ap.startswith('/') else '/' + ap}"  # POSIX
    return p


def _join_url(base: str, *parts: str) -> str:
    """
    Join a URL base and path parts with single slashes, preserving scheme form.
    - If base ends with '://', do not strip slashes (keeps 'file://' intact).
    - Ensures 'file://' is normalized to 'file:///' if needed.
    """
    if base.endswith("://"):
        b = base
    else:
        b = base.rstrip("/")
    segs = [p.strip("/") for p in parts if p]
    url = f"{b}/{'/'.join(segs)}" if segs else b
    if url.startswith("file://") and not url.startswith("file:///"):
        url = url.replace("file://", "file:///")
    return url


# ----------------------------------------------------------------------
# File operations
# ----------------------------------------------------------------------

def copy_tree(src_url: str,
              dst_url: str,
              workers: int = 16,
              buffer_size: int = 8 * 1024 * 1024) -> int:
    """
    Recursively copy all files under src_url into dst_url.
    Raises S3CredentialsExpired if AWS credentials are expired.


    - If source and destination are the same provider and support
      server-side copy, use that (fast, no local I/O).
    - Otherwise stream through this process with multiple threads.

    Note: copy_tree does not use the caching layer (localize_to_path).
    If you want persistent reuse across runs, call localize_to_path
    on each source file first.

    :param src_url: Source prefix URL (e.g. s3://bucket/prefix or file:///dir).
                    A bare path is also allowed; it will be normalized to file://.
    :param dst_url: Destination prefix URL (e.g. file:///localdir or s3://otherbucket/target).
                    A bare path is also allowed; it will be normalized to file://.
    :param workers: Number of parallel threads to use.
    :param buffer_size: Buffer size for streamed copies (default 8 MiB).
    :return: Number of files successfully copied.
    """
    logger.info(called_from())

    fs_src, _ = get_filesystem(src_url)
    fs_dst, _ = get_filesystem(dst_url)

    src_base, src_prefix = _norm_prefix(src_url)
    dst_base, dst_prefix = _norm_prefix(dst_url)

    # Find all source files
    # fs.find may return scheme-less paths for some backends (e.g., s3fs returns "bucket/key").
    # Build src_root for listing.
    src_root = f"{src_base}/{src_prefix}".rstrip("/")
    try:
        files = [p for p in fs_src.find(src_root) if not p.endswith("/")]
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise

    if not files:
        logger.warning(f"No files found at {src_url}")
        return 0

    logger.info(f"Copying {len(files)} files from {src_url} to {dst_url} using {workers} workers")

    # Decide if we can use provider-native server-side copy
    use_server_side = _same_provider(fs_src, fs_dst) and _server_side_cp_supported(fs_src)

    def _dst_path(src_path: str) -> str:
        """
        Compute destination path by removing the src_root prefix and prepending the destination base/prefix.
        Falls back to just the basename if src_path doesn't start with src_root.
        """
        if src_path.startswith(src_root):
            relative = src_path[len(src_root):].lstrip("/")
        else:
            relative = os.path.basename(src_path)
        return _join_url(dst_base, dst_prefix, relative)

    def _copy_one(src_path: str) -> tuple[str, float, int]:
        """
        Copy one file:
        - Try server-side copy if possible.
        - Otherwise stream through this process with buffer_size.

        Returns: (out_path, elapsed_sec, src_size_bytes)
        """
        t_start_sec = time.perf_counter()
        out_path = _dst_path(src_path)

        parent = os.path.dirname(urlparse(out_path).path).lstrip("/")
        try:
            fs_dst.mkdirs(_join_url(dst_base, parent), exist_ok=True)
        except Exception:
            pass

        # Attempt to get size for throughput reporting (best-effort)
        src_size_bytes = -1
        try:
            info = fs_src.info(src_path)
            src_size_bytes = int(info.get("size", -1))
        except Exception:
            pass

        if use_server_side:
            # Server-side copy within the same provider (fast, no data over your machine)
            _cp_file_server_side(fs_src, src_path, out_path)
        else:
            # Stream through memory with a large buffer; threads handle parallelism
            with fs_src.open(src_path, "rb") as r, fs_dst.open(out_path, "wb") as w:
                shutil.copyfileobj(r, w, length=buffer_size)

        elapsed_sec = time.perf_counter() - t_start_sec

        # Per-file timing/throughput log
        if src_size_bytes and src_size_bytes > 0:
            mebibytes = src_size_bytes / (1024 * 1024)
            mib_per_sec = mebibytes / elapsed_sec if elapsed_sec > 0 else 0.0
            logger.info(
                f"Finished copying {src_path} -> {out_path} in {elapsed_sec:.3f}s "
                f"({mebibytes:.2f} MiB @ {mib_per_sec:.2f} MiB/s)"
            )
        else:
            logger.info(f"Finished copying {src_path} -> {out_path} in {elapsed_sec:.3f}s")

        return out_path, elapsed_sec, max(src_size_bytes, 0)

    # Threaded fan-out over files
    wall_start_sec = time.perf_counter()
    completed = 0
    sum_bytes = 0
    sum_cpu_time_sec = 0.0  # sum of per-file times (not equal to wall time with parallelism)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_copy_one, p) for p in files]
        for fut in as_completed(futures):
            ret_path, ret_elapsed_sec, ret_size_bytes = fut.result()  # raises if error
            completed += 1
            sum_bytes += ret_size_bytes
            sum_cpu_time_sec += ret_elapsed_sec

    wall_elapsed_sec = time.perf_counter() - wall_start_sec

    logger.info(f"Successfully copied {completed}/{len(files)} files from {src_url} to {dst_url}")

    # Summary timing/throughput (added)
    if sum_bytes > 0:
        total_mib = sum_bytes / (1024 * 1024)
        wall_mib_per_sec = total_mib / wall_elapsed_sec if wall_elapsed_sec > 0 else 0.0
        avg_per_file_sec = wall_elapsed_sec / completed if completed else 0.0
        logger.info(
            f"Copy summary: {total_mib:.2f} MiB in {wall_elapsed_sec:.3f}s "
            f"({wall_mib_per_sec:.2f} MiB/s, avg per file {avg_per_file_sec:.3f}s, workers={workers}, "
            f"{'server-side' if use_server_side else 'streamed'})"
        )
    else:
        logger.info(
            f"Copy summary: duration {wall_elapsed_sec:.3f}s (workers={workers}, "
            f"{'server-side' if use_server_side else 'streamed'})"
        )

    return completed


def path_exists(path: str) -> bool:
    """
    Cloud/local agnostic exists() check.
    Works for file://, s3://, gcs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.

    :param path: URL or local filesystem path.
    :return: True if path exists, False otherwise.
    """
    if not path:
        return False

    parsed = urlparse(path)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        return os.path.exists(parsed.path or path)

    try:
        fs = fsspec.filesystem(scheme)
        return fs.exists(path)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except Exception:
        return False


def is_dir(path: str) -> bool:
    """
    Cloud/local agnostic directory check.
    Works for file://, s3://, gcs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.

    :param path: URL or local filesystem path.
    :return: True if path exists and is a directory/prefix, False otherwise.
    """
    parsed = urlparse(normalize_url(path))
    scheme = parsed.scheme or "file"

    if scheme == "file":
        return os.path.isdir(parsed.path or path)

    try:
        fs = fsspec.filesystem(scheme)
        return fs.isdir(path)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except Exception:
        return False


def open_file(path: str, mode: str = "r", **kwargs):
    """
    Open a local or cloud file for reading or writing.

    Uses fsspec under the hood, so `s3://`, `gs://`, `az://`, etc. all work.
    This does not use the caching layer — it always streams directly.

    :param path: Local path or cloud URL.
    :param mode: File mode, e.g. "r", "rb", "w".
    :param kwargs: Passed through to fsspec.open().
    :return: A file-like object.
    """
    fs, norm_url = get_filesystem(path)
    return fs.open(norm_url, mode, **kwargs)


def list_files(path: str, pattern: str = "*.csv") -> list[str]:
    """
    List files under a local or cloud directory and return normalized URLs.

    Works for file://, s3://, gs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.
    Raises FileNotFoundError if the given path is not a directory.

    Uses fsspec.glob, then normalizes outputs so all results are fully-qualified URLs.

    :param path: Directory path (local or cloud).
    :param pattern: Glob pattern (default "*.csv").
    :return: List of normalized file URLs.
    """
    fs, norm_url = get_filesystem(path)
    # Ensure trailing slash on directory
    norm_url = norm_url.rstrip("/")
    try:
        if not fs.isdir(norm_url):
            raise FileNotFoundError(f"{norm_url} is not a directory")
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise

    try:
        # fsspec's glob may return bare keys (like "bucket/key.csv")
        files = fs.glob(f"{norm_url}/{pattern}")
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise

    out_files = []
    base, _ = _norm_prefix(norm_url)

    for f in files:
        if f.endswith("/"):  # skip dirs
            continue

        # Case 1: already a fully-qualified URL
        if "://" in f:
            out_files.append(f)
            continue

        # Case 2: s3fs-style "bucket/key"
        if f.startswith(base.split("://", 1)[1] + "/"):
            out_files.append(f"{base}/{f.split('/', 1)[1]}")
            continue

        # Case 3: plain key ("aorc_2.2/...") — prepend base
        out_files.append(f"{base}/{f}")

    return [normalize_url(f) for f in out_files]


# ----------------------------------------------------------------------
# Caching + localization
# ----------------------------------------------------------------------

def _is_remote(url_or_path: str) -> bool:
    """
    Return True if the given path is remote (cloud storage).

    Remote schemes include: s3, gs/gcs, az/abfs/abfss.
    """
    p = urlparse(url_or_path)
    return bool(p.scheme) and p.scheme.lower() in _REMOTE_SCHEMES


def _cache_key(url: str) -> str:
    """
    Generate a stable SHA256 hash for a URL.

    Used as the basename for cached files and metadata sidecars.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _meta_path(cache_dir: str, key: str) -> str:
    """
    Construct the path to the JSON metadata file in the cache directory.
    Metadata is stored alongside cached files to record etag/size/mtime.
    """
    return os.path.join(cache_dir, f"{key}.meta.json")


def _data_path(cache_dir: str, key: str, suffix=".gpkg") -> str:
    """
    Construct the path to the cached file contents in the cache directory.
    The suffix is typically the file type (.gpkg, .csv, etc.).
    """
    return os.path.join(cache_dir, f"{key}{suffix}")


def _read_meta(path: str) -> dict:
    """
    Read a JSON metadata file, returning {} if unreadable or missing.
    """
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_meta(path: str, meta: dict) -> None:
    """
    Atomically write JSON metadata to disk for a cached file.
    Ensures partially-written metadata files aren’t left behind.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


def _info_for(fs, url: str) -> dict:
    """
    Wrapper for fs.info(url) that returns {} instead of raising.
    Safe for missing files or backends with limited support.
    """
    try:
        return fs.info(url)
    except Exception:
        return {}


@contextmanager
def localize_to_path(
        url_or_path: str,
        *,
        enable_cache: bool = True,
        suffix: str = ".gpkg",
) -> Iterator[Tuple[str, str]]:
    """
    Yield (original_path, local_path) for local or remote resources.
    Raises S3CredentialsExpired if AWS credentials are expired.

    Local paths:
      - Yields (p, p) without copying or caching.

    Remote URLs (s3/gs/az/abfs):
      - If enable_cache=True (default): persist under CLOUD_CACHE_DIR (/var/tmp/fsspec-cache)
        and reuse across runs.
        * Cache validation uses provider metadata: {etag, size, mtime}.
        * Cache hit when local file exists and metadata matches → reuse cached file.
        * Cache miss → download to <basename>.downloading, then atomically rename to <basename>
          and update metadata.
        * Metadata stored in JSON sidecar with {etag, size, mtime, url, t}.
        * If two different remote files share the same basename, the newer download
          will overwrite the older one.  We don't expect this to happen

      - If enable_cache=False: download into a NamedTemporaryFile and delete on exit.
        (Use this for one-shot reads that do not need persistence.)

    Typical use cases:
      * Geopackage inputs or forcing/observational CSVs that are read multiple times in a workflow —
        avoid redundant downloads by reusing the cached copy.
      * Pipelines where a file is validated (first pass) and then copied or transformed (second pass).
        Both passes will use the same cached local file.
      * Unit tests or short-lived processes may set enable_cache=False to avoid polluting /var/tmp.

    This mechanism is separate from copy_tree(); copy_tree streams directly and
    does not populate this cache.
    """
    orig = url_or_path
    if not _is_remote(orig):
        # Local path: no caching layer involved
        yield orig, orig
        return

    os.makedirs(CLOUD_CACHE_DIR, exist_ok=True)
    p = urlparse(orig)
    scheme = p.scheme.lower()
    fs = fsspec.filesystem(scheme)

    # Remote metadata to validate cache freshness
    meta_remote = _info_for(fs, orig)
    etag = str(meta_remote.get("ETag") or meta_remote.get("etag") or "")
    size = int(meta_remote.get("Size") or meta_remote.get("size") or -1)

    lm = meta_remote.get("LastModified") or meta_remote.get("last_modified")
    if isinstance(lm, datetime.datetime):
        mtime = int(lm.timestamp())
    elif isinstance(lm, (int, float)):
        mtime = int(lm)
    elif isinstance(lm, str):
        try:
            mtime = int(float(lm))
        except Exception:
            mtime = 0
    else:
        mtime = 0

    # Always use original basename for local cache filename
    basename = Path(p.path).name
    data_path = os.path.join(CLOUD_CACHE_DIR, basename)
    meta_path = data_path + ".meta.json"

    if enable_cache:
        meta_local = _read_meta(meta_path)
        ok = (
                os.path.exists(data_path)
                and meta_local.get("etag") == etag
                and meta_local.get("size") == size
                and meta_local.get("mtime") == mtime
        )

        if ok:
            # Cache hit
            yield orig, data_path
            return

        # Cache miss → download then promote
        tmp_download = data_path + ".downloading"
        logger.info(f"Downloading remote file to cache: {orig} → {data_path}")
        try:
            # Use fs.get to persist efficiently; same-bucket copies may be server-side
            fs.get(orig, tmp_download)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ExpiredToken":
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise

        try:
            os.replace(tmp_download, data_path)
            _write_meta(meta_path, {
                "etag": etag,
                "size": size,
                "mtime": mtime,
                "url": orig,
                "t": time.time()
            })
            yield orig, data_path
            return
        finally:
            if os.path.exists(tmp_download):
                os.remove(tmp_download)

    # Fallback: temp file if cache disabled
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmpf:
        tmp_path = tmpf.name
    try:
        logger.info(f"Downloading remote file to temp: {orig} → {tmp_path}")
        try:
            fs.get(orig, tmp_path)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ExpiredToken":
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        yield orig, tmp_path
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
