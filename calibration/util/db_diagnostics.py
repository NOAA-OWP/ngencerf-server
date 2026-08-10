import logging
import re
import threading
import time
import traceback

from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.utils import OperationalError as DjangoOperationalError
from psycopg import Error as PostgresDriverError  # psycopg3 always available

logger = logging.getLogger(__name__)

# ---- Rate limiting for retries ------------------------------------------------
# Keep diagnostics from spamming logs during retry loops (e.g., django-dbconn-retry).
# We log at most once per RATE_LIMIT_SECONDS for a given (alias, error signature).
RATE_LIMIT_SECONDS: float = 60.0
_last_diag_log: dict[tuple[str, str, str], float] = {}
_rl_lock = threading.Lock()

# ---- Patch guard (idempotency) -----------------------------------------------
_PATCHED = False


def _should_log(alias: str, exc: Exception) -> bool:
    """
    Decide whether to emit diagnostics for this failure based on a simple
    time-based rate limit keyed by alias + error signature.
    """
    err_type = f"{type(exc).__module__}.{type(exc).__name__}"
    err_sig = str(exc)[:200]  # trim to avoid long keys

    key = (alias, err_type, err_sig)
    now = time.monotonic()
    with _rl_lock:
        last = _last_diag_log.get(key, 0.0)
        if (now - last) >= RATE_LIMIT_SECONDS:
            _last_diag_log[key] = now
            return True

    # We still want to show *some* evidence that we suppressed a duplicate:
    logger.debug(
        f"[DB Diagnostics] Suppressing repeated diagnostics for alias='{alias}', "
        f"error={err_type} (last logged {now - last:.1f}s ago; threshold {RATE_LIMIT_SECONDS:.0f}s)."
    )
    return False


def _parse_statement_timeout(options_str: str | None) -> str | None:
    """
    Parse 'statement_timeout' from a PostgreSQL options string such as:
      '-c statement_timeout=10000ms -c search_path=public'
    Returns the value as a string (e.g., '10000ms') or None if not present.
    """
    if not options_str:
        return None
    # Look for '-c statement_timeout=<value>' in the options string.
    m = re.search(r"statement_timeout\s*=\s*(\S+)", options_str)
    return m.group(1) if m else None


def log_db_diagnostics_on_failure(alias: str = 'default', settings_dict: dict | None = None) -> None:
    """
    Log diagnostic information when a database connection fails.

    This includes:
      - The DB connection parameters (host, port, user, name).
      - Timeout-related config: CONN_MAX_AGE, OPTIONS.connect_timeout, OPTIONS.options (parsed for statement_timeout).
      # Connection usage statistics, if we can obtain a cursor (may not be possible on connection/auth failures).
      - SSL-related settings (sslmode, sslrootcert).


    Intended to help diagnose OperationalError / psycopg errors (timeouts, auth failures, etc.).
    """
    try:
        if not settings_dict:
            settings_dict = connections[alias].settings_dict

        # ---- Connection parameters ----
        logger.error("[DB Diagnostics] DB settings summary:")
        logger.error(f"  NAME: {settings_dict.get('NAME')}")
        logger.error(f"  USER: {settings_dict.get('USER')}")
        logger.error(f"  HOST: {settings_dict.get('HOST')}")
        logger.error(f"  PORT: {settings_dict.get('PORT')}")

        # ---- Timeouts and options ----
        opts = settings_dict.get('OPTIONS', {}) or {}
        logger.error("[DB Diagnostics] Timeout-related settings:")
        logger.error(f"  CONN_MAX_AGE: {settings_dict.get('CONN_MAX_AGE')!r}")
        logger.error(f"  OPTIONS.connect_timeout: {opts.get('connect_timeout')!r}")
        logger.error(f"  OPTIONS.options: {opts.get('options')!r}")
        logger.error(f"  Parsed statement_timeout: {_parse_statement_timeout(opts.get('options'))!r}")

        logger.error(f"[DB Diagnostics] SSL mode: {opts.get('sslmode', 'not set')!r}")
        logger.error(f"[DB Diagnostics] sslrootcert: {opts.get('sslrootcert')!r}")

    except Exception as e:
        logger.error(f"[DB Diagnostics] Could not retrieve DB settings: {e}")

    # ---- Postgres activity stats ----
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute("""
                           SELECT COUNT(*) FILTER (WHERE state = 'active') AS active,
                                  COUNT(*) FILTER (WHERE state = 'idle')   AS idle,
                                  COUNT(*)                                 AS total
                           FROM pg_stat_activity
                           WHERE datname = current_database();
                           """)
            active, idle, total = cursor.fetchone()

            cursor.execute("SHOW max_connections;")
            max_conn = int(cursor.fetchone()[0])

            logger.error(
                f"[DB Stats] total={total}, active={active}, idle={idle}, "
                f"max={max_conn}, usage={total}/{max_conn} ({(total / max_conn) * 100:.1f}%)"
            )
    except Exception as e:
        logger.error(f"[DB Stats] Failed to retrieve activity: {e}")


def patch_ensure_connection_with_diagnostics():
    """
    Patch BaseDatabaseWrapper.ensure_connection() to log diagnostics on failure.

    This is more reliable than patching connect() directly because many Django
    DB calls (including ORM queries) use ensure_connection().
    """
    global _PATCHED
    if _PATCHED:
        return

    original_ensure_connection = BaseDatabaseWrapper.ensure_connection

    def wrapped_ensure_connection(self):
        start = time.perf_counter()  # Always define first
        try:
            return original_ensure_connection(self)
        except (DjangoOperationalError, PostgresDriverError) as e:
            elapsed = time.perf_counter() - start
            if _should_log(self.alias, e):
                err_type = f"{type(e).__module__}.{type(e).__name__}"
                logger.error(
                    f"[DB ERROR] ensure_connection() failed for alias '{self.alias}' "
                    f"(duration: {elapsed:.3f}s): {err_type}: {e}"
                )
                logger.error("[DB Diagnostics] Traceback:\n" + "".join(traceback.format_exc()))
                log_db_diagnostics_on_failure(self.alias, settings_dict=getattr(self, "settings_dict", None))
            raise

    BaseDatabaseWrapper.ensure_connection = wrapped_ensure_connection
    _PATCHED = True
