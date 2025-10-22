from django.contrib.auth import get_user_model
from django.db import models

from calibration.models.base_run import BaseRun
from calibration.models.forecast_run import ForecastRun


class VerificationRun(BaseRun):
    owner = models.ForeignKey(get_user_model(), null=False, on_delete=models.RESTRICT, db_index=True)
    forecast_run = models.ForeignKey(ForecastRun, null=True, on_delete=models.CASCADE, db_index=True)
    verification_config = models.TextField(null=True)
    job_data_dir = models.CharField(max_length=255, null=False)

    class Meta:
        db_table = 'verification_run'
        indexes = [
            models.Index(fields=['forecast_run'], name='idx_verif_forecast_run'),
            models.Index(fields=['owner'], name='idx_verif_owner'),
            models.Index(fields=['status'], name='idx_verif_status'),
        ]

    def __str__(self):
        return (
            f"VerificationRun {self.id}, "
            f"owner: {self.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
