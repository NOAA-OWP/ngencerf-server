import datetime
import hashlib
import logging
import os
import re
import socket
import ssl
import threading
import time
import traceback

import psycopg
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


def _test_ssl_handshake(settings_dict: dict):
    """
    Attempt a one-off connection to verify SSL handshake and log cert details.
    Confirms if psycopg is using the provided sslrootcert or falling back.
    """
    opts = settings_dict.get("OPTIONS", {}) or {}
    sslmode = opts.get("sslmode", "require")
    sslrootcert = opts.get("sslrootcert")

    dsn = (
        f"dbname={settings_dict['NAME']} "
        f"user={settings_dict['USER']} "
        f"host={settings_dict['HOST']} "
        f"port={settings_dict['PORT']} "
        f"sslmode={sslmode} "
    )
    if sslrootcert:
        dsn += f"sslrootcert={sslrootcert} "

    logger.error("[DB Diagnostics] Starting SSL handshake test...")

    try:
        with psycopg.connect(dsn, connect_timeout=5, options="-c statement_timeout=5000") as conn:
            logger.error("[DB Diagnostics] SSL handshake test: SUCCESS")

            # psycopg 3.2.9: SSL object may be None if handshake failed or not required
            ssl_obj = getattr(conn.info, "ssl_object", None)
            if ssl_obj:
                try:
                    cert = ssl_obj.getpeercert()
                    logger.error(f"  Server certificate subject: {cert.get('subject')}")
                    logger.error(f"  Server certificate issuer: {cert.get('issuer')}")
                    logger.error(f"  Server certificate notBefore: {cert.get('notBefore')}")
                    logger.error(f"  Server certificate notAfter:  {cert.get('notAfter')}")
                except Exception as cert_exc:
                    logger.error(f"  Could not extract server cert details: {cert_exc}")

                # NEW: Log which CA file OpenSSL actually used
                try:
                    ca_file_used = ssl_obj.context.get_ca_certs()
                    logger.error(f"  Trusted CA certs loaded: {len(ca_file_used)}")
                    if sslrootcert:
                        logger.error(f"  Requested sslrootcert: {sslrootcert}")
                        logger.error("  psycopg is expected to honor this file if valid.")
                    else:
                        logger.error("  No sslrootcert specified; psycopg relied on system defaults.")
                except Exception as ca_exc:
                    logger.error(f"  Could not confirm CA file used: {ca_exc}")
            else:
                logger.error("  No SSL object available — server may not require SSL.")
    except Exception as ssl_exc:
        logger.error(f"[DB Diagnostics] SSL handshake test FAILED: {ssl_exc}")
        msg = str(ssl_exc).lower()
        if "certificate verify failed" in msg:
            logger.error("[DB Diagnostics] Certificate validation failed — likely missing or incorrect sslrootcert.")
        elif "connection reset" in msg:
            logger.error("[DB Diagnostics] Connection reset during SSL handshake — possible mismatch between client and server SSL settings.")


def _parse_statement_timeout(options_str: str | None) -> str | None:
    """
    Parse 'statement_timeout' from a PostgreSQL options string such as:
      '-c statement_timeout=10000ms -c search_path=public'
    Returns the value as a string (e.g., '10000ms') or None if not present.
    """
    if not options_str:
        return None
    # Look for 'statement_timeout=<value>' anywhere in the string.
    m = re.search(r"statement_timeout\s*=\s*(\S+)", options_str)
    return m.group(1) if m else None


def _log_ca_certificate_info(sslrootcert: str | None):
    try:
        # Always log the initial sslrootcert setting
        logger.error(f"[DB Diagnostics] sslrootcert setting: {sslrootcert!r}")

        if sslrootcert:
            logger.error(f"[DB Diagnostics] Explicit sslrootcert provided: {sslrootcert!r}")
            if os.path.isfile(sslrootcert):
                size = os.path.getsize(sslrootcert)
                logger.error(f"  SSL root cert exists: YES ({size} bytes)")

                # SHA256 fingerprint
                with open(sslrootcert, "rb") as f:
                    cert_bytes = f.read()
                sha256 = hashlib.sha256(cert_bytes).hexdigest()
                logger.error(f"  SSL root cert SHA256: {sha256}")

                # Expiration date
                try:
                    x509 = ssl._ssl._test_decode_cert(sslrootcert)
                    exp_date = datetime.datetime.strptime(
                        x509['notAfter'], "%b %d %H:%M:%S %Y %Z"
                    )
                    logger.error(f"  SSL root cert expires: {exp_date} (UTC)")
                except Exception as cert_exc:
                    logger.error(f"  Could not parse cert expiration: {cert_exc}")
            else:
                logger.error("  SSL root cert exists: NO (check path!)")

        else:
            # No sslrootcert → psycopg will rely on system defaults
            default_paths = ssl.get_default_verify_paths()
            cafile = os.path.abspath(default_paths.cafile) if default_paths.cafile else None
            capath = os.path.abspath(default_paths.capath) if default_paths.capath else None

            logger.error("[DB Diagnostics] No sslrootcert provided — using system defaults:")
            logger.error(
                f"  Default CA file: {cafile!r} "
                f"({'exists' if cafile and os.path.isfile(cafile) else 'missing'})"
            )
            logger.error(
                f"  Default CA path: {capath!r} "
                f"({'exists' if capath and os.path.isdir(capath) else 'missing'})"
            )
            logger.error("[DB Diagnostics] psycopg will trust system CA bundle unless "
                         "sslmode=require and the server cert is self-signed.")
            logger.error("[DB Diagnostics] To confirm CA validation, "
                         "set OPTIONS.sslrootcert explicitly in DATABASES "
                         "and compare against system defaults.")

    except Exception as e:
        logger.error(f"[DB Diagnostics] Failed to log CA cert info: {e}")


def log_db_diagnostics_on_failure(alias: str = 'default', settings_dict: dict | None = None) -> None:
    """
    Log diagnostic information when a database connection fails.

    This includes:
      - The DB connection parameters (host, port, user, name).
      - Timeout-related config: CONN_MAX_AGE, OPTIONS.connect_timeout, OPTIONS.options (parsed for statement_timeout).
      - Connection usage statistics, IF we can get a cursor (often not possible on hard failures).
      - SSL settings and CA certs.

    Intended to help diagnose OperationalError / psycopg errors (timeouts, auth failures, etc.).
    """
    try:
        if not settings_dict:
            settings_dict = connections[alias].settings_dict

        # ---- Connection parameters ----
        logger.error("[DB Diagnostics] Database settings used:")
        logger.error(f"  NAME: {settings_dict.get('NAME')}")
        logger.error(f"  USER: {settings_dict.get('USER')}")
        logger.error(f"  HOST: {settings_dict.get('HOST')}")
        logger.error(f"  PORT: {settings_dict.get('PORT')}")

        # ---- Timeouts and options ----
        opts = settings_dict.get('OPTIONS', {}) or {}
        conn_max_age = settings_dict.get('CONN_MAX_AGE')
        connect_timeout = opts.get('connect_timeout')
        options_raw = opts.get('options')
        stmt_timeout = _parse_statement_timeout(options_raw)

        logger.error("[DB Diagnostics] Timeout-related settings:")
        logger.error(f"  CONN_MAX_AGE: {conn_max_age!r}")
        logger.error(f"  OPTIONS.connect_timeout: {connect_timeout!r}")
        logger.error(f"  OPTIONS.options: {options_raw!r}")
        logger.error(f"  Parsed statement_timeout: {stmt_timeout!r}")

        # ---- SSL info ----
        sslmode = opts.get('sslmode', 'not set')
        sslrootcert = opts.get('sslrootcert')

        logger.error("[DB Diagnostics] SSL configuration:")
        logger.error(f"  sslmode: {sslmode!r}")
        _log_ca_certificate_info(sslrootcert)

        # ---- SSL handshake diagnostics ----
        _test_ssl_handshake(settings_dict)

        # ---- Host & thread info ----
        hostname = socket.gethostname()
        thread = threading.current_thread().name
        logger.error(f"[DB Diagnostics] Hostname: {hostname}, Thread: {thread}")

        # ---- DNS resolution timing ----
        host = settings_dict.get('HOST')
        port = settings_dict.get('PORT')
        try:
            dns_start = time.perf_counter()
            resolved = socket.getaddrinfo(host, port)
            dns_elapsed = time.perf_counter() - dns_start
            logger.error(
                f"[DB Diagnostics] DNS resolution for host '{host}' "
                f"took {dns_elapsed:.3f}s → {resolved[0][4][0]}"
            )
        except Exception as dns_exc:
            logger.error(f"[DB Diagnostics] DNS resolution failed for host '{host}': {dns_exc}")

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
                f"[DB Stats on Failure] total={total}, active={active}, idle={idle}, "
                f"max={max_conn}, usage={total}/{max_conn} ({(total / max_conn) * 100:.1f}%)"
            )
    except Exception as stats_exc:
        # This is expected if the DB is completely unreachable or authentication failed.
        logger.error(f"[DB Stats] Could not retrieve connection stats: {stats_exc}")


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
                logger.error("Traceback:\n" + "".join(traceback.format_exc()))
                log_db_diagnostics_on_failure(
                    self.alias, settings_dict=getattr(self, "settings_dict", None)
                )
            raise

    BaseDatabaseWrapper.ensure_connection = wrapped_ensure_connection
    _PATCHED = True
