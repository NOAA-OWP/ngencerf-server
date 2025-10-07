from django.db import models

from calibration.models.base_run import BaseRun


class ColdStartRun(BaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="forecasts", on_delete=models.CASCADE, db_index=True)
    configuration = models.ForeignKey("ForecastConfiguration", null=False, on_delete=models.RESTRICT)
    cold_start_date = models.DateTimeField(null=True)

    class Meta:
        db_table = 'cold_start_run'
        indexes = [
            models.Index(fields=['calibration_run'], name='idx_coldstart_calibration_run'),
            models.Index(fields=['status'], name='idx_coldstart_status'),
            models.Index(fields=['cold_start_date'], name='idx_coldstart_date'),
        ]

    def __str__(self):
        return (
            f"ColdStartRun {self.id}, "
            f"cycle: {self.configuration.name}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
