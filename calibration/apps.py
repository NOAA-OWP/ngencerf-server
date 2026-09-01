import logging
import os
import sys

from django.apps import AppConfig
from django.conf import settings

from calibration.auth.active_directory_config import validate_active_directory_settings
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
            cmd = sys.argv[1] if len(sys.argv) > 1 else os.path.basename(sys.argv[0])
            logger.info(f'*** Running {cmd}')

        logger.info(f'Environment: {settings.JOB_EXECUTION_MODE}')
        log_worker_info()

        if running_dev_server or running_gunicorn:
            validate_active_directory_settings()

        # ------------------------------------------------------------------
        # ALWAYS display Git, DB and environment info
        # ------------------------------------------------------------------
        logger.info('')
        print_git_info_all()

        logger.info('')
        print_db_info()
        logger.info('')

        if running_dev_server or running_gunicorn:
            logger.info(f'NGENCERF_BASE_URL: {settings.NGENCERF_BASE_URL}\n')
            logger.info(f'ENTERPRISE_DATA_URL: {settings.ENTERPRISE_DATA_URL}\n')
            logger.info(f'CONTAINER_DATA_ROOT: {settings.CONTAINER_DATA_ROOT}')
            logger.info(f'HOST_DATA_ROOT: {settings.HOST_DATA_ROOT}')
            logger.info(f'NGEN_STATIC_DIR: {settings.NGEN_STATIC_DIR}')
            logger.info(f'NGENCERF_ARCHIVE_S3_PATH: {settings.NGENCERF_ARCHIVE_S3_PATH}')
            logger.info(f'NGENCERF_ZIPS_S3_PATH: {settings.NGENCERF_ZIPS_S3_PATH}')
            logger.info(f"FORCING_AORC_CONUS_BMI_DATE_RANGE: {settings.FORCING_AORC_CONUS_BMI_DATE_RANGE}")
            logger.info(f'DJANGO DEBUG: {settings.DEBUG}')
            logger.info(f"MPI_NODE_RULES: {settings.MPI_NODE_RULES}")
            logger.info(f"NODE_TYPE_RULES: {settings.SLURM_NODE_TYPE_RULES}")

            from calibration.util.ngen_locations import check_files

            check_files()

        # Diagnostics wrapper for DB
        patch_ensure_connection_with_diagnostics()
