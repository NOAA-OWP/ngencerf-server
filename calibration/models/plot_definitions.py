from django.db import models

from calibration.models.base_model import BaseModel


class PlotDefinition(BaseModel):
    description = models.TextField(null=False)
    is_active = models.BooleanField(null=False, default=True)
    name = models.CharField(max_length=50, unique=True, null=False)
    display_name = models.CharField(max_length=50, unique=True, null=False)
    location = models.CharField(max_length=50, null=False)
    valid_optimizations = models.TextField(null=True)
    lstm_flag = models.BooleanField(default=False)
    job_type = models.CharField(max_length=20, null=False)
    filename_mask = models.TextField(null=False)
    timeseries_available = models.BooleanField(default=False)

    class Meta:
        db_table = 'plot_definitions'

    def __str__(self):
        return (
            f"PlotDefinition: {self.id}, "
            f"name: {self.name:20}, "
            f"display_name: {self.display_name:20}, "
            f"description: {self.description}, "
            f"filename_mask: {self.filename_mask}"
        )
