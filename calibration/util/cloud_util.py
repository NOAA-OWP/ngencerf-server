"""
cloud_util.py

Filesystem abstraction utilities built on top of fsspec.

This module provides a unified interface for working with both local and cloud
storage (S3, GCS, Azure Blob/ADLS, etc.). It includes helpers for:

- Path normalization (`normalize_url`, `get_filesystem`):
  Ensures that bare paths are converted into proper URLs so that fsspec can
  operate consistently across providers.

- File operations (`is_dir`, `list_files`):
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
  * Use `localize_to_path` when the same remote file will be accessed multiple
    times within a workflow (e.g., geopackages or forcing CSVs).
  * Use `copy_tree` for bulk movement of files between providers or to local
    disk.

Environment:
  * Uses fsspec / boto3 standard authentication mechanisms.
  * For AWS S3, callers may optionally provide a named AWS profile for
    operations that need non-default credentials.
  * Cache is stored in /var/tmp by default, which typically survives reboots.
"""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple, Any, cast
from urllib.parse import urlparse

import boto3
import botocore.client
import botocore.exceptions
import fsspec
from boto3.exceptions import S3UploadFailedError
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ProfileNotFound

from calibration.views.called_from import called_from

logger = logging.getLogger(__name__)

# Persistent cache for localized cloud files.
# /var/tmp survives reboots; /tmp is usually wiped at boot.
CLOUD_CACHE_DIR = "/var/tmp/fsspec-cache"

# Regexes for detecting Windows local paths
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")  # UNC paths like \\server\share

_REMOTE_SCHEMES = {"s3", "gs", "gcs", "az", "abfs", "abfss"}


# Authentication may come from env/config as usual (AWS_*, shared credentials,
# GOOGLE_APPLICATION_CREDENTIALS, AZURE_*, etc.). For S3, some helpers also
# allow callers to provide an explicit AWS profile name.

class CredentialsExpired(Exception):
    """Generic credential-expired error across all cloud providers."""
    pass


class S3CredentialsExpired(CredentialsExpired):
    """Raised when AWS S3 credentials are expired."""
    pass


class S3ProfileError(Exception):
    """Raised when an AWS profile is missing or invalid."""
    pass


# ----------------------------------------------------------------------
# Internal message / error helpers
# ----------------------------------------------------------------------
def _format_profile_suffix(profile_name: str | None) -> str:
    """
    Return a human-readable suffix for messages that should mention which AWS
    profile was used.
    """
    return f" (profile: {profile_name})" if profile_name else ""


def _expired_credentials_message(
        function_name: str,
        path: str,
        profile_name: str | None = None,
) -> str:
    """
    Build a user-facing message for expired/invalid AWS credentials.
    """

    return (
        f"Credentials expired{_format_profile_suffix(profile_name)} "
        f"while calling {function_name} on {path}. "
        f"Please notify your system administrator."
    )


def _raise_if_s3_profile_error(exc: Exception, profile_name: str | None = None) -> None:
    """
    Raise S3ProfileError for common local AWS profile/config problems.

    These failures happen before any request reaches AWS, so they should not be
    reported as missing paths or expired credentials.
    """
    if isinstance(exc, ProfileNotFound):
        raise S3ProfileError(f"{exc}{_format_profile_suffix(profile_name)}") from exc

    msg = str(exc).lower()

    # Be conservative here; only map clearly profile/config-related errors.
    if "profile" in msg and (
            "could not be found" in msg
            or "not found" in msg
            or "does not exist" in msg
            or "invalid" in msg
    ):
        raise S3ProfileError(f"{exc}{_format_profile_suffix(profile_name)}") from exc


# ----------------------------------------------------------------------
# Filesystem utilities
# ----------------------------------------------------------------------

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


def join_url(base: str, *parts: str) -> str:
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


def get_filesystem(
        url: str,
        *,
        profile_name: str | None = None,
) -> tuple[fsspec.AbstractFileSystem, str]:
    """
    Return an fsspec filesystem for the given URL and the normalized URL.

    - If passed a bare local path, we normalize to file://... so fsspec is happy.
    - The returned fs is created from the URL's scheme (s3, file, gs, az, ...).
    - For S3 URLs, callers may optionally provide an AWS profile name.

    :param url: Full URL string for a resource (cloud or local).
    :param profile_name: Optional AWS profile name used for S3 URLs only.
    :return: (filesystem, normalized_url) tuple
    :raises S3ProfileError: If profile_name is provided for S3 and the AWS profile is missing or invalid.
    """
    url = normalize_url(url)
    parsed = urlparse(url)
    scheme = parsed.scheme or "file"

    try:
        if scheme == "s3":
            if profile_name:
                fs = fsspec.filesystem("s3", profile=profile_name)
            else:
                fs = fsspec.filesystem("s3")
        else:
            fs = fsspec.filesystem(scheme)
    except Exception as e:
        _raise_if_s3_profile_error(e, profile_name=profile_name)
        raise

    return fs, url


# ----------------------------------------------------------------------
# Copy utilities
# ----------------------------------------------------------------------

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


def copy_tree(src_url: str,
              dst_url: str,
              workers: int = 16,
              buffer_size: int = 8 * 1024 * 1024,
              verify: bool = False,
              profile_name: str | None = None) -> int:
    """
    Generic and reliable tree copy between:
        • EFS → S3/GCS/Azure
        • S3/GCS/Azure → EFS
        • Cloud → Cloud (server-side when supported)
        • Local → Local

    Preserves directory structure. Can optionally verify via SHA256.
    When copying S3→local with verify=True, uses the source manifest instead
    of re-hashing cloud objects.

    Manifest rules:
      • LOCAL → CLOUD with verify=True: manifest.json is CREATED on cloud.
        Hashes are computed from the LOCAL source only and stored for later
        restore-time verification. The uploaded cloud object is not read back.
      • CLOUD → LOCAL with verify=True: manifest.json is USED but NOT RESTORED.
      • Symlinks are preserved: stored as metadata in manifest and recreated on restore.


    ------------------------------------------------------------------
    URL HANDLING
    ------------------------------------------------------------------
    fsspec requires well-formed URLs. Local paths such as:
        /ngencerf/data/run/123
        ../../relative/path
        ~/stuff
    are *not* proper URLs. Depending on the backend, fsspec may:
        • reject them,
        • treat them as relative paths,
        • generate inconsistent behavior across providers.

    normalize_url():
        • expands ~
        • absolutizes the path
        • converts it into a proper file URL:
              /path/to/x  →  file:///path/to/x

    This guarantees:
        • fsspec sees a real URL (s3://, gs://, az://, file://)
        • local and cloud code paths behave consistently
        • path comparisons (prefix stripping, relpath, etc.) work predictably
        • no surprises with Windows-style paths

    When normalize_url() is needed:
        ✓ any time the caller provides a bare local filesystem path
        ✓ any time the caller provides a relative path
        ✓ any time fsspec must process the path through filesystem(s)

    When normalize_url() is NOT strictly required:
        • when the user already provides valid URLs:
              s3://bucket/key
              gs://bucket/key
              file:///abs/path

    BUT it’s still safe and recommended to run normalize_url() on everything,
    because it standardizes all inputs and prevents subtle bugs.

    ------------------------------------------------------------------
    Copy Strategy
    ------------------------------------------------------------------
      * Local source  → enumerated with os.walk()
      * Cloud source  → enumerated with fs.find()
      * Cloud→Cloud   → attempt provider server-side copy
      * Otherwise     → streamed copy via threads

    Note: copy_tree does NOT use the caching layer (localize_to_path).
    Use localize_to_path() yourself if you need persistent reuse of remote
    files (e.g., large GeoPackages reused across workflows).

    Note:
      * A single profile_name applies to both source and destination S3 URLs.
      * This is sufficient for local↔S3 workflows or when both S3 endpoints
        use the same AWS profile.
      * If a future workflow needs different source/destination AWS profiles,
        this function should be extended to accept separate src_profile_name
        and dst_profile_name arguments.

    :param src_url: Source prefix. Accepts:
                        • A full cloud URL (s3://bucket/prefix, gs://…, az://…)
                        • A full local URL (file:///path/to/dir)
                        • A bare local path (/ngencerf/data/run/123 or relative paths)

                    Bare local paths are automatically normalized into fully-qualified
                    file:// URLs via normalize_url(). The caller does NOT need to
                    pre-normalize them.

    :param dst_url: Destination prefix. Same rules as src_url:
                        • Cloud URLs stay as-is
                        • file:/// URLs stay as-is
                        • Bare local paths are automatically converted to file:/// form

                    Normalization ensures fsspec always receives a valid URL and can
                    resolve the correct backend.

    :param workers: Number of parallel threads for streamed copies. Higher values
                    increase throughput when copying many small-to-medium files.
    :param buffer_size:
        Size of the memory buffer used during streamed copies.
        Only applies when copying via this process (EFS↔S3, EFS↔Local, etc.).
        Ignored for server-side cloud copies.

    :param verify:
        When True:
          • LOCAL → CLOUD:
                - compute SHA256 of source file only
                - write manifest at the cloud destination (_manifest.json)
                - do NOT read back and hash uploaded cloud objects
          • CLOUD → LOCAL:
                - manifest must already exist at source
                - each restored local file is hashed and checked against manifest
                - _manifest.json is not restored
    :param profile_name:
        Optional AWS profile name used for S3 source/destination URLs.
        If omitted, the default credential chain is used.

    :return:
        Number of files successfully copied. If source prefix is empty,
        returns 0. Errors propagated to caller unless captured as
        S3CredentialsExpired for AWS credential issues.

    :raises S3CredentialsExpired:
        If AWS credentials are expired or invalid during S3 operations.
    :raises S3ProfileError:
        If an explicit AWS profile is missing or invalid.
    """
    logger.info(f"{called_from()}{_format_profile_suffix(profile_name)}")

    # Normalize both URLs (converts bare paths → file:///)
    src_url = normalize_url(src_url)
    dst_url = normalize_url(dst_url)

    # Parse schemes
    src_fs, _ = get_filesystem(src_url, profile_name=profile_name)
    dst_fs, _ = get_filesystem(dst_url, profile_name=profile_name)

    src_scheme = urlparse(src_url).scheme or "file"
    dst_scheme = urlparse(dst_url).scheme or "file"

    # Split source/dest into (base, prefix)
    src_base, src_prefix = _norm_prefix(src_url)  # e.g. ("file:///","/ngen/.../1_peter")
    dst_base, dst_prefix = _norm_prefix(dst_url)

    # Used ONLY during local→cloud verification.
    # manifest_entries collects file hash entries for writing new manifest.json.
    manifest_entries = [] if verify and src_scheme == "file" and dst_scheme != "file" else None

    # Symlink metadata is stored only during local→cloud so symlinks
    # can be recreated later during cloud→local restore.
    manifest_symlinks = [] if verify and src_scheme == "file" and dst_scheme != "file" else None

    # ------------------------------------------------------------
    # Helper to compute SHA256 when verify=True
    # ------------------------------------------------------------
    def compute_sha256(fs, path) -> str:
        h = hashlib.sha256()
        with fs.open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------
    # If verify and source is cloud → load manifest.json
    # ------------------------------------------------------------
    source_manifest_dict = None  # fast lookup dict: rel_path → sha256
    source_manifest_raw = None  # full manifest JSON (includes symlinks)
    if verify and src_scheme != "file":
        manifest_url = join_url(src_base, src_prefix, "_manifest.json")

        # Explicit existence check — DO NOT use fs.find() for this
        if src_fs.exists(manifest_url):
            try:
                with src_fs.open(manifest_url, "r") as mf:
                    source_manifest_raw = json.load(mf)  # keep full structure (files + symlinks)

                # Convert file list to dict for fast hash verification
                source_manifest_dict = {
                    entry["relative"]: entry["sha256"]
                    for entry in source_manifest_raw.get("files", [])
                }

                logger.info(
                    f"Loaded manifest for cloud→local verify: "
                    f"{manifest_url}{_format_profile_suffix(profile_name)}"
                )

            except Exception as e:
                logger.error(
                    f"Failed to load manifest.json at "
                    f"{manifest_url}{_format_profile_suffix(profile_name)}: {e}"
                )
                raise
        else:
            raise RuntimeError(
                f"Verification enabled, but no manifest.json found on source cloud directory: "
                f"{manifest_url}{_format_profile_suffix(profile_name)}"
            )

    # ------------------------------------------------------------
    # STEP 1 — Generate the file list correctly
    # ------------------------------------------------------------
    def list_local_files(base_path: str) -> list[tuple[str, str]]:
        """
        Return list of (absolute_file_path, relative_path_from_base)
        for a local directory source.
        """
        root_path = urlparse(base_path).path  # file:///... → /path
        out = []
        for dirpath, _, filenames in os.walk(root_path):
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, root_path).replace("\\", "/")
                out.append((abs_path, rel))
        return out

    def list_cloud_files(prefix_url: str) -> list[tuple[str, str]]:
        """
        Return list of (full_url, relative_path_from_prefix)
        for a cloud-provider source.

        IMPORTANT:
          fs.find() returns backend-dependent paths:
            • 'bucket/key'
            • 'key'
            • or fully-qualified URLs (s3://bucket/key)

        The implementation normalizes all cases into full URLs by rebuilding
        the provider URL manually (using join_url), rather than using
        normalize_url(), because normalize_url() would incorrectly treat
        provider keys as local paths.
        """
        try:
            all_objs = src_fs.find(prefix_url)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", prefix_url, profile_name)
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", prefix_url, profile_name)
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        # Normalize prefix for path comparison
        parsed = urlparse(prefix_url)
        prefix_bucket = parsed.netloc  # "ngwpc-dev"
        prefix_path = parsed.path.lstrip("/")  # "peter/.../1_peter"
        out = []

        for obj in all_objs:
            if obj.endswith("/"):
                continue

            # obj may be:
            #   "bucket/key"            (S3-style)
            #   "key"                   (GCS/other)
            #   "s3://bucket/key"       (full URL)
            # We must rebuild the full provider URL WITHOUT using normalize_url()

            if "://" in obj:
                full = obj
                full_path = urlparse(obj).path.lstrip("/")
            else:
                # Extract the key portion
                if obj.startswith(prefix_bucket + "/"):
                    key = obj.split("/", 1)[1]
                else:
                    key = obj

                # Rebuild full provider URL
                full = join_url(src_base, key)
                full_path = key

            # Compute rel-path strictly from the provider path
            if full_path.startswith(prefix_path):
                rel = full_path[len(prefix_path):].lstrip("/")
            else:
                rel = os.path.basename(full_path)

            out.append((full, rel))

        return out

    src_files = list_local_files(src_url) if src_scheme == "file" else list_cloud_files(src_url)

    if not src_files:
        logger.warning(f"No files found at {src_url}{_format_profile_suffix(profile_name)}")
        return 0

    # ------------------------------------------------------------
    # SKIP restoring manifest.json when CLOUD → LOCAL with verify=True
    # ------------------------------------------------------------
    if verify and src_scheme != "file":
        before = len(src_files)
        src_files = [(a, r) for (a, r) in src_files if r != "_manifest.json"]
        after = len(src_files)
        if before != after:
            logger.info(f"Skipped restoring _manifest.json (manifest is used but not copied){_format_profile_suffix(profile_name)}")

    logger.info(
        f"Copying {len(src_files)} files from {src_url} to {dst_url} using {workers} workers"
        f"{_format_profile_suffix(profile_name)}"
    )

    # Check server-side cp possibility
    use_server_side = (
            src_scheme == dst_scheme
            and _same_provider(src_fs, dst_fs)
            and _server_side_cp_supported(src_fs)
    )

    # Build destination path from relative path
    def make_dst(rel: str) -> str:
        return join_url(dst_base, dst_prefix, rel)

    # ------------------------------------------------------------
    # STEP 2 — Copy + (optional) verify one file
    # ------------------------------------------------------------
    def _copy_one(abs_src: str, rel_path: str) -> tuple[str, float, int]:
        t0 = time.perf_counter()
        dst_full = make_dst(rel_path)

        # ------------------------------------------------------------
        # Handle symlinks: preserve metadata instead of copying
        # ------------------------------------------------------------
        if src_scheme == "file" and os.path.islink(abs_src):
            target = os.readlink(abs_src)

            # Record symlink for local→cloud write
            if manifest_symlinks is not None:
                manifest_symlinks.append({
                    "relative": rel_path,
                    "target": target,
                })

            logger.info(f"Recorded symlink {rel_path} -> {target}")
            return dst_full, 0.0, -1

        # Make parent directory on destination
        dst_parent = os.path.dirname(urlparse(dst_full).path)
        try:
            dst_fs.mkdirs(join_url(dst_base, dst_parent), exist_ok=True)
        except Exception:
            pass

        # Try to get size (best-effort)
        size_bytes = -1
        try:
            info = src_fs.info(abs_src)
            size_bytes = int(info.get("size", -1))
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", abs_src, profile_name)
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", abs_src, profile_name)
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        except Exception:
            pass

        # Copy file (server-side or streamed)
        try:
            if use_server_side:
                _cp_file_server_side(src_fs, abs_src, dst_full)
            else:
                with src_fs.open(abs_src, "rb") as r, dst_fs.open(dst_full, "wb") as w:
                    shutil.copyfileobj(r, w, length=buffer_size)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", abs_src, profile_name)
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message("copy_tree", abs_src, profile_name)
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        dt = time.perf_counter() - t0

        # Base message (without throughput yet)
        base_msg = f"Copied {abs_src} -> {dst_full} in {dt:.3f}s"

        # Compute throughput if possible
        if size_bytes > 0:
            mib = size_bytes / (1024 * 1024)
            rate = mib / dt if dt > 0 else 0
            throughput = f" ({mib:.2f} MiB @ {rate:.2f} MiB/s)"
        else:
            throughput = ""

        # ------------------------------------------------------------
        # Verification / integrity tracking
        # ------------------------------------------------------------
        if verify:
            # CLOUD → LOCAL restore verification:
            # verify restored local file against manifest hash
            if source_manifest_dict is not None and src_scheme != "file":
                expected = source_manifest_dict.get(rel_path)  # O(1) lookup
                if expected is None:
                    raise RuntimeError(f"No manifest entry for {rel_path}")

                # Hash ONLY destination (local)
                dst_hash = compute_sha256(dst_fs, dst_full)

                if expected != dst_hash:
                    raise RuntimeError(
                        f"Verification FAILED for {rel_path}: {expected} != {dst_hash}"
                    )

            else:
                # LOCAL → CLOUD archive verification:
                # compute hash ONLY from source file and store it in the manifest.
                # Do not read back the uploaded cloud object.
                if src_scheme == "file" and dst_scheme != "file" and manifest_entries is not None:
                    src_hash = compute_sha256(src_fs, abs_src)
                    manifest_entries.append({
                        "relative": rel_path,
                        "sha256": src_hash,
                        "size": size_bytes if size_bytes > 0 else None
                    })

        # ----------------------------
        # UNIFIED LOG LINE
        # ----------------------------
        if verify:
            logger.debug(f"{base_msg}{throughput} - verified OK")

        else:
            # Unified no-verify line
            logger.debug(f"{base_msg}{throughput}")

        return dst_full, dt, size_bytes

    # ------------------------------------------------------------
    # STEP 4 — Fan-out threads to copy
    # ------------------------------------------------------------
    wall_start = time.perf_counter()
    total_bytes = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_copy_one, abs_src, rel) for abs_src, rel in src_files]
        for fut in as_completed(futures):
            _, _, sz = fut.result()
            completed += 1
            if sz > 0:
                total_bytes += sz

    wall = time.perf_counter() - wall_start

    if total_bytes > 0:
        mib = total_bytes / (1024 * 1024)
        rate = mib / wall if wall > 0 else 0
        logger.info(
            f"Copy summary: {completed} files, {mib:.2f} MiB in {wall:.3f}s "
            f"({rate:.2f} MiB/s, workers={workers}, "
            f"{'server-side' if use_server_side else 'streamed'})"
            f"{_format_profile_suffix(profile_name)}"
        )
    else:
        logger.info(
            f"Copy summary: {completed} files in {wall:.3f}s (workers={workers})"
            f"{_format_profile_suffix(profile_name)}"
        )

    # ------------------------------------------------------------
    # Write manifest.json ONLY when verify=True AND destination is cloud
    # ------------------------------------------------------------
    if verify and src_scheme == "file" and dst_scheme != "file":
        manifest_path = join_url(dst_base, dst_prefix, "_manifest.json")
        try:
            with dst_fs.open(manifest_path, "w") as mf:
                mf.write(json.dumps({
                    "files": manifest_entries or [],
                    "symlinks": manifest_symlinks or [],
                }, indent=2))
            logger.info(f"Wrote manifest: {manifest_path}{_format_profile_suffix(profile_name)}")
        except Exception as e:
            logger.error(f"Failed to write manifest.json at {manifest_path}{_format_profile_suffix(profile_name)}: {e}")

    # ------------------------------------------------------------
    # Recreate symlinks after CLOUD → LOCAL restore
    # (uses source_manifest_raw since it contains symlink metadata)
    # ------------------------------------------------------------
    if (
            verify
            and src_scheme != "file"
            and source_manifest_raw
            and "symlinks" in source_manifest_raw
    ):
        dest_root = urlparse(dst_url).path
        for entry in source_manifest_raw["symlinks"]:
            rel = entry["relative"]
            target = entry["target"]
            link_path = os.path.join(dest_root, rel)

            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            try:
                os.symlink(target, link_path)  # creates symlink even if target missing
                logger.info(f"Restored symlink {rel} -> {target}")
            except Exception as e:
                logger.error(f"Failed to recreate symlink {rel}: {e}")

    return completed


# ----------------------------------------------------------------------
# File operations
# ----------------------------------------------------------------------

def is_dir(path: str, *, profile_name: str | None = None) -> bool:
    """
    Cloud/local agnostic directory check.
    Works for file://, s3://, gcs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.
    Raises S3ProfileError if an explicit AWS profile is missing or invalid.

    :param path: URL or local filesystem path.
    :param profile_name: Optional AWS profile name used for S3 paths only.
    :return: True if path exists and is a directory/prefix, False otherwise.
    """
    parsed = urlparse(normalize_url(path))
    scheme = parsed.scheme or "file"

    if scheme == "file":
        return os.path.isdir(parsed.path or path)

    try:
        fs, norm_path = get_filesystem(path, profile_name=profile_name)
        return fs.isdir(norm_path)
    except S3ProfileError:
        raise
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("is_dir", path, profile_name)
            ) from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("is_dir", path, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e
    except Exception as e:
        _raise_if_s3_profile_error(e, profile_name=profile_name)
        return False


def s3_prefix_exists(s3_prefix_uri: str, *, profile_name: str | None = None) -> bool:
    """
    Determine whether an S3 prefix behaves like an existing "directory".

    This function checks whether at least one object exists under the given
    prefix by performing a `list_objects_v2` request with `MaxKeys=1`. If
    any object is returned, the prefix is considered to exist.

    Important notes about S3 behavior
    ---------------------------------
    Amazon S3 does not have real directories. A "directory" is simply a key
    prefix. A prefix is considered to exist only if at least one object
    currently exists whose key begins with that prefix.

    This function therefore interprets "prefix exists" as:

        "At least one object currently exists under this prefix."

    `.keep` file convention
    -----------------------
    This application assumes that every managed prefix contains a small
    placeholder file such as `.keep`.

    The presence of this file ensures the prefix always appears to exist
    when listed. Without such a file, an otherwise valid prefix may appear
    to not exist because S3 will return no objects for that prefix.

    For example:

        s3://bucket/my-prefix/

    If the bucket contains:

        my-prefix/.keep
        my-prefix/file1.zip

    then the prefix will be detected as existing.

    However, if no objects currently exist under the prefix, the S3 listing
    will return empty and this function will return False, even though the
    prefix could still be used to store objects.

    When to use this function
    -------------------------
    Use this when you want to confirm that a configured prefix has already
    been provisioned and contains at least one object (typically a `.keep`
    file). This is useful for validating configuration paths such as
    `NGENCERF_ZIPS_S3_PATH`.

    Do NOT use this to test whether an S3 location is writable or whether
    a new prefix could be created, since empty prefixes are invisible to S3.

    :param s3_prefix_uri: Fully-qualified S3 prefix URI (e.g. "s3://bucket/prefix/").
    :param profile_name: Optional AWS profile name used for authentication.
    :return: True if at least one object exists under the prefix, otherwise False.
    :raises ValueError: If the URI is not a valid S3 prefix.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises S3ProfileError: If the specified AWS profile does not exist or is invalid.
    """
    s3_prefix_uri = normalize_s3_prefix(s3_prefix_uri)
    bucket, prefix = _parse_s3_uri(s3_prefix_uri)

    try:
        s3 = get_s3_client(profile_name=profile_name)
        resp = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(resp.get("Contents"))
    except S3ProfileError:
        raise
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("s3_prefix_exists", s3_prefix_uri, profile_name)
            ) from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("s3_prefix_exists", s3_prefix_uri, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e
    except Exception as e:
        _raise_if_s3_profile_error(e, profile_name=profile_name)
        raise


def list_files(path: str, pattern: str = "*.csv", *, profile_name: str | None = None) -> list[str]:
    """
    List files under a local or cloud directory and return normalized URLs.

    Supports both local paths and cloud URLs (file://, s3://, gs://, az://, etc.).
    Raises S3CredentialsExpired if AWS credentials are expired.
    Raises S3ProfileError if an explicit AWS profile is missing or invalid.
    Raises FileNotFoundError if the given path is not a directory.

    Behavior:
      * Uses fsspec to glob all files under the given path that match the pattern.
      * Filters out directories (entries ending with "/").
      * Normalizes outputs so all results are fully-qualified URLs:
          - Local files → file:///absolute/path/to/file
          - Cloud keys  → scheme://bucket/key
      * Returns only files; directories are skipped.

    :param path: Directory path (local or cloud).
    :param pattern: Glob pattern for files (default "*.csv").
    :param profile_name: Optional AWS profile name used for S3 paths only.
    :return: List of normalized file URLs.
    """
    try:
        fs, norm_url = get_filesystem(path, profile_name=profile_name)
    except S3ProfileError:
        raise
    # Ensure trailing slash on directory
    norm_url = norm_url.rstrip("/")

    try:
        if not fs.isdir(norm_url):
            raise FileNotFoundError(f"{norm_url} is not a directory")
    except S3ProfileError:
        raise
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("list_files", path, profile_name)
            ) from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("list_files", path, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e
    except Exception as e:
        _raise_if_s3_profile_error(e, profile_name=profile_name)
        raise

    try:
        # fsspec.glob may return fully-qualified URLs or bare keys
        files = fs.glob(f"{norm_url}/{pattern}")
    except S3ProfileError:
        raise
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("list_files", path, profile_name)
            ) from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("list_files", path, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e
    except Exception as e:
        _raise_if_s3_profile_error(e, profile_name=profile_name)
        raise

    out_files = []
    base, _ = _norm_prefix(norm_url)
    scheme = urlparse(norm_url).scheme or "file"

    for f in files:
        if f.endswith("/"):  # skip dirs
            continue

        # Case 1: already a fully-qualified URL
        if "://" in f:
            # Already fully-qualified (e.g., s3://bucket/key or file:///...)
            candidate = f
        elif scheme == "file":
            # Local filesystem: f is relative, join with real directory path
            dir_path = urlparse(norm_url).path or norm_url
            candidate = os.path.join(dir_path, f)
        else:
            # Cloud provider: f may be "bucket/key" or just "key"
            if f.startswith(base.split("://", 1)[1] + "/"):
                # Case: "bucket/key"
                candidate = f"{base}/{f.split('/', 1)[1]}"
            else:
                # Case: plain "key"
                candidate = f"{base}/{f}"

        # Ensure consistent normalization (e.g., file:/// for local paths)
        out_files.append(normalize_url(candidate))

    return out_files


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

    Note:
      * This helper intentionally remains generic for now and does not accept
        an AWS profile_name override.
      * Remote localization/caching currently uses the default fsspec/boto3
        credential chain for the process.
      * If a future workflow needs profile-specific localization, this helper
        can be extended to pass profile_name through get_filesystem().
    """
    orig = url_or_path
    if not _is_remote(orig):
        # Local path: no caching layer involved
        yield orig, orig
        return

    os.makedirs(CLOUD_CACHE_DIR, exist_ok=True)
    p = urlparse(orig)
    scheme = p.scheme.lower()

    # Intentionally use the default filesystem resolution here.
    # localize_to_path() remains generic for now and does not yet support
    # per-call AWS profile overrides.
    fs = fsspec.filesystem(scheme)

    # Remote metadata to validate cache freshness
    meta_remote = _info_for(fs, orig)
    etag = str(meta_remote.get("ETag") or meta_remote.get("etag") or "")
    size = int(meta_remote.get("Size") or meta_remote.get("size") or -1)

    lm = meta_remote.get("LastModified") or meta_remote.get("last_modified")
    if isinstance(lm, datetime):
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


# ----------------------------------------------------------------------
# AWS / S3 object utilities
# ----------------------------------------------------------------------

def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """
    Parse an S3 URI into bucket and object key components.

    Validates that the input is a properly formed S3 object URI and extracts
    the bucket name and key. The URI must include both a bucket and a non-empty
    object key.

    Accepted format:
        s3://bucket-name/path/to/object.ext

    This function does not verify that the object actually exists in S3.
    It only validates structure and performs parsing.

    :param s3_uri: Fully-qualified S3 object URI.
    :return: Tuple of (bucket, key).
    :raises ValueError: If the URI is invalid or missing required components.
    """
    if not s3_uri or not isinstance(s3_uri, str):
        raise ValueError("s3_uri must be a non-empty string")

    p = urlparse(s3_uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")

    key = (p.path or "").lstrip("/")
    if key == "":
        raise ValueError(f"S3 URI must include an object key: {s3_uri}")

    return p.netloc, key


def _delete_s3_keys(
        *,
        bucket: str,
        keys: list[str],
        profile_name: str | None = None,
        context_path: str,
) -> int:
    """
    Delete specific S3 keys from a bucket.

    This is the shared low-level delete primitive used by higher-level helpers
    such as:
      * delete_all_s3_objects_under_prefix()
      * delete_expired_s3_objects_under_prefix()

    :param bucket: S3 bucket name.
    :param keys: Exact object keys to delete.
    :param profile_name: Optional AWS profile name.
    :param context_path: User-facing path/URI for error messages.
    :return: Number of objects successfully deleted.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises botocore.exceptions.ClientError: For other AWS errors.
    :raises S3ProfileError: If profile_name is provided but invalid.
    """
    if not bucket:
        raise ValueError("bucket is required")

    if not keys:
        return 0

    s3 = get_s3_client(profile_name=profile_name)
    deleted = 0

    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        delete_objects = [{"Key": key} for key in batch]

        try:
            resp = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": delete_objects, "Quiet": False},
            )
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("_delete_s3_keys", context_path, profile_name)
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message("_delete_s3_keys", context_path, profile_name)
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        for d in resp.get("Deleted") or []:
            k = d.get("Key")
            if k:
                logger.info(f"S3 deleted: s3://{bucket}/{k}{_format_profile_suffix(profile_name)}")
                deleted += 1

        errors = resp.get("Errors") or []
        for err in errors:
            k = err.get("Key")
            logger.error(
                f"S3 delete failed: s3://{bucket}/{k} "
                f"code={err.get('Code')} message={err.get('Message')}"
                f"{_format_profile_suffix(profile_name)}"
            )

        if errors:
            raise RuntimeError(
                f"S3 delete failed for {len(errors)} object(s) under {context_path}"
                f"{_format_profile_suffix(profile_name)}"
            )

    return deleted


def normalize_s3_prefix(uri: str) -> str:
    """
    Validate and normalize an S3 directory prefix.

    This function ensures that the provided URI represents a valid S3 prefix
    (i.e., a bucket plus a key prefix) and returns a normalized form that
    always ends with a trailing slash. The normalized form allows safe use
    with helpers such as join_url() when constructing object keys.

    Validation rules:
    - URI must start with "s3://".
    - A bucket name must be present.
    - A key prefix must be present (i.e., "s3://bucket/prefix").
    - The returned value always ends with "/".

    Examples:
        s3://bucket/prefix      -> s3://bucket/prefix/
        s3://bucket/prefix/     -> s3://bucket/prefix/

    Invalid examples:
        s3://bucket
        s3://
        bucket/prefix

    :param uri: S3 URI expected to represent a bucket prefix.
    :return: Normalized S3 prefix guaranteed to end with '/'.
    :raises ValueError: If the URI is not a valid S3 prefix.
    """
    if not uri or not isinstance(uri, str):
        raise ValueError("uri must be a non-empty string")

    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError(f"Invalid S3 prefix URI: {uri}")

    prefix = (p.path or "").lstrip("/")
    if not prefix:
        raise ValueError(f"S3 prefix URI must include a prefix: {uri}")

    normalized = f"s3://{p.netloc}/{prefix}"
    return normalized if normalized.endswith("/") else f"{normalized}/"


def upload_file_to_s3(*, local_path: str, s3_uri: str, profile_name: str | None = None) -> str:
    """
    Upload a local file to S3.

    Transfers a file from the local filesystem to an S3 object location
    specified by a fully-qualified S3 URI. The destination object will be
    overwritten if it already exists.

    This function performs a direct upload using boto3 and does not use
    fsspec or the caching layer.

    Authentication is handled via standard AWS credential resolution.
    If profile_name is provided, boto3 uses that named AWS profile instead
    of the default credential chain.

    :param local_path: Path to the local file to upload.
    :param s3_uri: Destination S3 object URI (s3://bucket/key).
    :param profile_name: Optional AWS profile name to use for this upload.
    :return: The destination S3 URI.
    :raises ValueError: If the S3 URI is invalid.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises botocore.exceptions.ClientError: For other AWS errors.
    :raises S3ProfileError: If profile_name is provided but the AWS profile is missing or invalid.
    """
    logger.info(
        f"Uploading file to S3{_format_profile_suffix(profile_name)}: "
        f"{local_path} -> {s3_uri}"
    )

    if not local_path:
        raise ValueError("local_path is required")

    bucket, key = _parse_s3_uri(s3_uri)

    s3 = get_s3_client(profile_name=profile_name)

    try:
        s3.upload_file(local_path, bucket, key)

    except S3UploadFailedError as e:
        underlying = e.__cause__ or e.__context__

        if isinstance(underlying, botocore.exceptions.ClientError):
            code = underlying.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("upload_file_to_s3", s3_uri, profile_name)
                ) from e

        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("upload_file_to_s3", s3_uri, profile_name)
            ) from e

        raise RuntimeError(
            f"S3 upload failed{_format_profile_suffix(profile_name)} "
            f"for {local_path} -> {s3_uri}: {e}"
        ) from e

    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("upload_file_to_s3", s3_uri, profile_name)
            ) from e
        raise

    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("upload_file_to_s3", s3_uri, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

    try:
        size = os.path.getsize(local_path)
        logger.info(
            f"S3 upload complete: {local_path} -> {s3_uri} "
            f"({size / 1024 / 1024:.2f} MB){_format_profile_suffix(profile_name)}"
        )
    except Exception:
        logger.info(f"S3 upload complete: {local_path} -> {s3_uri}{_format_profile_suffix(profile_name)}")

    return s3_uri


def generate_presigned_download_url(*, s3_uri: str, expires_seconds: int, profile_name: str | None = None) -> str:
    """
    Generate a presigned HTTP URL for downloading an S3 object.

    Creates a temporary signed URL that allows unauthenticated clients to
    download the specified S3 object using HTTP GET. The URL remains valid
    for the requested duration and then expires automatically.

    This is typically used when:
        • The backend controls access to objects
        • The client should download directly from S3
        • Authentication headers should not be required for the download

    Authentication is handled via standard AWS credential resolution.
    If profile_name is provided, boto3 uses that named AWS profile instead
    of the default credential chain.

    The URL is generated using AWS Signature Version 4 via boto3.

    :param s3_uri: S3 object URI to download (s3://bucket/key).
    :param expires_seconds: Lifetime of the URL in seconds.
    :param profile_name: Optional AWS profile name to use when generating the presigned URL.
    :return: A fully-qualified HTTPS presigned download URL.
    :raises ValueError: If the S3 URI is invalid or expiration is not positive.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises botocore.exceptions.ClientError: For other AWS errors.
    :raises S3ProfileError: If profile_name is provided but the AWS profile is missing or invalid.
    """
    if expires_seconds <= 0:
        raise ValueError("expires_seconds must be > 0")

    bucket, key = _parse_s3_uri(s3_uri)

    s3 = get_s3_client(profile_name=profile_name)
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )
        logger.debug(
            f"Generated presigned download URL for {s3_uri} "
            f"(expires={expires_seconds}s){_format_profile_suffix(profile_name)}"
        )
        return url
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
            raise S3CredentialsExpired(
                _expired_credentials_message("generate_presigned_download_url", s3_uri, profile_name)
            ) from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired(
                _expired_credentials_message("generate_presigned_download_url", s3_uri, profile_name)
            ) from e
        raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e


def delete_all_s3_objects_under_prefix(
        *,
        s3_dir_uri: str,
        profile_name: str | None = None,
) -> int:
    """
    Delete all S3 objects under a prefix.

    This is intended for deleting a logical "directory" in S3 after a successful
    restore/unarchive. Since S3 has no true directories, this lists all objects
    under the prefix and deletes them.

    :param s3_dir_uri: S3 directory URI (e.g. s3://bucket/prefix/).
    :param profile_name: Optional AWS profile name to use for S3 list/delete operations.
    :return: Number of objects successfully deleted.
    :raises ValueError: If s3_dir_uri is not a valid S3 URI.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises botocore.exceptions.ClientError: For other AWS errors.
    :raises S3ProfileError: If profile_name is provided but the AWS profile is missing or invalid.
    """
    s3_dir_uri = normalize_s3_prefix(s3_dir_uri)
    p = urlparse(s3_dir_uri)

    bucket = p.netloc
    prefix = (p.path or "").lstrip("/")
    continuation_token = None
    keys_to_delete: list[str] = []

    s3 = get_s3_client(profile_name=profile_name)

    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        try:
            resp = s3.list_objects_v2(**kwargs)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message(
                        "delete_all_s3_objects_under_prefix",
                        s3_dir_uri,
                        profile_name,
                    )
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message(
                        "delete_all_s3_objects_under_prefix",
                        s3_dir_uri,
                        profile_name,
                    )
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        for obj in resp.get("Contents") or []:
            key = obj.get("Key")
            if not key:
                continue
            keys_to_delete.append(key)

        if not resp.get("IsTruncated"):
            break

        continuation_token = resp.get("NextContinuationToken")

    deleted = _delete_s3_keys(
        bucket=bucket,
        keys=keys_to_delete,
        profile_name=profile_name,
        context_path=s3_dir_uri,
    )

    logger.info(
        f"Deleted {deleted} object(s) under S3 prefix {s3_dir_uri}"
        f"{_format_profile_suffix(profile_name)}"
    )

    return deleted


def delete_expired_s3_objects_under_prefix(
        *,
        s3_dir_uri: str,
        cutoff_unix_seconds: float,
        profile_name: str | None = None,
) -> int:
    """
    Delete S3 objects under a prefix that are older than a cutoff timestamp.

    This is intended for cleanup of "directory-like" S3 prefixes that store
    transient artifacts (e.g., generated ZIP files). The deletion is based on
    each object's LastModified time as returned by S3 listing.

    Notes:
    - S3 has no real directories; this lists objects by Prefix and deletes matching keys.
    - The prefix URI is validated and normalized to end with "/".
    - If listing or deletion fails due to expired/invalid AWS credentials, raises S3CredentialsExpired.

    :param s3_dir_uri: S3 directory URI (e.g., s3://bucket/prefix/).
    :param cutoff_unix_seconds: Delete objects with LastModified.timestamp() < cutoff_unix_seconds.
    :param profile_name: Optional AWS profile name to use for S3 list/delete operations.
    :return: Number of objects successfully deleted.
    :raises ValueError: If s3_dir_uri is not a valid S3 URI.
    :raises S3CredentialsExpired: If AWS credentials are missing, expired, or invalid.
    :raises botocore.exceptions.ClientError: For other AWS errors.
    :raises S3ProfileError: If profile_name is provided but the AWS profile is missing or invalid.
    """
    if cutoff_unix_seconds is None:
        raise ValueError("cutoff_unix_seconds is required")

    s3_dir_uri = normalize_s3_prefix(s3_dir_uri)
    p = urlparse(s3_dir_uri)

    bucket = p.netloc
    prefix = (p.path or "").lstrip("/")
    continuation_token = None
    attempted_keys: set[str] = set()
    keys_to_delete: list[str] = []

    cutoff_dt = datetime.fromtimestamp(float(cutoff_unix_seconds), timezone.utc)
    s3 = get_s3_client(profile_name=profile_name)

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        try:
            resp = s3.list_objects_v2(**kwargs)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message("delete_expired_s3_objects_under_prefix", s3_dir_uri, profile_name)
                ) from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message("delete_expired_s3_objects_under_prefix", s3_dir_uri, profile_name)
                ) from e
            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        for obj in resp.get("Contents") or []:
            key = obj.get("Key")
            lm = obj.get("LastModified")
            size = obj.get("Size")

            if not key or lm is None:
                continue

            # Only manage zip artifacts under this prefix.
            if not key.lower().endswith(".zip"):
                logger.debug(f"S3 cleanup skipping non-zip: s3://{bucket}/{key}{_format_profile_suffix(profile_name)}")
                continue

            if key in attempted_keys:
                logger.warning(
                    f"S3 cleanup saw duplicate key in listing; skipping duplicate: "
                    f"s3://{bucket}/{key}{_format_profile_suffix(profile_name)}"
                )
                continue

            lm_dt = lm
            if getattr(lm_dt, "tzinfo", None) is None:
                lm_dt = lm_dt.replace(tzinfo=timezone.utc)

            if lm_dt < cutoff_dt:
                logger.info(
                    f"S3 cleanup will delete: s3://{bucket}/{key} "
                    f"(last_modified={lm_dt.isoformat()} < cutoff={cutoff_dt.isoformat()}, size={size})"
                    f"{_format_profile_suffix(profile_name)}"
                )
                attempted_keys.add(key)
                keys_to_delete.append(key)
            else:
                logger.debug(
                    f"S3 cleanup keeping: s3://{bucket}/{key} "
                    f"(last_modified={lm_dt.isoformat()} >= cutoff={cutoff_dt.isoformat()})"
                    f"{_format_profile_suffix(profile_name)}"
                )

        if not resp.get("IsTruncated"):
            break

        continuation_token = resp.get("NextContinuationToken")

    return _delete_s3_keys(
        bucket=bucket,
        keys=keys_to_delete,
        profile_name=profile_name,
        context_path=s3_dir_uri,
    )


# ----------------------------------------------------------------------
# AWS / S3 session and credential utilities
# ----------------------------------------------------------------------

def _get_boto3_session(*, profile_name: str | None = None) -> boto3.session.Session:
    """
    Return a boto3 session.

    If profile_name is provided, the named AWS profile is used.
    Otherwise the default boto3 credential/config resolution is used.

    :raises S3ProfileError: If profile_name is provided but the AWS profile
        is missing or invalid.
    """
    try:
        if profile_name:
            return boto3.Session(profile_name=profile_name)

        return boto3.Session()
    except ProfileNotFound as e:
        raise S3ProfileError(f"{e}{_format_profile_suffix(profile_name)}") from e


def get_s3_client(*, profile_name: str | None = None) -> botocore.client.BaseClient:
    """
    Return an S3 client.

    If profile_name is provided, the client is created from a boto3 session
    bound to that named AWS profile. Otherwise the default boto3 credential
    chain is used.
    """
    session = _get_boto3_session(profile_name=profile_name)
    return session.client("s3")


def get_public_s3_client() -> botocore.client.BaseClient:
    """
    Return an unsigned S3 client for public buckets.

    Use this for public NOAA/NWS buckets where application AWS credentials
    should not be required.
    """
    return boto3.client(
        "s3",
        config=Config(
            signature_version=cast(Any, UNSIGNED),
        ),
    )


def list_s3_common_prefixes(
        *,
        bucket: str,
        prefix: str = "",
        delimiter: str = "/",
        public: bool = False,
        profile_name: str | None = None,
) -> list[str]:
    """
    List immediate child prefixes under an S3 prefix.

    Example:
        bucket='noaa-nws-aorc-v1-1-1km', prefix=''
        may return ['1979/', '1980/', ..., '2025/']

    :param bucket: S3 bucket name.
    :param prefix: Optional key prefix.
    :param delimiter: S3 delimiter, usually '/'.
    :param public: If True, use unsigned public S3 access.
    :param profile_name: Optional AWS profile for non-public access.
    :return: List of CommonPrefixes values.
    """
    if not bucket:
        raise ValueError("bucket is required")

    s3 = get_public_s3_client() if public else get_s3_client(profile_name=profile_name)

    prefixes: list[str] = []
    continuation_token = None
    context_path = f"s3://{bucket}/{prefix}"

    while True:
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix,
            "Delimiter": delimiter,
        }

        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        try:
            resp = s3.list_objects_v2(**kwargs)

        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")

            if code in ("ExpiredToken", "InvalidAccessKeyId", "InvalidClientTokenId"):
                raise S3CredentialsExpired(
                    _expired_credentials_message(
                        "list_s3_common_prefixes",
                        context_path,
                        profile_name,
                    )
                ) from e

            raise

        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired(
                    _expired_credentials_message(
                        "list_s3_common_prefixes",
                        context_path,
                        profile_name,
                    )
                ) from e

            raise PermissionError(f"{e}{_format_profile_suffix(profile_name)}") from e

        except Exception as e:
            _raise_if_s3_profile_error(e, profile_name=profile_name)
            raise

        for item in resp.get("CommonPrefixes") or []:
            value = item.get("Prefix")
            if value:
                prefixes.append(value)

        if not resp.get("IsTruncated"):
            break

        continuation_token = resp.get("NextContinuationToken")

    return prefixes


def check_aws_credentials(*, timeout_seconds: int = 3) -> None:
    """
    This function is not used anymore

    Fast sanity check that AWS credentials are present and valid.

    Raises S3CredentialsExpired if credentials are missing, expired,
    or otherwise invalid. Intended for startup / readiness checks.
    """
    try:
        retry_config: dict[str, Any] = {
            "mode": "standard",
            "total_max_attempts": 1,
        }

        sts = boto3.client(
            "sts",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries=cast(Any, retry_config),  # cast to avoid Pycharm warning
            ),
        )

        identity = sts.get_caller_identity()

        logger.info(
            "AWS credentials OK: account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

    except (botocore.exceptions.NoCredentialsError, botocore.exceptions.PartialCredentialsError):
        # Boto3 could not construct a usable credential set locally
        # (missing, incomplete, unreadable, or unresolved credentials).
        # No request was made to AWS.
        raise S3CredentialsExpired("AWS credentials are missing or incomplete") from None

    except botocore.exceptions.ClientError as e:
        # Credentials were constructed successfully and a request reached AWS STS,
        # but STS rejected the request due to invalid, expired, or otherwise
        # unacceptable credentials.
        code = e.response.get("Error", {}).get("Code", "Unknown")

        if code in {"ExpiredToken", "InvalidClientTokenId"}:
            raise S3CredentialsExpired("AWS credentials are expired or invalid") from None

        # Any other STS error at startup still indicates unusable credentials
        # (e.g. wrong account, broken assume-role chain, signature issues).
        raise S3CredentialsExpired(f"AWS credential check failed: {code}") from None
