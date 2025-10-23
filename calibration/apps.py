import logging
import os
import sys

from django.apps import AppConfig
from django.conf import settings
from django.core.cache import caches

from calibration.util.db_diagnostics import patch_ensure_connection_with_diagnostics
from calibration.util.git_util import print_git_info_all

logger = logging.getLogger(__name__)


def print_db_info():
    db_info = settings.DATABASES['default']
    logger.info(f"Database Engine: {db_info['ENGINE']}")
    logger.info(f"Database Name: {db_info['NAME']}")
    logger.info(f"Database URL: {db_info['HOST']}:{db_info['PORT']}")
    logger.info(f"Database User: {db_info['USER']}")


def log_worker_info():
    pid = os.getpid()
    argv = " ".join(sys.argv)
    logger.info(f"Worker PID: {pid} | argv: {argv}")


def print_banner():
    RED = "\33[91m"
    BLUE = "\33[94m"
    GREEN = "\033[32m"
    YELLOW = "\033[93m"
    PURPLE = '\033[0;35m'
    CYAN = "\033[36m"
    END = "\033[0m"

    banner = f"""
    {CYAN}
███╗   ██╗ ██████╗ ███████╗███╗   ██╗ ██████╗███████╗██████╗ ███████╗
████╗  ██║██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔════╝██╔══██╗██╔════╝
██╔██╗ ██║██║  ███╗█████╗  ██╔██╗ ██║██║     █████╗  ██████╔╝█████╗  
██║╚██╗██║██║   ██║██╔══╝  ██║╚██╗██║██║     ██╔══╝  ██╔══██╗██╔══╝ta  
██║ ╚████║╚██████╔╝███████╗██║ ╚████║╚██████╗███████╗██║  ██║██║     
╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚══════╝╚═╝  ╚═╝╚═╝     
                                                                     
███████╗███████╗██████╗ ██╗   ██╗███████╗██████╗                     
██╔════╝██╔════╝██╔══██╗██║   ██║██╔════╝██╔══██╗                    
███████╗█████╗  ██████╔╝██║   ██║█████╗  ██████╔╝                    
╚════██║██╔══╝  ██╔══██╗╚██╗ ██╔╝██╔══╝  ██╔══██╗                    
███████║███████╗██║  ██║ ╚████╔╝ ███████╗██║  ██║                    
╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝ {END}"""

    logger.info(banner)


class CalibrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'calibration'

    def ready(self):
        # Check if we're running the server or a management command
        running_server = (
                'runserver' in sys.argv
                or 'runsslserver' in sys.argv
                or any('gunicorn' in arg for arg in sys.argv)
        )

        if running_server:
            print_banner()
        else:
            logger.info(f'*** Running {sys.argv[1]}')

        logger.info(f'Environment: {settings.NGEN_ENVIRONMENT_STR}')
        log_worker_info()
        if running_server:
            # ------------------------------------------------------------------
            # Clear Django file-based cache at startup (runs once per worker)
            #
            # Note:
            #   This is technically overkill since all workers share the same
            #   file-based cache directory, but Gunicorn doesn’t provide an
            #   easy way to execute initialization logic just once at master
            #   startup. Clearing here is harmless and ensures a clean cache.
            # -------------------------------------------------------------
            try:
                cache = caches['default']
                cache.clear()
                logger.info(f'Cleared Django file-based cache at {settings.CACHE_DIRECTORY}')

                # --- SANITY TEST ---
                test_key = "cache_sanity_check_key"
                cache.set(test_key, "OK", timeout=None)
                if cache.get(test_key) == "OK":
                    logger.info("Django file-based cache is WORKING (write/read success)")
                else:
                    logger.warning("Django cache SET/GET check FAILED — likely fallback to LocMemCache or DummyCache")

            except Exception as e:
                logger.error(f'Django cache failed during init SET/GET check: {e}')

            logger.info('')
            print_git_info_all()

        logger.info('')
        print_db_info()
        logger.info('')
        logger.info(f'NGWPC Enterprise Data Server url: {settings.ENTERPRISE_DATA_URL}\n')
        logger.info(f'NGEN_CAL_MOUNT_POINT - {settings.NGEN_CAL_MOUNT_POINT}')
        logger.info(f'NGEN_STATIC_DIR - {settings.NGEN_STATIC_DIR}')
        logger.info(f'DJANGO DEBUG - {settings.DEBUG}')

        from calibration.util.ngen_locations import check_files

        check_files()

        patch_ensure_connection_with_diagnostics()
