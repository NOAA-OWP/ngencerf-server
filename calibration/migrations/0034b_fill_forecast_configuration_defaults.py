# calibration/migrations/0034b_fill_forecast_configuration_defaults.py
from django.db import migrations


def fill_forecast_configuration_defaults(apps, schema_editor):
    """
    Fills NULL values in forecast_configuration columns that are about
    to become non-nullable in migration 0035_adjust_forecast_configuration_fields.
    Ensures 0035 will apply cleanly without IntegrityError.
    """
    ForecastConfiguration = apps.get_model('calibration', 'ForecastConfiguration')
    Domain = apps.get_model('calibration', 'Domain')

    # 1. Fill numeric fields with safe defaults
    ForecastConfiguration.objects.filter(availability_lag__isnull=True).update(availability_lag=0)
    ForecastConfiguration.objects.filter(cycle_start__isnull=True).update(cycle_start=0)
    ForecastConfiguration.objects.filter(cycle_end__isnull=True).update(cycle_end=0)
    ForecastConfiguration.objects.filter(cycle_freq__isnull=True).update(cycle_freq=0)
    ForecastConfiguration.objects.filter(fcst_win__isnull=True).update(fcst_win=0)
    ForecastConfiguration.objects.filter(fcst_timestep__isnull=True).update(fcst_timestep=1.0)

    # 2. Fill domain if any records have NULL
    null_domains = ForecastConfiguration.objects.filter(domain__isnull=True)
    if null_domains.exists():
        first_domain = Domain.objects.first()
        if not first_domain:
            # If there are no domains at all, create a dummy one so the constraint passes
            first_domain = Domain.objects.create(name="Default Domain", internal_name="default_domain")
        null_domains.update(domain=first_domain)

class Migration(migrations.Migration):

    dependencies = [
        ('calibration', '0034_rename_forecastcycle_to_forecastconfiguration'),
    ]

    operations = [
        migrations.RunPython(
            fill_forecast_configuration_defaults,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

