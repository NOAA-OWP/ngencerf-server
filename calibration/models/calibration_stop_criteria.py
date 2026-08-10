from django.db import models

from calibration.models.base_model import BaseModel
from calibration.models.calibration_run import CalibrationRun


class CalibrationStopCriteria(BaseModel):
    description = models.TextField(null=False)
    calibration_run = models.ForeignKey(CalibrationRun, null=False, on_delete=models.CASCADE)
    value = models.IntegerField()

    class Meta:
        db_table = 'calibration_stop_criteria'
        constraints = [
            models.UniqueConstraint(fields=['calibration_run'], name='uq_stopcriteria_per_run'),
        ]
