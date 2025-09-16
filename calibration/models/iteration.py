from django.db import models

from calibration.models.base_model import BaseModel


class Iteration(BaseModel):
    iteration_num = models.IntegerField(null=False)
    calibration_run = models.ForeignKey('CalibrationRun', null=False, on_delete=models.CASCADE)
    objective_function_value = models.FloatField(null=True)
    worker_name = models.TextField(null=False)
    worker_number = models.PositiveIntegerField(null=False)
    best_params = models.BooleanField(null=False, default=False)

    class Meta:
        db_table = 'iteration'
        constraints = [
            models.UniqueConstraint(fields=['iteration_num', 'worker_name', 'calibration_run'],
                                    name='iteration_iteration_num_worker_calibration_run__unique')
        ]

        indexes = [
            models.Index(fields=['calibration_run'], name='idx_iteration_run'),
            models.Index(fields=['worker_number'], name='idx_iteration_worker_number'),
            models.Index(fields=['calibration_run', 'worker_name', '-iteration_num'], name="idx_iter_run_worker_num_desc")
        ]

    def __str__(self):
        return (
            f"Iteration: {self.id}, "
            f"iteration_num: {self.iteration_num}, "
            f"worker_name: {str(self.worker_name)}, "
            f"worker_number: {self.worker_number}"
        )
