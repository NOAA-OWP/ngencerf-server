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
    banner = """

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
╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝ """

    logger.info(banner)


class CalibrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'calibration'

    def ready(self):
        # -------------------------------------------------------------
        # Detect dev server or gunicorn
        # -------------------------------------------------------------
        running_dev_server = any(cmd in sys.argv for cmd in ('runserver', 'runsslserver'))
        running_gunicorn = any('gunicorn' in arg for arg in sys.argv)

        # -------------------------------------------------------------
        # Banner + basic info
        # -------------------------------------------------------------
        if running_dev_server or running_gunicorn:
            print_banner()
        else:
            # Management command
            logger.info(f'*** Running {sys.argv[1]}')

        logger.info(f'Environment: {settings.NGEN_ENVIRONMENT_STR}')
        log_worker_info()

        # ------------------------------------------------------------------
        # ALWAYS display Git, DB and environment info
        # ------------------------------------------------------------------
        logger.info('')
        print_git_info_all()

        logger.info('')
        print_db_info()
        logger.info('')

        logger.info(f'NGWPC Enterprise Data Server url: {settings.ENTERPRISE_DATA_URL}\n')
        logger.info(f'NGEN_CAL_MOUNT_POINT: {settings.NGEN_CAL_MOUNT_POINT}')
        logger.info(f'NGEN_STATIC_DIR: {settings.NGEN_STATIC_DIR}')
        logger.info(f'DJANGO DEBUG: {settings.DEBUG}')

        from calibration.util.ngen_locations import check_files

        check_files()

        # Diagnostics wrapper for DB
        patch_ensure_connection_with_diagnostics()
