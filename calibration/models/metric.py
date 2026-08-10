from django.db import models

from calibration.models.base_model import BaseModel


class Metric(BaseModel):
    is_active = models.BooleanField(null=False, default=True)
    name = models.CharField(max_length=50, unique=True, null=False)
    display_name = models.CharField(max_length=100, null=False)
    categorical = models.BooleanField(null=False, default=False)
    event_based = models.BooleanField(null=False, default=False)
    objective_function = models.BooleanField(null=False, default=False)

    class Meta:
        db_table = 'metric'

    def __str__(self):
        return (
            f"Metric: {self.id}, "
            f"name: {self.name:10}"
        )
