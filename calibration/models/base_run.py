from django.db import models

from calibration.models.base_model import BaseModel


class BaseRun(BaseModel):
    submit_date = models.DateTimeField(null=True)
    run_start = models.DateTimeField(null=True)
    run_end = models.DateTimeField(null=True)
    slurm_job_id = models.IntegerField(null=True)
    performance_metrics = models.ForeignKey('PerformanceMetrics', null=True, on_delete=models.CASCADE)
    status = models.ForeignKey('Status', null=False, on_delete=models.RESTRICT, db_index=True)
    failure_messages = models.TextField(null=True)

    class Meta:
        abstract = True
