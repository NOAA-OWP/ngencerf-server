from django.db import models

from calibration.models.base_model import BaseModel
from calibration.models.module_group import ModuleGroup
from calibration.models.output_variable import OutputVariable


class Module(BaseModel):
    description = models.TextField(null=False)
    is_active = models.BooleanField(default=False)
    name = models.CharField(max_length=50, unique=True, null=False)
    display_name = models.CharField(max_length=50, unique=True, null=False)
    groups = models.ManyToManyField(ModuleGroup, db_table='module_group_members')
    output_variables = models.ManyToManyField(OutputVariable, db_table='module_output_variables')
    use_edfs = models.BooleanField(default=True)

    class Meta:
        db_table = 'module'

    def __str__(self):
        return (
            f"Module: {self.id}, "
            f"name: {self.name:20}"
        )
