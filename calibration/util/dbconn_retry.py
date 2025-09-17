import logging
import threading
import time

from django.dispatch import receiver
from django_dbconn_retry import pre_reconnect, post_reconnect

logger = logging.getLogger(__name__)

_thread_ctx = threading.local()


@receiver(pre_reconnect)
def pre_reconnect_handler(_sender, _dbwrapper, **kwargs):
    _thread_ctx.start_time = time.perf_counter()
    attempt = kwargs.get("attempt", "?")
    alias = getattr(_dbwrapper, "alias", "default")
    logger.warning(f"[DB Reconnect] Attempting reconnect to alias '{alias}' (attempt {attempt})...")


@receiver(post_reconnect)
def post_reconnect_handler(_sender, _dbwrapper, **kwargs):
    elapsed = time.perf_counter() - getattr(_thread_ctx, "start_time", time.perf_counter())
    attempt = kwargs.get("attempt", "?")
    alias = getattr(_dbwrapper, "alias", "default")
    logger.warning(f"[DB Reconnect] Reconnection attempt {attempt} for alias '{alias}' completed in {elapsed:.3f}s.")
