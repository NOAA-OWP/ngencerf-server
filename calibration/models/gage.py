from django.db import models

from calibration.models.base_model import BaseModel


class Gage(BaseModel):
    is_active = models.BooleanField(null=False, default=True)
    gage_id = models.CharField(max_length=50, null=False, db_index=True)
    nws_id = models.CharField(max_length=50, null=True)
    rfc = models.ForeignKey('Rfc', null=True, on_delete=models.SET_NULL)
    nwm_v3_calibration = models.BooleanField(null=False, default=False)
    headwater_calibration = models.BooleanField(null=False, default=False)
    agency = models.CharField(max_length=50, null=False)
    station_name = models.CharField(max_length=50, null=False)
    latitude = models.FloatField(null=True)
    longitude = models.FloatField(null=True)
    altitude = models.FloatField(null=True)
    huc = models.CharField(max_length=50, null=False)
    drainage_area = models.FloatField(null=True)
    domain = models.ForeignKey('Domain', null=False, on_delete=models.CASCADE)

    class Meta:
        db_table = 'gage'
        constraints = [
            models.UniqueConstraint(fields=['gage_id', 'agency'], name='gage__gage_id__agency__unique')
        ]

    def __str__(self):
        return f"gage: {self.gage_id} - {self.agency}, station name: {self.station_name}, domain: {self.domain})"



