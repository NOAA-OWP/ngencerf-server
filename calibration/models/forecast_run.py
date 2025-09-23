from django.db import models

from calibration.models.base_run import BaseRun


class ForecastRun(BaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="forecasts", on_delete=models.CASCADE, db_index=True)
    cycle = models.ForeignKey("ForecastCycle", null=False, on_delete=models.RESTRICT)
    cycle_date = models.DateTimeField(null=True)
    cold_start_date = models.DateTimeField(null=True)
    forcing_download_run = models.OneToOneField('ForecastForcingDownloadRun', null=False, on_delete=models.CASCADE, related_name='forecast_run')
    
    class Meta:
        db_table = 'forecast_run'

    def __str__(self):
        return (
            f"ForecastRun {self.id}, "
            f"cycle {self.cycle.name}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
