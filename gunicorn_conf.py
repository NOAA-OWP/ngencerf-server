import logging
import os

from calibration.umask_debug import install_umask_trap

# =====================================================================
# IMPORTANT — READ THIS FIRST
# =====================================================================
#
# This Gunicorn config EXPECTS that Gunicorn is started with:
#
#       --preload
#
# Without --preload:
#   • Django is NOT loaded in the master process
#   • settings are unavailable
#   • when_ready() cannot safely access Django APIs
#
# With --preload:
#   • Django loads ONCE in the MASTER
#   • Workers fork from an initialized Django state
#   • umask trap installs cleanly
#
# runCerf.sh script already uses --preload.
#
# =====================================================================

# ============================================================
# Gunicorn native logging settings (Option A — disable noise)
# ============================================================

# Disable access logs entirely
accesslog = None

# Disable Gunicorn's own stderr logging format
# (Django logging already handles everything cleanly)
errorlog = "-"

# Ensure Gunicorn does NOT add its own handlers
loglevel = "info"
capture_output = False
disable_existing_loggers = True


# =====================================================================
# 1. Hook: Configure Gunicorn loggers to use Django handlers
# =====================================================================

def _configure_logging(worker=None):
    """
    Attach Django's root logger handlers to the Gunicorn master logger only.

    DO NOT attach handlers to worker.log.logger when using UvicornWorker,
    because Uvicorn already forwards logs to Gunicorn, and adding handlers
    here will cause duplicate log messages.
    """
    try:
        # Django has already installed its handlers by the time loggers
        # are used here (because of --preload).
        django_root = logging.getLogger()
        django_handlers = django_root.handlers

        # Gunicorn master logger
        g_master = logging.getLogger("gunicorn.error")
        g_master.setLevel(logging.INFO)

        # Avoid duplicate handler attachment
        existing = {id(h) for h in g_master.handlers}
        for h in django_handlers:
            if id(h) not in existing:
                g_master.addHandler(h)

    except Exception as e:
        # Early import failures are expected before Django initializes.
        print(f"[gunicorn_conf] Logging hook failed (safe to ignore early): {e}")


# =====================================================================
# 2. Hook: Master is starting (Django not yet loaded here)
# =====================================================================

def on_starting(_server):
    master_logger = logging.getLogger("gunicorn.error")
    master_logger.info("[gunicorn_conf] Master starting up (PID=%s)", os.getpid())


# =====================================================================
# 3. Hook: Master is ready AFTER preload (Django loaded HERE)
# =====================================================================

def when_ready(_server):
    """
    Runs once in the master AFTER Django is loaded.
    Just configure logging.
    """
    _configure_logging()

    logger = logging.getLogger("gunicorn.error")
    logger.info("[gunicorn_conf] Master ready (PID=%s)", os.getpid())


# =====================================================================
# 4. Hook: Worker initialization (per worker)
# =====================================================================

def post_fork(_server, worker):
    """
    Runs once for each worker (initial and respawned).
    Install umask trap + enforce umask for each worker.
    """
    install_umask_trap()

    _configure_logging(worker)

    # Enforce umask per-worker
    os.umask(0o022)

    worker.log.info(
        "[gunicorn_conf] post_fork: Worker started PID=%s with umask=022",
        worker.pid
    )


# =====================================================================
# 5. Hook: Worker exiting
# =====================================================================

def worker_exit(_server, worker):
    logging.getLogger("gunicorn.error").warning(
        "[gunicorn_conf] worker_exit: Worker PID=%s exiting", worker.pid
    )


# =====================================================================
# 6. Optional debugging hooks
# =====================================================================

def worker_int(worker):
    worker.log.warning("[gunicorn_conf] worker_int: Worker %s got SIGINT", worker.pid)


def worker_abort(worker):
    worker.log.error("[gunicorn_conf] worker_abort: Worker %s aborted", worker.pid)
