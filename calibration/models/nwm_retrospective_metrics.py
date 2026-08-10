from django.db import models

from calibration.models.base_model import BaseModel


class NWMRetrospectiveMetrics(BaseModel):
    run_type = models.CharField(max_length=25, null=False)
    period = models.CharField(max_length=20, null=False)
    metric = models.ForeignKey('Metric', null=False, on_delete=models.RESTRICT)
    metric_value = models.FloatField(null=False)
    calibration_run = models.ForeignKey('CalibrationRun', null=False, on_delete=models.CASCADE)

    class Meta:
        db_table = 'nwm_retrospective_metrics'
        constraints = [
            models.UniqueConstraint(fields=['metric', 'period',  'run_type', 'calibration_run'], name='validation_metric__metric__period__run_type__calibration_run__unique')
        ]

    def __str__(self):
        return (
            f"NWMRetrospectiveMetrics: {self.id}, "
            f"Metric: {self.metric.name:10}, "
            f"Value: {self.metric_value}, "
            f"Calibration Job: {self.calibration_run_id}"
        )
