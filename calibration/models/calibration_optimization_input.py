from django.db import models

from calibration.models.base_model import BaseModel


class CalibrationOptimizationInput(BaseModel):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, on_delete=models.CASCADE, db_index=True)
    optimization_input = models.ForeignKey('OptimizationInput', null=False, on_delete=models.CASCADE, db_index=True)
    value = models.FloatField(null=False)

    class Meta:
        db_table = 'calibration_optimization_input'
        constraints = [
            models.UniqueConstraint(
                fields=['calibration_run', 'optimization_input'],
                name='uq_optinput_per_run'
            ),
        ]
        indexes = [
            models.Index(fields=['calibration_run', 'optimization_input'], name='idx_optinput_run_input'),
        ]

    def __str__(self):
        return (
            f"CalibrationOptimizationInput: {self.id}, "
            f"Optimization input: ({self.optimization_input}), "
            f"Value: {self.value}"
        )
