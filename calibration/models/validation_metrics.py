from django.db import models

from calibration.models.base_model import BaseModel


class ValidationMetrics(BaseModel):
    run_type = models.CharField(max_length=25, null=False)
    period = models.CharField(max_length=20, null=False)
    metric = models.ForeignKey('Metric', null=False, on_delete=models.RESTRICT)
    metric_value = models.FloatField(null=False)
    validation_run = models.ForeignKey('ValidationRun', null=False, on_delete=models.CASCADE)

    class Meta:
        db_table = 'validation_metric'
        constraints = [
            models.UniqueConstraint(fields=['metric', 'period',  'run_type', 'validation_run'], name='validation_metric__metric__period__run_type__validation_run__unique')
        ]

    def __str__(self):
        return (
            f"ValidationMetrics: {self.id}, "
            f"Metric: {self.metric.name:10}, "
            f"Value: {self.metric_value}, "
            f"Validation Run: {self.validation_run_id}"
        )
