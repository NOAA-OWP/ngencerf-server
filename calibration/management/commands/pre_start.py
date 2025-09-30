import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from calibration.enums import StatusEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun
from django.conf import settings
from cerfServer.settings import NgenEnvironmentEnum

logger = logging.getLogger(__name__)


# This should be run prior to starting the server to clean up any orphans

class Command(BaseCommand):
    help = "Clean up any jobs left in RUNNING status by marking them as SERVER_ERROR."

    def handle(self, *args, **options):
        logger.info("Starting cleanup of running jobs")

        try:
            if settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
                logger.info("Skipping cleanup (NGEN_ENVIRONMENT=PARALLEL_WORKS)")
                return

            with transaction.atomic():
                running_status = StatusEnum.RUNNING.db_instance
                error_status = StatusEnum.SERVER_ERROR.db_instance

                cal_count = CalibrationRun.objects.filter(status=running_status).update(status=error_status)
                logger.info(f'Updated {cal_count} calibration run records')

                val_count = ValidationRun.objects.filter(status=running_status).update(status=error_status)
                logger.info(f'Updated {val_count} validation run records')

                fcst_count = ForecastRun.objects.filter(status=running_status).update(status=error_status)
                logger.info(f'Updated {fcst_count} forecast run records')

            logger.info("Cleanup of running jobs completed successfully.")

        except Exception as e:
            logger.exception(f"Error during cleanup: {e}")
            raise CommandError(f"Cleanup failed: {e}")

