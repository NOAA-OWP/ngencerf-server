from django.db import models

from calibration.models.base_model import BaseModel


class ModulePropertyChoice(BaseModel):
    module_property = models.ForeignKey("ModuleProperty", on_delete=models.CASCADE, related_name="choices")
    label = models.CharField(max_length=120)
    value_int = models.IntegerField(null=True)
    value_str = models.CharField(max_length=100, null=True)
    description = models.TextField(null=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "module_property_choice"
        indexes = [
            models.Index(fields=["module_property", "sort_order"]),
        ]
        constraints = [
            # enforce uniqueness per property by whichever value type is used
            models.UniqueConstraint(
                fields=["module_property", "value_int"],
                condition=models.Q(value_int__isnull=False),
                name="uq_property_choice__prop_value_int",
            ),
            models.UniqueConstraint(
                fields=["module_property", "value_str"],
                condition=models.Q(value_str__isnull=False),
                name="uq_property_choice__prop_value_str",
            ),
            models.CheckConstraint(
                check=(
                    # exactly one is set.  The other is null
                        (models.Q(value_int__isnull=False) & models.Q(value_str__isnull=True)) |
                        (models.Q(value_int__isnull=True) & models.Q(value_str__isnull=False))
                ),
                name="ck_choice_exactly_one_value",
            ),
        ]

    def __str__(self):
        return (
            f"ModulePropertyChoice: {self.id}, "
            f"prop_id={self.module_property_id}, "
            f"label={self.label}, sort={self.sort_order}"
        )
