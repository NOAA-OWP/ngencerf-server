from django.db import models

from calibration.models.base_run import BaseRun
from calibration.models.forecast_run import ForecastRun


class VerificationRun(BaseRun):
    forecast_run = models.ForeignKey(ForecastRun, null=False, on_delete=models.CASCADE, db_index=True)

    class Meta:
        db_table = 'verification_run'
        indexes = [
            models.Index(fields=['forecast_run'], name='idx_verif_forecast_run'),
            models.Index(fields=['status'], name='idx_verif_status'),
        ]

    def __str__(self):
        return (
            f"VerificationRun {self.id}, "
            f"forecast_run_id: {self.forecast_run_id}, "
            f"status.name: {self.status.name}"
        )
