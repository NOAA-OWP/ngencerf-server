from django.db import models

from calibration.models.base_model import BaseModel


class CalibrationFormulation(BaseModel):
    module = models.ForeignKey('Module', null=False, on_delete=models.RESTRICT)
    calibration_run = models.ForeignKey('CalibrationRun', null=False, on_delete=models.CASCADE)

    class Meta:
        db_table = 'calibration_formulation'
        constraints = [
            models.UniqueConstraint(
                fields=['module', 'calibration_run'],
                name='calibration_formulation__module__calibration_run__unique'
            )
        ]
        indexes = [
            models.Index(fields=['calibration_run'], name='idx_formulation_run'),
            models.Index(fields=['module'], name='idx_formulation_module'),
            models.Index(fields=['calibration_run', 'module'], name='idx_formulation_run_module'),
        ]

    def __str__(self):
        return (
            f"CalibrationFormulation: {self.id}, "
            f"module: {self.module.name:20} ({self.module.id}), "
            f"calibration_run: {self.calibration_run.id}"
        )
