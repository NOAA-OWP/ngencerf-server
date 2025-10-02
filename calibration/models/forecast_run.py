from django.db import models

from calibration.models.base_run import BaseRun


class ForecastRun(BaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="forecasts_from_calibration", on_delete=models.CASCADE, db_index=True)
    cold_start_run = models.ForeignKey('ColdStartRun', null=True, related_name="forecasts_from_cold_start", on_delete=models.CASCADE, db_index=True)
    configuration = models.ForeignKey("ForecastConfiguration", null=False, on_delete=models.RESTRICT)
    cycle_date = models.DateTimeField(null=False)

    class Meta:
        db_table = 'forecast_run'

    def __str__(self):
        return (
            f"ForecastRun {self.id}, "
            f"configuration: {self.configuration.name}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
