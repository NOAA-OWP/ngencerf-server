from django.db import models

from calibration.models.base_model import BaseModel


class OutputVariable(BaseModel):
    is_active = models.BooleanField(default=False)
    name = models.CharField(max_length=50, unique=True, null=False)
    order = models.IntegerField(null=False)

    class Meta:
        db_table = 'output_variable'

    def __str__(self):
        return (
            f"Output Variable: {self.id}, "
            f"name: {self.name:20}"
        )
