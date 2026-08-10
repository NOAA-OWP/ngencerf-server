from django.db import models

from calibration.models.base_model import BaseModel


class ModuleProperty(BaseModel):
    module = models.ForeignKey("Module", on_delete=models.RESTRICT, related_name="properties")
    description = models.TextField(null=False)
    name = models.CharField(max_length=50, null=False)
    display_name = models.CharField(max_length=50, null=False)
    data_type = models.CharField(max_length=50, null=False)
    default_value = models.CharField(max_length=200, null=False)

    class Meta:
        db_table = "module_property"

        constraints = [
            models.UniqueConstraint(fields=["module", "name"], name="uq_module_property__module_name"),
        ]

    def __str__(self):
        return (
            f"ModuleProperty: {self.id}, "
            f"module_id={self.module_id}, "
            f"name={self.name}, "
            f"display_name={self.display_name}"
        )
