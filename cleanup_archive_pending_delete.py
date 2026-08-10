#!/usr/bin/env python3
"""
Cleanup quarantined archive directories left behind after successful S3 archiving.

This script is intended to be run from cron. It recursively scans one or more
roots for directories whose names contain the quarantine marker
'.__archived_pending_delete__.' and attempts to delete them with bounded retries.

These directories are created by the archive process:
- After copying job data to S3, the original local directory is renamed to a
  quarantine path (instead of being deleted immediately).
- This avoids archive failures caused by NFS/EFS behavior where open file handles
  (often represented as '.nfs*' files) temporarily prevent full deletion.
- The renamed directory is no longer part of the active job path and is safe to
  remove once the filesystem releases those handles.

This script completes that cleanup by retrying deletion of those quarantined
directories until they are fully removed.

The script uses only the Python standard library and does not require Django or
database access.

Examples:
    python cleanup_archived_pending_delete.py
    python cleanup_archived_pending_delete.py /ngencerf-app/data/ngen-cal-data/ngen-cal-work/run_calib
    python cleanup_archived_pending_delete.py /ngencerf-app/data/ngen-cal-data/ngen-cal-work/run_calib --attempts 5 --delay-seconds 2

See cleanup-archived-pending-delete-cron.prod for sample cron job
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import shutil
import sys
import time
from pathlib import Path


QUARANTINE_MARKER = ".__archived_pending_delete__."


def configure_logging(verbose: bool) -> None:
    """
    Configure process-wide logging.

    :param verbose: When True, enable DEBUG logging. Otherwise use INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )


logger = logging.getLogger(__name__)


def is_quarantined_directory(path: Path) -> bool:
    """
    Return True when a path is a quarantined archive directory.

    :param path: The filesystem path to inspect.
    :return: True if the path is a directory with the quarantine marker.
    """
    return path.is_dir() and QUARANTINE_MARKER in path.name


def find_quarantined_directories(roots: list[Path]) -> list[Path]:
    """
    Recursively find quarantined directories under one or more roots.

    Results are returned sorted by path depth, deepest first. This helps avoid
    trying to delete a parent quarantine directory before a nested child.

    :param roots: Root directories to scan.
    :return: Sorted list of quarantined directory paths.
    """
    matches: list[Path] = []

    for root in roots:
        if not root.exists():
            logger.warning("Scan root does not exist: %s", root)
            continue

        if not root.is_dir():
            logger.warning("Scan root is not a directory: %s", root)
            continue

        for path in root.rglob("*"):
            try:
                if is_quarantined_directory(path):
                    matches.append(path)
            except OSError as exc:
                logger.warning("Failed to inspect path %s while scanning: %s", path, exc)

    # Deepest paths first.
    return sorted(matches, key=lambda p: (len(p.parts), str(p)), reverse=True)


def delete_tree_with_retries(path: Path, attempts: int = 10, delay_seconds: float = 1.0) -> bool:
    """
    Delete a directory tree with bounded retries for common NFS/EFS cleanup races.

    This helper is intended for cases where shutil.rmtree() may fail because a
    file in the tree is still open by another process. On NFS/EFS, that can
    surface as retryable errors such as ENOTEMPTY or EBUSY, often involving
    temporary '.nfs*' placeholder files.

    A missing path is treated as success. This avoids races where the directory
    disappears between an existence check and the delete attempt.

    On failure, this helper logs only the single path reported by the exception
    instead of recursively scanning the remaining tree, which keeps the failure
    path cheap to evaluate even for very large directories.

    :param path: The directory tree to delete.
    :param attempts: Maximum number of delete attempts.
    :param delay_seconds: Delay between retry attempts in seconds.
    :return: True if the path is gone at the end of the operation, otherwise False.
    """
    if not path.exists():
        return True

    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return True
        except OSError as exc:
            # The directory may have disappeared after the existence check or
            # between retry attempts. That is equivalent to successful deletion.
            if exc.errno == errno.ENOENT or not path.exists():
                return True

            # Retry only for the NFS/EFS-style cases that may clear once another
            # process releases an open file handle.
            is_retryable = exc.errno in {
                errno.ENOTEMPTY,
                errno.EBUSY,
            }

            failed_path = getattr(exc, "filename", None)
            full_failed_path = (
                path / failed_path
                if failed_path and not os.path.isabs(failed_path)
                else Path(failed_path) if failed_path else path
            )

            is_nfs_placeholder = full_failed_path.name.startswith(".nfs")

            logger.warning(
                "Delete attempt %s/%s failed for %s: [Errno %s] %s. Failed path: %s. NFS placeholder: %s",
                attempt,
                attempts,
                path,
                exc.errno,
                exc,
                full_failed_path,
                is_nfs_placeholder,
            )

            # For non-retryable errors, or after the final attempt, stop retrying.
            if not is_retryable or attempt == attempts:
                return False

            time.sleep(delay_seconds)

    return not path.exists()


def cleanup_quarantined_directories(
    roots: list[Path],
    attempts: int,
    delay_seconds: float,
) -> int:
    """
    Scan for quarantined directories and try to delete each one.

    :param roots: Root directories to scan.
    :param attempts: Maximum number of delete attempts per directory.
    :param delay_seconds: Delay between delete retries in seconds.
    :return: Exit code. Zero means all matched directories were deleted.
    """
    quarantined_dirs = find_quarantined_directories(roots)

    if not quarantined_dirs:
        logger.info("No quarantined directories found.")
        return 0

    logger.info("Found %s quarantined director%s.", len(quarantined_dirs), "y" if len(quarantined_dirs) == 1 else "ies")

    failed: list[Path] = []

    for path in quarantined_dirs:
        logger.info("Cleaning quarantined directory: %s", path)
        deleted = delete_tree_with_retries(
            path=path,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        if deleted:
            logger.info("Deleted quarantined directory: %s", path)
        else:
            failed.append(path)
            logger.error("Could not delete quarantined directory: %s", path)

    if failed:
        logger.error("Cleanup finished with %s failure(s).", len(failed))
        for path in failed:
            logger.error("Remaining quarantined directory: %s", path)
        return 1

    logger.info("Cleanup finished successfully.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse command-line arguments.

    :param argv: Raw command-line arguments excluding the program name.
    :return: Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Delete quarantined archive directories left behind on EFS/NFS."
    )
    parser.add_argument(
        "roots",
        nargs="*",
        help="Root directories to scan. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=10,
        help="Maximum delete attempts per directory. Default: 10",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Delay between delete retries in seconds. Default: 1.0",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """
    Program entry point.

    :param argv: Raw command-line arguments excluding the program name.
    :return: Process exit code.
    """
    args = parse_args(argv)
    configure_logging(args.verbose)

    if args.attempts < 1:
        logger.error("--attempts must be at least 1.")
        return 2

    if args.delay_seconds < 0:
        logger.error("--delay-seconds must be non-negative.")
        return 2

    roots = [Path(root).resolve() for root in args.roots] if args.roots else [Path.cwd().resolve()]

    logger.debug("Scanning roots: %s", ", ".join(str(root) for root in roots))
    return cleanup_quarantined_directories(
        roots=roots,
        attempts=args.attempts,
        delay_seconds=args.delay_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
