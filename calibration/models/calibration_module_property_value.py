from django.db import models

from calibration.models.base_model import BaseModel


class CalibrationModulePropertyValue(BaseModel):
    calibration_formulation = models.ForeignKey("CalibrationFormulation", null=False, on_delete=models.CASCADE, related_name="module_property_values")
    module_property = models.ForeignKey("ModuleProperty", null=False, on_delete=models.RESTRICT, related_name="+")

    # exactly one of these should be set, based on module_property.data_type
    value_bool = models.BooleanField(null=True)
    value_int = models.IntegerField(null=True)
    value_double = models.FloatField(null=True)
    value_str = models.CharField(max_length=500, null=True)

    class Meta:
        db_table = "calibration_module_property_value"

        constraints = [
            models.UniqueConstraint(
                fields=["calibration_formulation", "module_property"],
                name="uq_calibration_module_property_value__formulation_property",
            ),
            # exactly one value column is non-null
            models.CheckConstraint(
                check=(
                        (models.Q(value_bool__isnull=False) & models.Q(value_int__isnull=True) & models.Q(value_double__isnull=True) & models.Q(
                            value_str__isnull=True)) |
                        (models.Q(value_bool__isnull=True) & models.Q(value_int__isnull=False) & models.Q(value_double__isnull=True) & models.Q(
                            value_str__isnull=True)) |
                        (models.Q(value_bool__isnull=True) & models.Q(value_int__isnull=True) & models.Q(value_double__isnull=False) & models.Q(
                            value_str__isnull=True)) |
                        (models.Q(value_bool__isnull=True) & models.Q(value_int__isnull=True) & models.Q(value_double__isnull=True) & models.Q(
                            value_str__isnull=False))
                ),
                name="ck_calibration_module_property_value_exactly_one",
            ),
        ]

        indexes = [
            models.Index(fields=["calibration_formulation"], name="idx_cal_mod_prop_val_form"),
            models.Index(fields=["module_property"], name="idx_cal_mod_prop_val_property"),
        ]

    def _get_value(self):
        if self.value_bool is not None:
            return self.value_bool
        if self.value_int is not None:
            return self.value_int
        if self.value_double is not None:
            return self.value_double
        if self.value_str is not None:
            return self.value_str
        return None  # should never happen if constraint is enforced

    def __str__(self):
        module_name = self.calibration_formulation.module.name
        property_name = self.module_property.name
        value = self._get_value()

        return (
            f"CalibrationModulePropertyValue: {self.id}, "
            f"run_id={self.calibration_formulation.calibration_run_id}, "
            f"module={module_name}, "
            f"property={property_name}, "
            f"value={value}"
        )
