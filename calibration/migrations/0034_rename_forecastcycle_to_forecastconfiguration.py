from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('calibration', '0033_update_forecast_cycles'),
    ]

    operations = [
        # Rename the model class
        migrations.RenameModel(
            old_name='ForecastCycle',
            new_name='ForecastConfiguration',
        ),

        # Rename the underlying DB table
        migrations.AlterModelTable(
            name='forecastconfiguration',
            table='forecast_configuration',
        ),

        # Rename the FK field in ForecastRun
        migrations.RenameField(
            model_name='forecastrun',
            old_name='cycle',
            new_name='configuration',
        ),
    ]
