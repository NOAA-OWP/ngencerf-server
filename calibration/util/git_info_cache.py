"""
Shared Redis cache utilities for Git metadata.

This module intentionally has no dependencies on calibration models or
model-backed enums so it can be imported safely while Django initializes the
application registry.
"""

import logging
import time

from django.core.cache import cache

from calibration.views.cache_prefix import CACHE_PREFIX

logger = logging.getLogger(__name__)

_GIT_INFO_CACHE_KEY = f"{CACHE_PREFIX}git_info"
_GIT_INFO_LOCK_KEY = f"{CACHE_PREFIX}git_info_lock"

_GIT_INFO_LOCK_TIMEOUT_SECONDS = 600
_GIT_INFO_WAIT_TIMEOUT_SECONDS = 600
_GIT_INFO_WAIT_INTERVAL_SECONDS = 1.0


def get_cached_git_info() -> dict[str, dict[str, str]] | None:
    """
    Retrieve merged Git metadata from the shared Django cache.

    :return: Cached Git metadata, or None if it is not currently cached.
    """
    return cache.get(_GIT_INFO_CACHE_KEY)


def set_cached_git_info(
    git_info: dict[str, dict[str, str]],
) -> None:
    """
    Store merged Git metadata indefinitely in the shared Django cache.

    Empty results are not cached so that a later request can retry after a
    temporary image-access or extraction failure.

    :param git_info: Merged and normalized Git metadata.
    """
    if git_info:
        cache.set(
            _GIT_INFO_CACHE_KEY,
            git_info,
            timeout=None,
        )


def acquire_git_info_cache_lock() -> bool:
    """
    Attempt to acquire the Redis-backed Git-information loading lock.

    ``cache.add()`` is atomic with the Redis cache backend, so only one worker
    can acquire the lock while the key exists.

    :return: True if the lock was acquired; otherwise False.
    """
    return cache.add(
        _GIT_INFO_LOCK_KEY,
        True,
        timeout=_GIT_INFO_LOCK_TIMEOUT_SECONDS,
    )


def release_git_info_cache_lock() -> None:
    """
    Release the Git-information loading lock.
    """
    cache.delete(_GIT_INFO_LOCK_KEY)


def wait_for_cached_git_info() -> dict[str, dict[str, str]] | None:
    """
    Wait for another worker to populate the shared Git-information cache.

    Waiting stops when the cached result becomes available, the loading lock
    disappears, or the configured wait timeout expires.

    :return: Cached Git metadata when available, or None if the loading worker
             releases its lock without populating the cache.
    """
    deadline = time.monotonic() + _GIT_INFO_WAIT_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        git_info = get_cached_git_info()
        if git_info is not None:
            return git_info

        if cache.get(_GIT_INFO_LOCK_KEY) is None:
            return None

        time.sleep(_GIT_INFO_WAIT_INTERVAL_SECONDS)

    logger.error(
        "Timed out waiting for another worker to populate the "
        "Git-information cache."
    )
    return None


def clear_cached_git_info() -> None:
    """
    Remove cached Git metadata and any existing loading lock.

    This is intended for testing or explicit cache invalidation. Git metadata
    is otherwise treated as static for the lifetime of the deployment.
    """
    cache.delete_many(
        [
            _GIT_INFO_CACHE_KEY,
            _GIT_INFO_LOCK_KEY,
        ]
    )
