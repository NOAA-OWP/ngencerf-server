import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from calibration.enums import StatusEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun, ColdStartRun
from calibration.models.base_run import BaseRun
from calibration.views.calibration_run_views import get_slurm_status
from calibration.views.common import get_job_description
from cerfServer.settings import NgenEnvironmentEnum

logger = logging.getLogger(__name__)

RUN_MODELS = (CalibrationRun, ValidationRun, ForecastRun, ColdStartRun)


# This should be run prior to starting the server to clean up any orphans

class Command(BaseCommand):
    help = "Clean up any jobs left in RUNNING or SUBMITTED status by marking them as SERVER_ERROR."

    def handle(self, *args, **options):
        logger.info("Starting cleanup of running jobs")

        running_status = [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]
        error_status = StatusEnum.SERVER_ERROR.db_instance

        try:
            # ─────────────────────────────────────────────────────────────
            # Case 1: NOT on Parallel Works → we trust DB state only.
            # Safe to bulk mark *all* RUNNING entries immediately.
            # ─────────────────────────────────────────────────────────────
            if settings.NGEN_ENVIRONMENT != NgenEnvironmentEnum.PARALLEL_WORKS:

                total_count = 0
                for model in RUN_MODELS:
                    count = model.objects.filter(status__in=running_status).update(status=error_status)

                    logger.info(f'Updated {count} {model.__name__} records')
                    total_count += count

                logger.info(
                    f"Non-Parallel cleanup summary: updated {total_count} total job(s) to SERVER_ERROR."
                )
                return

            # ─────────────────────────────────────────────────────────────
            # Case 2: On Parallel Works → must check Slurm to confirm if
            # the job is *still actually running*, before setting error.
            # ─────────────────────────────────────────────────────────────
            total_running = 0  # total in DB with status=RUNNING
            total_marked_error = 0  # how many we actually updated

            def mark_error(run: BaseRun, reason: str):
                nonlocal total_marked_error
                total_marked_error += 1

                job_description = get_job_description(run)

                logger.warning(
                    f"Marking job {job_description} as SERVER_ERROR: "
                    f"slurm_job_id={run.slurm_job_id} — reason: {reason}"
                )
                # logger.warning(
                #     f"[DRY-RUN] Would mark job {job_description} as SERVER_ERROR "
                #     f"(slurm_job_id={run.slurm_job_id}) — reason: {reason}. "
                #     f"Status NOT changed."
                # )
                run.status = error_status
                run.save(update_fields=["status"])

            # Iterate across all job models
            for model in RUN_MODELS:
                # Only jobs that are *currently marked* RUNNING/SUBMITTED in the DB
                for run in model.objects.filter(status__in=running_status):
                    total_running += 1

                    if not run.slurm_job_id:
                        mark_error(run, "No slurm_job_id (definitely orphaned)")
                        continue

                    is_active, slurm_status = get_slurm_status(run.slurm_job_id)

                    logger.debug(
                        f"{get_job_description(run)}: "
                        f"Slurm active={is_active}, status={slurm_status}"
                    )

                    if not is_active:
                        mark_error(
                            run,
                            f"Slurm reports inactive (status={slurm_status})"
                        )
                    else:
                        logger.info(
                            f"Job still active on Slurm: "
                            f"{model.__name__}(id={run.id}, slurm_job_id={run.slurm_job_id})"
                        )

            logger.info(
                f"Parallel Works cleanup summary: "
                f"{total_marked_error} job(s) marked SERVER_ERROR "
                f"out of {total_running} RUNNING/SUBMITTED."
            )

            logger.info("Cleanup of running jobs completed successfully.")

        except Exception as e:
            logger.exception(f"Error during cleanup: {e}")
            raise CommandError(f"Cleanup failed: {e}")
