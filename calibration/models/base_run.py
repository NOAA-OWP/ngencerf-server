from django.db import models

from calibration.models.base_model import BaseModel


class BaseRun(BaseModel):
    # Time that the job is actually submitted from the UI
    submit_date = models.DateTimeField(null=True)
    # Time that we send the Slurm request
    sent_date = models.DateTimeField(null=True)
    # Time that the job actually starts running
    run_start = models.DateTimeField(null=True)
    run_end = models.DateTimeField(null=True)
    slurm_job_id = models.IntegerField(null=True)
    performance_metrics = models.ForeignKey('PerformanceMetrics', null=True, on_delete=models.CASCADE)
    status = models.ForeignKey('Status', null=False, on_delete=models.RESTRICT, db_index=True)
    failure_messages = models.TextField(null=True)

    class Meta:
        abstract = True
