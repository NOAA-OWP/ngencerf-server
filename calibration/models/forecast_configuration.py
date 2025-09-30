from django.db import models

from calibration.models.base_model import BaseModel


class ForecastConfiguration(BaseModel):
    is_active = models.BooleanField(null=False, default=True)
    name = models.CharField(max_length=50, unique=True, null=False)
    internal_name = models.CharField(max_length=50, unique=False, null=False)
    data_sources = models.CharField(max_length=100, null=False)
    time_range = models.CharField(max_length=100, null=False)
    availability_lag = models.IntegerField(null=False)
    domain = models.ForeignKey("Domain", null=False, on_delete=models.RESTRICT)
    cycle_start = models.IntegerField(null=False)
    cycle_end = models.IntegerField(null=False)
    cycle_freq = models.IntegerField(null=False)
    fcst_win = models.IntegerField(null=False)
    fcst_timestep = models.FloatField(null=False)

    class Meta:
        db_table = 'forecast_configuration'

    def __str__(self):
        return f"{self.name}"
