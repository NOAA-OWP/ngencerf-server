import logging
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from calibration.enums import StatusEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun, ColdStartRun
from calibration.models.base_run import BaseRun
from calibration.views.common import get_job_description
from cerfServer.settings import NgenEnvironmentEnum

logger = logging.getLogger(__name__)

RUN_MODELS = (CalibrationRun, ValidationRun, ForecastRun, ColdStartRun)


# This should be run prior to starting the server to clean up any orphans

class Command(BaseCommand):
    help = "Clean up any jobs left in RUNNING status by marking them as SERVER_ERROR."

    def handle(self, *args, **options):
        logger.info("Starting cleanup of running jobs")

        running_status = StatusEnum.RUNNING.db_instance
        error_status = StatusEnum.SERVER_ERROR.db_instance

        try:
            # ─────────────────────────────────────────────────────────────
            # Case 1: NOT on Parallel Works → we trust DB state only.
            # Safe to bulk mark *all* RUNNING entries immediately.
            # ─────────────────────────────────────────────────────────────
            if settings.NGEN_ENVIRONMENT != NgenEnvironmentEnum.PARALLEL_WORKS:

                total_count = 0
                for model in RUN_MODELS:
                    count = model.objects.filter(status=running_status).update(status=error_status)
                    logger.info(f'Updated {count} {model.__name__} records')
                    total_count += count

                logger.info(
                    f"Non-Parallel cleanup summary: updated {total_count} total job(s) to SERVER_ERROR."
                )


            else:
                # ─────────────────────────────────────────────────────────────
                # Case 2: On Parallel Works → must check Slurm to confirm if
                # the job is *still actually running*, before setting error.
                # ─────────────────────────────────────────────────────────────
                total_running = 0  # total in DB with status=RUNNING
                total_marked_error = 0  # how many we actually updated

                def mark_error(run: BaseRun, reason: str):
                    # Log with rich context: includes job type, id, name, slurm ID
                    nonlocal total_marked_error
                    total_marked_error += 1
                    job_description = get_job_description(run)
                    logger.warning(
                        f"Marking job {job_description} as SERVER_ERROR: "
                        f"slurm_job_id={run.slurm_job_id}) — reason: {reason}"
                    )
                    run.status = error_status
                    run.save(update_fields=["status"])

                base_url = urljoin(settings.SLURM_URL, settings.SLURM_STATUS_ENDPOINT)

                # Iterate across all job models
                for model in RUN_MODELS:
                    # Only jobs that are *currently marked* RUNNING in the DB
                    for run in model.objects.filter(status=running_status):
                        total_running += 1
                        slurm_id = run.slurm_job_id

                        if not slurm_id:
                            # Definitely orphaned — no record of Slurm job
                            mark_error(run, "No slurm_job_id (definitely orphaned).")
                            continue

                        # Query Slurm for the live job status
                        url = f"{base_url}?slurm_job_id={slurm_id}"

                        try:
                            resp = requests.get(url, timeout=10)
                            data = resp.json()

                            # Treat any error or non-RUNNING status as a failed job
                            if "error" in data:
                                mark_error(run, f"Slurm returned error: {data['error']}")
                            elif data.get("status") != "RUNNING":
                                mark_error(run, f"Slurm status is {data.get('status')!r}, not RUNNING.")
                            else:
                                # Job is truly still running — leave as-is
                                logger.info(
                                    f"Job still RUNNING on Slurm: "
                                    f"{model.__name__}(id={run.id}, slurm_job_id={slurm_id}) — leaving untouched."
                                )
                        except Exception as ex:
                            # Network/timeout/etc → safest assumption: job is gone
                            mark_error(run, f"Exception querying Slurm: {ex!r}")

                logger.info(
                    f"Parallel Works cleanup summary: "
                    f"{total_marked_error} job(s) marked SERVER_ERROR out of {total_running} RUNNING."
                )

            logger.info("Cleanup of running jobs completed successfully.")

        except Exception as e:
            logger.exception(f"Error during cleanup: {e}")
            raise CommandError(f"Cleanup failed: {e}")
