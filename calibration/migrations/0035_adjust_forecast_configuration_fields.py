# calibration/migrations/0035_adjust_forecast_configuration_fields.py
from django.db import migrations, models
import django.db.models.deletion


def delete_medium_range_forecast(apps, schema_editor):
    ForecastConfiguration = apps.get_model('calibration', 'ForecastConfiguration')
    # Delete any legacy rows for "Medium Range Forecast"
    # (covers both the canonical short name and any stray variants)
    ForecastConfiguration.objects.filter(
        internal_name__in=['medium_range', 'medium_range_forecast']
    ).delete()


class Migration(migrations.Migration):
    # Run delete and alters in separate transactions to avoid:
    # "cannot ALTER TABLE ... because it has pending trigger events"
    atomic = False

    dependencies = [
        ('calibration', '0034_rename_forecastcycle_to_forecastconfiguration'),
    ]

    operations = [
        # 1) Drop the legacy row that still has NULLs
        migrations.RunPython(delete_medium_range_forecast, reverse_code=migrations.RunPython.noop),

        # 2) Tighten fields now that the bad row is gone
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='availability_lag',
            field=models.IntegerField(null=False),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='domain',
            field=models.ForeignKey(
                "calibration.Domain",
                null=False,
                on_delete=django.db.models.deletion.RESTRICT,
            ),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='cycle_start',
            field=models.IntegerField(null=False),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='cycle_end',
            field=models.IntegerField(null=False),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='cycle_freq',
            field=models.IntegerField(null=False),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='fcst_win',
            field=models.IntegerField(null=False),
        ),
        migrations.AlterField(
            model_name='forecastconfiguration',
            name='fcst_timestep',
            field=models.FloatField(null=False),
        ),
    ]
