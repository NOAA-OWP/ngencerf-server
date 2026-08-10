from django.db import models

from calibration.models.base_model import BaseModel


class CalibrationParameter(BaseModel):
    calibration_formulation = models.ForeignKey('CalibrationFormulation', null=False, on_delete=models.CASCADE, db_index=True)
    name = models.CharField(max_length=255, null=False)
    description = models.TextField(null=False)
    data_type = models.CharField(max_length=50, null=False)
    initial_value = models.FloatField(null=True)
    minimum = models.FloatField(null=True)
    maximum = models.FloatField(null=True)
    units = models.CharField(max_length=50, null=True)
    user_selected_for_tuning = models.BooleanField(default=False)

    class Meta:
        db_table = 'calibration_parameter'
        constraints = [
            models.UniqueConstraint(
                fields=['name', 'calibration_formulation'],
                name='calibration_tune_parameter__name__calibration_formulation__unique',
            ),
        ]
        indexes = [
            models.Index(fields=['calibration_formulation'], name='idx_parameter_formulation'),
            models.Index(fields=['calibration_formulation', 'user_selected_for_tuning'],
                         name='idx_param_formulation_selected'),
        ]

    def __str__(self):
        return (
            f"CalibrationParameter: {self.id}, "
            f"name: {self.name:20},"
            f"user_selected_for_tuning: {str(self.user_selected_for_tuning):<5}, "
            f"calibration_formulation: {self.calibration_formulation.id} ({self.calibration_formulation.module.name}), "
            f"calibration_run: {self.calibration_formulation.calibration_run_id}"
        )
