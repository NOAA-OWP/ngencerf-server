from django.db import models

from calibration.models.base_run import BaseRun


class ValidationRun(BaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="validations", on_delete=models.CASCADE, db_index=True)
    iteration = models.ForeignKey('Iteration', null=True, on_delete=models.CASCADE)
    validation_type = models.CharField(max_length=20, null=False)
    validation_worker_name = models.CharField(max_length=20, null=True)

    class Meta:
        db_table = 'validation_run'
        indexes = [
            models.Index(fields=['calibration_run', 'validation_type'], name='idx_validationrun_run_type'),
            models.Index(fields=['calibration_run'], name='idx_validation_calibration_run'),
            models.Index(fields=['status'], name='idx_validation_status'),
        ]

    @property
    def worker_name(self):
        return self.iteration.worker_name if self.iteration else None

    @property
    def iteration_num(self):
        return self.iteration.iteration_num if self.iteration else None

    def __str__(self):
        return (
            f"ValidationRun {self.id}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"validation_type: {self.validation_type}, "
            f"status.name: {self.status.name}"
        )
