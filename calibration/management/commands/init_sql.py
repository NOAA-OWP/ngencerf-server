import logging
import sys
from typing import cast

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from calibration.enums import DataTypeEnum, DomainEnum
from calibration.enums_vanilla import JobType
from calibration.models import Domain, ObservationalSource, Optimization, Metric, OptimizationInput, PlotDefinition, \
    GeopackageSource, ForecastConfiguration, CustomUser
from calibration.models.forcing_source import ForcingSource
from calibration.models.module import Module
from calibration.models.module_group import ModuleGroup
from calibration.models.output_variable import OutputVariable
from calibration.models.rfc import Rfc
from calibration.models.status import Status

logger = logging.getLogger(__name__)

"""
STATIC DATA RULES

This command seeds static reference tables. It may be re-run safely — records are matched by `name`.

Allowed in this file:
- Adding new records
- Updating non-key fields such as description, is_active, display_name, etc.

NOT allowed directly in this file (must use a custom Django migration first):

────────────────────────────────────────────────────────────────────────────
1) RENAMING an existing record (changing the `name` natural key)

   - Changing `name` here alone will create a *new* row instead of updating the old one.
   - A custom migration must update the row in the database first.

   STEP 1: Create an empty migration
       python manage.py makemigrations calibration --empty --name rename_metric_corr

   # Example: renames the metric called 'Corr' to 'PearsonCorr'
   STEP 2: Edit the migration file — example:
       from django.db import migrations

       def rename_metric(apps, schema_editor):
           Metric = apps.get_model("calibration", "Metric")
           obj = Metric.objects.get(name="Corr")
           obj.name = "PearsonCorr"
           obj.save(update_fields=["name"])

       class Migration(migrations.Migration):
           dependencies = [...]
           operations = [migrations.RunPython(rename_metric)]

   STEP 3: THEN, also update the new name in this file so future runs of init_sql
           recognize the updated record rather than creating a duplicate.

────────────────────────────────────────────────────────────────────────────
2) DELETING an existing record

   - Deleting a row from this file does *not* remove it from the database.
   - A custom migration must delete it from the database first.

   # Example: deletes the metric called 'ObsoleteMetric'
   STEP 1: Create an empty migration
       python manage.py makemigrations calibration --empty --name delete_obsolete_metric

   STEP 2: Edit the migration file — example:
       from django.db import migrations

       def delete_metric(apps, schema_editor):
           Metric = apps.get_model("calibration", "Metric")
           Metric.objects.filter(name="ObsoleteMetric").delete()

       class Migration(migrations.Migration):
           dependencies = [...]
           operations = [migrations.RunPython(delete_metric)]

   STEP 3: THEN, also remove that record from this file, so init_sql does not
           try to update an object that no longer exists.

────────────────────────────────────────────────────────────────────────────

SUMMARY:
- init_sql is for ADDING or UPDATING non-key fields only.
- Renames and deletions must be done with a migration first, and THEN reflected here.
- If not, you will create duplicate records or cause update errors.
"""


class Command(BaseCommand):
    help = "Initializes static tables"

    # This script can be run multiple times without harm.  The name field will not be changed, but all other fields, such as
    # 'description' and 'is_active' will be.
    # Do not delete any of the data entries.  They will not be deleted.  Deleting any entries in the database cana cause problems
    # because these fields are Foreign Keys in other tables.
    # Instead, do a 'soft' delete by setting 'is_active' to false.
    # You can add new records and this script will add them.

    # Don't turn this flag on unless you know what you're doing.
    # For Development oly
    DELETE_FLAG = False

    def __init__(self):
        super().__init__()
        self.user = None  # Define the attribute here

    def handle(self, *args, **options):
        logger.info('Initializing static tables')

        try:
            # need to get a user that is guaranteed to be there, such as admin
            self.user = get_user_model().objects.get(email='admin@nextgenwaterprediction.com')
        except ObjectDoesNotExist:
            logger.error('********************************')
            logger.error('** Admin user does not exist. **')
            logger.error('********************************')
            sys.exit(1)

        logger.info(f"In init_sql: email: {cast(CustomUser, self.user).email}")

        # List of all initialization functions to run in sequence
        steps = [
            self.define_module_groups,
            self.define_output_variables,
            self.define_modules,
            self.define_domains,
            self.define_rfc,
            self.define_forcing_source,
            self.define_observational_source,
            self.define_geopackage_source,
            self.define_forecast_configuration,
            self.define_optimization,
            self.define_metric,
            self.define_status,
            self.define_plot_definitions,
        ]

        for func in steps:
            name = func.__name__
            logger.info(f"Running {name}()")
            try:
                func()
            except Exception as e:
                logger.exception(f"Error during {name}: {e}")
                # Django respects CommandError and propagates a non-zero exit status
                raise CommandError(f"init_sql failed in {name}: {e}")

        logger.info("Static table initialization completed successfully.")

    def define_module_groups(self):
        if self.DELETE_FLAG:
            ModuleGroup.objects.all().delete()

        values = [{"name": "Glacier", "order": 1},
                  {"name": "Snowmelt", "order": 2},
                  {"name": "Evapotranspiration", "order": 3},
                  {"name": "Soil Moisture", "order": 4},
                  {"name": "Rainfall Runoff", "order": 5},
                  {"name": "Routing", "order": 6}
                  ]

        for v in values:
            ModuleGroup.objects.update_or_create(name=v['name'], defaults={"order": v['order'], "is_active": v.get('is_active', True),
                                                                           "created_by": self.user})

    def define_output_variables(self):
        if self.DELETE_FLAG:
            OutputVariable.objects.all().delete()

        output_variable_names = ["sfcheadsubrt",
                                 "inflow",
                                 "outflow",
                                 "reservoir_assimilated_value",
                                 "water_sfc_elev",
                                 "nudge",
                                 "qBucket",
                                 "streamflow",
                                 "velocity",
                                 "ACSNOM",
                                 "SNOWT_AVG",
                                 "SOILICE",
                                 "SOILSAT_TOP",
                                 "QRAIN",
                                 "FSNO",
                                 "SNOWH",
                                 "SNLIQ",
                                 "SNEQV",
                                 "QSNOW",
                                 "SOIL_T",
                                 "SOIL_M",
                                 "SFCRNOFF",
                                 "TRAD",
                                 "LH",
                                 "FIRA",
                                 "HFX"
                                 ]

        values = [{"name": name, "order": order + 1} for order, name in enumerate(output_variable_names)]

        for v in values:
            OutputVariable.objects.update_or_create(name=v['name'], defaults={"order": v['order'], "created_by": self.user})

    def define_modules(self):
        if self.DELETE_FLAG:
            Module.objects.all().delete()

        values = [{"name": "Topoflow-Glacier",
                   "description": "A glacier energy balance module as part of TopoFlow, which calculates runoff based on snow/ice melt",
                   "groups": ["Glacier"],
                   "output_variables": ["ACSNOM", "SNOWH", "SNEQV", "QSNOW", "TRAD", "LH", "FIRA", "HFX"],
                   "is_active": False},
                  {"name": "Noah-OWP-Modular",
                   "description": "An extended, refactored version of the Noah-MP land surface model",
                   "groups": ["Snowmelt", "Evapotranspiration"],
                   "output_variables": ["ACSNOM", "SNOWT_AVG", "QRAIN", "FSNO", "SNOWH", "SNLIQ", "SNEQV", "QSNOW", "TRAD", "LH", "FIRA", "HFX"]},
                  {"name": "Snow-17",
                   "description": "Snow17 is a snow accumulation and melt model that has been used by the National Weather Service since the late 1970s for operational streamflow forecasting.  It is a temperature-index model",
                   "groups": ["Snowmelt"],
                   "output_variables": ["ACSNOM", "SNOWH", "SNEQV"]},
                  {"name": "UEB", "display_name": "Utah Energy Balance (UEB)",
                   "description": "description",
                   "groups": ["Snowmelt"],
                   "output_variables": ["ACSNOM", "SNOWT_AVG", "QRAIN", "SNEQV", "QSNOW", "TRAD", "LH", "FIRA", "HFX"]},
                  {"name": "CFE-S", "display_name": "CFE-S (Schaake)",
                   "description": "The Conceptual Functional Equivalent (CFE) model to the National Water Model. The X represents the Xinanjiang function (configuration: surface_partitioning_scheme= Xinanjiang)",
                   "groups": ["Rainfall Runoff"],
                   "output_variables": ["sfcheadsubrt", "qBucket", "streamflow", "QRAIN", "SFCRNOFF"]},
                  {"name": "CFE-X", "display_name": "CFE-X (Xinanjiang)",
                   "description": "The Conceptual Functional Equivalent (CFE) model to the National Water Model. The S represents the Schaake function (configuration: surface_partitioning_scheme=Schaake)",
                   "groups": ["Rainfall Runoff"],
                   "output_variables": ["sfcheadsubrt", "qBucket", "streamflow", "QRAIN", "SFCRNOFF"]},
                  {"name": "LSTM",
                   "description": "The Long Short-Term Memory (LSTM) network Module is dependent on a trained deep learning model. The forward pass of this LSTM model nextgen_cuda_lstm.py is heavily based on NeuralHydrology's CudaLSTM",
                   "groups": ["Glacier", "Snowmelt", "Evapotranspiration", "Soil Moisture", "Rainfall Runoff"]},
                  {"name": "PET",
                   "description": "PET handles potential evapotranspiration functions: Aerodynamic method, Combination method, Energy balance method, Penman Monteith method and Priestly Taylor method.",
                   "groups": ["Evapotranspiration"],
                   "is_active": False},
                  {"name": "TopModel",
                   "description": "A physically based, distributed watershed model that simulates hydrologic fluxes of water.",
                   "groups": ["Rainfall Runoff"],
                   "output_variables": ["streamflow", "QRAIN", "SFCRNOFF"]},
                  {"name": "Sac-SMA",
                   "description": "A BMI enabled version of the Sacramento Soil Moisture Accounting (Sac-SMA) model.  This version of Sac-SMA allows for multiple hydrological response units (HRUs) to be modeled at once.",
                   "groups": ["Rainfall Runoff"],
                   "output_variables": ["qBucket", "streamflow", "SFCRNOFF"]},
                  {"name": "LASAM", "display_name": "LASAM (Lumped Arid Semi-Arid Model)",
                   "description": "Lumped Arid/Semi-arid Model (LASAM) for infiltration and surface runoff.  The LASAM simulates infiltration and runoff based on Layered Green & Ampt with redistribution (LGAR) model.).",
                   "groups": ["Rainfall Runoff"],
                   "output_variables": ["qBucket", "streamflow", "SOILSAT_TOP", "QRAIN", "SOIL_M", "SFCRNOFF"]},
                  {"name": "SMP",
                   "description": "The soil moisture profiles (SMP schemes provide soil moisture distributed over a one-dimensional vertical column and depth to water table. These schemes facilitate coupling among hydrological and thermal models such as (CFE and SFT or LASAM and SFT).",
                   "groups": ["Soil Moisture"],
                   "output_variables": ["SOILSAT_TOP", "SOIL_M"]},
                  {"name": "SFT",
                   "description": "The soil freeze-thaw model simulates the transport of heat in soil using a one-dimensional vertical column. The model uses a standard diffusion equation discretized using a fully-implicit scheme at the interior and a semi-implicit scheme at the top and bottom boundaries, similar to NOAH-MP. More details are provided below.",
                   "groups": ["Soil Moisture"],
                   "output_variables": ["SOILICE", "SOIL_T"]},
                  {"name": "T-Route",
                   "description": "Tree-Based Channel Routing -  a dynamic channel routing model, offers a comprehensive solution for river network routing problems. Provides a series lateral inflows for each node in a channel network and computes the resulting streamflows.",
                   "groups": ["Routing"],
                   "output_variables": ["inflow", "outflow", "reservoir_assimilated_value", "water_sfc_elev", "nudge", "streamflow", "velocity", ""]}
                  ]

        for v in values:
            module_instance, _ = Module.objects.update_or_create(
                name=v['name'], defaults={
                    "display_name": v.get('display_name', v['name']),
                    "is_active": v.get('is_active', True),
                    "description": v['description'],
                    "created_by": self.user
                }
            )

            group_names = v['groups'] if 'groups' in v else []
            groups = ModuleGroup.objects.filter(name__in=group_names)

            output_variable_names = v['output_variables'] if 'output_variables' in v else []
            output_variables = OutputVariable.objects.filter(name__in=output_variable_names)

            # noinspection PyUnresolvedReferences
            module_instance.groups.set(groups)
            # noinspection PyUnresolvedReferences
            module_instance.output_variables.set(output_variables)
            module_instance.save()

    def define_domains(self):
        if self.DELETE_FLAG:
            Domain.objects.all().delete()

        values = [{"name": "Alaska", "description": "Alaska"},
                  {"name": "Hawaii", "description": "Hawaii"},
                  {"name": "CONUS", "description": "Continental United Status"},
                  {"name": "Puerto_Rico", "description": "Puerto Rico, including US Virgin Islands"}
                  ]

        for v in values:
            Domain.objects.update_or_create(name=v['name'], defaults={"is_active": v.get('is_active', True),
                                                                      "description": v['description'],
                                                                      "created_by": self.user})

    def define_rfc(self):
        if self.DELETE_FLAG:
            Rfc.objects.all().delete()

        values = [{"name": "NWRFC", "description": "Northwest River Forecast Center"},
                  {"name": "CNRFC", "description": "California/Nevada River Forecast Center"},
                  {"name": "CBRFC", "description": "Colorado Basin River Forecast Center"},
                  {"name": "MBRFC", "description": "Missouri Basin River Forecast Center"},
                  {"name": "ABRFC", "description": "Arkansas Red-Basin River Forecast Center"},
                  {"name": "WGRFC", "description": "West Gulf River Forecast Center"},
                  {"name": "NCRFC", "description": "North Central River Forecast Center"},
                  {"name": "LMRFC", "description": "Lower Mississippi River Forecast Center"},
                  {"name": "OHRFC", "description": "Ohio River Forecast Center"},
                  {"name": "SERFC", "description": "Southeast River Forecast Center"},
                  {"name": "MARFC", "description": "Mid-Atlantic River Forecast Center"},
                  {"name": "NERFC", "description": "Northeast River Forecast Center"},
                  {"name": "ARFC", "description": "Alaska River Forecast Center"},
                  {"name": "APRFC", "description": "Alaska Pacific River Forecast Center"},
                  {"name": "Canada", "description": "Canada River Forecast Center"}
                  ]

        for v in values:
            Rfc.objects.update_or_create(name=v['name'], defaults={"is_active": v.get('is_active', True),
                                                                   "description": v['description'],
                                                                   "created_by": self.user})

    def define_forcing_source(self):
        if self.DELETE_FLAG:
            ForcingSource.objects.all().delete()

        values = [{"name": "AORC", "description": "Analysis of Record For Calibration", "is_active": True},
                  {"name": "NWM Retrospective", "description": "NWM Retrospective", "is_active": True},
                  {"name": "User Upload", "description": "Uploaded by the user from a local file"},
                  ]

        for v in values:
            ForcingSource.objects.update_or_create(name=v['name'], defaults={"is_active": v.get('is_active', True),
                                                                             "description": v['description'],
                                                                             "created_by": self.user})

    def define_observational_source(self):
        if self.DELETE_FLAG:
            ObservationalSource.objects.all().delete()

        values = [{"name": "USGS", "description": "US Geological Society", "is_active": False},
                  {"name": "USACE", "description": "US Army Corp of Engineers", "is_active": False},
                  {"name": "BOR", "description": "Bureau of Reclamation", "is_active": False},
                  {"name": "ENV", "description": "Environmental Canada", "is_active": False},
                  {"name": "CA DWR", "description": "California Department of Water Resources", "is_active": False},
                  {"name": "TX DoT", "description": "Texas Department of Transportation", "is_active": False},
                  {"name": "RFC", "description": "River Forecast Center", "is_active": False},
                  {"name": "SNOTEL", "description": "Snow Telemetry", "is_active": False},
                  {"name": "Historical", "description": "NGWPC Enterprise Data Services", "is_active": True},
                  {"name": "User Upload", "description": "Upload by the user from a local file", "is_active": True},
                  ]

        for v in values:
            ObservationalSource.objects.update_or_create(name=v['name'],
                                                         defaults={"is_active": v.get('is_active', True),
                                                                   "description": v['description'],
                                                                   "created_by": self.user})

    def define_geopackage_source(self):
        if self.DELETE_FLAG:
            GeopackageSource.objects.all().delete()

        values = [{"name": "Hydrofabric", "description": "NGWPC Enterprise Data Services", "is_active": True},
                  {"name": "User Upload", "description": "Upload by the user from a local file", "is_active": True},
                  ]

        for v in values:
            GeopackageSource.objects.update_or_create(name=v['name'],
                                                      defaults={"is_active": v.get('is_active', True),
                                                                "description": v['description'],
                                                                "created_by": self.user})

    def define_forecast_configuration(self):
        if self.DELETE_FLAG:
            ForecastConfiguration.objects.all().delete()

        alaska_domain = DomainEnum.get_instance('Alaska')
        hawaii_domain = DomainEnum.get_instance('Hawaii')
        puerto_rico_domain = DomainEnum.get_instance('Puerto_Rico')
        conus_domain = DomainEnum.get_instance('CONUS')

        values = [
            {
                "name": "Analysis and Assimilation (AnA)", "internal_name": "standard_ana", "data_sources": "HRRR, RAP, MRMS-MS, MRMS-RO, USGS gages",
                "time_range": "3 hr",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 48, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Short Range Forecast", "internal_name": "short_range", "data_sources": "HRRR, RAP",
                "time_range": "Latest forecast cycle - 18 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 18, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Short Range Alaska", "internal_name": "short_range_alaska", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 15 hours (for even-numbered cycles)",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 15, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Short Range Extended Alaska", "internal_name": "short_range_extended_alaska", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 45 hours (for odd-numbered cycles)",
                "domain": alaska_domain,
                "cycle_start": 3, "cycle_end": 21, "cycle_freq": 6, "fcst_win": 45, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Short Range Hawaii", "internal_name": "short_range_hawaii", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 48 hours",
                "domain": hawaii_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 48, "fcst_timestep": 0.25,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Short Range Puerto Rico", "internal_name": "short_range_puertorico", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 48 hours",
                "domain": puerto_rico_domain,
                "cycle_start": 6, "cycle_end": 18, "cycle_freq": 12, "fcst_win": 48, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Extended AnA", "internal_name": "extended_ana", "data_sources": "RAP, HRRR, Stage IV",
                "time_range": "tbd",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 48, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Blend", "internal_name": "medium_range_blend", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Medium Range MEM1", "internal_name": "medium_range_mem1", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range MEM2", "internal_name": "medium_range_mem2", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range MEM3", "internal_name": "medium_range_mem3", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range MEM4", "internal_name": "medium_range_mem4", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range MEM5", "internal_name": "medium_range_mem5", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range MEM6", "internal_name": "medium_range_mem6", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Blend Alaska", "internal_name": "medium_range_blend_alaska", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": True
            },
            {
                "name": "Medium Range Alaska MEM1", "internal_name": "medium_range_alaska_mem1", "data_sources": "tbd",
                "time_range": "Latest forecast cycle -240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Alaska MEM2", "internal_name": "medium_range_alaska_mem2", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Alaska MEM3", "internal_name": "medium_range_alaska_mem3", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Alaska MEM4", "internal_name": "medium_range_alaska_mem4", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Alaska MEM5", "internal_name": "medium_range_alaska_mem5", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Medium Range Alaska MEM6", "internal_name": "medium_range_alaska_mem6", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 240 hours",
                "domain": alaska_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 240, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Long Range AnA", "internal_name": "long_range_ana", "data_sources": "HRRR, RAP, MRMS-MS, MRMS-RO, USGS gages",
                "time_range": "tbd",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 48, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Long Range Forecast", "internal_name": "long_range", "data_sources": "long_range_forecast",
                "time_range": "tbd",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 23, "cycle_freq": 1, "fcst_win": 48, "fcst_timestep": 1,
                "availability_lag": 6,
                "is_active": False
            },
            {
                "name": "Long Range MEM1", "internal_name": "long_range_mem1", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 720 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 720, "fcst_timestep": 6,
                "availability_lag": 12,
                "is_active": True
            },
            {
                "name": "Long Range MEM2", "internal_name": "long_range_mem2", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 720 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 720, "fcst_timestep": 6,
                "availability_lag": 12,
                "is_active": True
            },
            {
                "name": "Long Range MEM3", "internal_name": "long_range_mem3", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 720 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 720, "fcst_timestep": 6,
                "availability_lag": 12,
                "is_active": True
            },
            {
                "name": "Long Range MEM4", "internal_name": "long_range_mem4", "data_sources": "tbd",
                "time_range": "Latest forecast cycle - 720 hours",
                "domain": conus_domain,
                "cycle_start": 0, "cycle_end": 18, "cycle_freq": 6, "fcst_win": 720, "fcst_timestep": 6,
                "availability_lag": 12,
                "is_active": True
            },
        ]

        for v in values:
            ForecastConfiguration.objects.update_or_create(name=v['name'],
                                                           defaults={"is_active": v.get('is_active', True),
                                                                     "internal_name": v['internal_name'],
                                                                     "data_sources": v['data_sources'],
                                                                     "time_range": v['time_range'],
                                                                     "domain": v['domain'],
                                                                     "availability_lag": v['availability_lag'],
                                                                     "cycle_start": v['cycle_start'],
                                                                     "cycle_end": v['cycle_end'],
                                                                     "cycle_freq": v['cycle_freq'],
                                                                     "fcst_win": v['fcst_win'],
                                                                     "fcst_timestep": v['fcst_timestep'],
                                                                     "created_by": self.user})

    def define_optimization(self):
        if self.DELETE_FLAG:
            Optimization.objects.all().delete()
            OptimizationInput.objects.all().delete()

        values = [{"name": "DDS", "description": "Dynamically Dimensioned Search",
                   "inputs": [{"name": "r", "description": "Sample region size", "data_type": DataTypeEnum.DOUBLE, "default_value": 0.2, "min": 0.2,
                               "max": 0.2}]},
                  {"name": "PSO", "description": "Particle Swarm Optimization",
                   "inputs": [{"name": "swarm_size", "description": "Swarm size", "data_type": DataTypeEnum.INTEGER, "default_value": 2, "min": 2},
                              {"name": "c1", "description": "Acceleration coefficient c1", "data_type": DataTypeEnum.DOUBLE, "default_value": 2.0,
                               "min": 1.0, "max": 3.0},
                              {"name": "c2", "description": "Acceleration coefficient c2 ", "data_type": DataTypeEnum.DOUBLE, "default_value": 2.0,
                               "min": 1.0, "max": 3.0},
                              {"name": "w", "description": "Inertia weight", "data_type": DataTypeEnum.DOUBLE, "default_value": 0.7, "min": 0.0,
                               "max": 1.0}]},
                  {"name": "GWO", "description": "Grey Wolf Optimization",
                   "inputs": [{"name": "swarm_size", "description": "Swarm size", "data_type": DataTypeEnum.INTEGER, "default_value": 4, "min": 4}]},
                  ]

        # stop_criteria_name and stop_criteria_data_type are not used at this time.  Setting to these values for now, but we never look at it
        for v in values:
            optimization, created = Optimization.objects.update_or_create(name=v['name'],
                                                                          defaults={"is_active": v.get('is_active', True),
                                                                                    "description": v['description'],
                                                                                    "stop_criteria_name": "iterations",
                                                                                    "stop_criteria_data_type": DataTypeEnum.INTEGER.value,
                                                                                    "created_by": self.user})

            for i in v['inputs']:
                OptimizationInput.objects.update_or_create(name=i['name'], optimization=optimization,
                                                           defaults={"is_active": i.get('is_active', True),
                                                                     "description": i['description'],
                                                                     "data_type": i['data_type'].value,
                                                                     "default_value": i['default_value'],
                                                                     "min": i.get('min', None),
                                                                     "max": i.get('max', None),
                                                                     "created_by": self.user})

    def define_metric(self):
        if self.DELETE_FLAG:
            Metric.objects.all().delete()

        values = [{"name": "Corr",
                   "display_name": "Pearson Correlation (Cor)"},
                  {"name": "MAE",
                   "display_name": "Mean Absolute Error (MAE)"},
                  {"name": "RMSE",
                   "display_name": "Root Mean Square Error (RMSE)"},
                  {"name": "RSR",
                   "display_name": "Ratio of RMSE to standard deviation of observation (RSR)"},
                  {"name": "PBIAS",
                   "display_name": "Percent Bias (PBIAS)"},
                  {"name": "KGE",
                   "display_name": "Kling-Gupta Efficiency (KGE)"},
                  {"name": "NSE",
                   "display_name": "Nash-Sutcliffe-Efficiency (NSE)"},
                  {"name": "NSELog",
                   "display_name": "NSE of Logarithmic values (LogNSE)"},
                  {"name": "NNSE",
                   "display_name": "Normalized NSE (NNSE)"},
                  {"name": "POD",
                   "display_name": "Probability of Detection (POD)",
                   "categorical": True},
                  {"name": "CSI",
                   "display_name": "Critical Success Index (CSI)",
                   "categorical": True},
                  {"name": "FAR",
                   "display_name": "False Alarm Ratio (FAR)",
                   "categorical": True},
                  {"name": "HSEG_FDC",
                   "display_name": "Percent bias of high flow segment of flow duration curve (HFDC)"},
                  {"name": "LSEG_FDC",
                   "display_name": "Percent bias of low flow segment of flow duration curve (LFDC)"},
                  {"name": "PKBIAS",
                   "display_name": "Absolute Peak Flow Bias (PKBIAS)",
                   "event_based": True},
                  {"name": "PKTE",
                   "display_name": "Peak Flow Timing Error (PKTE)",
                   "event_based": True},
                  {"name": "EVBIAS",
                   "display_name": "Event Volume Bias (EVBIAS)",
                   "event_based": True},
                  {"name": "FBIAS",
                   "display_name": "Frequency Bias (FBIAS)",
                   "categorical": True, "objective_function": False},
                  {"name": "MSEG_FDC",
                   "display_name": "Percent bias of middle flow segment of flow duration curve (MFDC)",
                   "objective_function": False},
                  {"name": "NSEWt",
                   "display_name": "Weighted NSE and NSELog (NSEWt)",
                   "objective_function": False},
                  ]

        for v in values:
            Metric.objects.update_or_create(name=v['name'], defaults={"is_active": v.get('is_active', True),
                                                                      "display_name": v['display_name'],
                                                                      "categorical": v.get('categorical', False),
                                                                      "event_based": v.get('event_based', False),
                                                                      "objective_function": v.get('objective_function', True),
                                                                      "created_by": self.user})

    def define_status(self):
        if self.DELETE_FLAG:
            Status.objects.all().delete()

        values = [{"name": "Saved"},
                  {"name": "Ready"},
                  {"name": "Submitted"},
                  {"name": "Running"},
                  {"name": "Done"},
                  {"name": "Cancelled"},
                  {"name": "Failed"},
                  {"name": "Resumed"},
                  {"name": "Server error"}
                  ]

        for v in values:
            Status.objects.update_or_create(name=v['name'], defaults={"created_by": self.user})

    def define_plot_definitions(self):

        # Since this table is not used as a foreign key, it's easy to just delete and re-create
        PlotDefinition.objects.all().delete()

        # Temporarily delete them, although this doesn't hurt, since this table is not a FK in any other table
        PlotDefinition.objects.all().delete()

        values = [
            {
                "name": "Hydrograph evolution",
                "display_name": "Hydrograph evolution",
                "description": "Time series plot comparing streamflow simulations from the control, the best iteration and the last iteration with the observed streamflow",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_hydrograph_iteration.png",
                "timeseries_available": True,
                "lstm_flag": True
            },
            {
                "name": "Objective Function evolution",
                "display_name": "Objective Function evolution",
                "description": "The evolution of objective function during all iterations with the best iteration highlighted in red",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_objfun_iteration.png"
            },
            {
                "name": "Metric evolution",
                "display_name": "Metric evolution",
                "description": "The evolution of objective function and all other metrics during all iterations with the best iteration highlighted in red",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_metric_iteration.png",
                "lstm_flag": True
            },
            {
                "name": "Parameter evolution",
                "display_name": "Parameter evolution",
                "description": "The evolution of each calibration parameter during all iterations with the best iteration highlighted in red",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_param_iteration.png"
            },
            {
                "name": "Scatterplot streamflow",
                "display_name": "Scatterplot streamflow",
                "description": "Scatter plot of streamflow simulations from the control, the best iteration and the last iteration vs the observed streamflow",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_scatterplot_streamflow_iteration.png",
                "lstm_flag": True
            },
            {
                "name": "Metrics vs Objective Function",
                "display_name": "Metrics vs Objective Function",
                "description": "Scatter plot of objective function vs each of the other evaluation metrics from all iterations (to examine tradeoffs between the objective function and other metrics)",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_metric_objfun.png"
            },
            {
                "name": "Stream Flow Precipitation",
                "display_name": "Stream Flow Precipitation",
                "description": "Same as Hydrograph Evolution but with the precipitation time series added at the top using an inverted y-axis",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_streamflow_precip_iteration.png",
                "lstm_flag": True
            },
            {
                "name": "Flow Duration Curves",
                "display_name": "Flow Duration Curves",
                "description": "Comparison of the flow duration curves for the streamflow simulations from the control, the best iteration, the last iteration and the observed streamflow",
                "location": "plot_iteration",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_fdc_iteration.png",
                "lstm_flag": True
            },
            {
                "name": "Cost History",
                "display_name": "Cost History",
                "description": "Comparison of the best global, local and best cost values at each iteration",
                "location": "output_calibration",
                "valid_optimizations": "[\"GWO\", \"PSO\"]",
                "job_type": JobType.CALIBRATION.value,
                "filename_mask": "{gage_id}_cost_hist.png",
            },
            {
                "name": "Bar Chart Metrics",
                "display_name": "Bar Chart Metrics",
                "description": "Bar chart comparing metrics from best and control validation runs for each evaluation period of the best global, local and best cost values at each iteration",
                "location": "plot_valid",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.VALIDATION.value,
                "filename_mask": "{gage_id}_barplot_metrics_valid_run.png",
                "lstm_flag": True
            },
            {
                "name": "Flow Duration Curves Validation",
                "display_name": "Flow Duration Curves Validation",
                "description": "Plot of flow duration curve comparing best and control validation runs with observation for each evaluation period",
                "location": "plot_valid",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.VALIDATION.value,
                "filename_mask": "{gage_id}_fdc_valid_run.png",
                "lstm_flag": True
            },
            {
                "name": "Hydrograph Validation",
                "display_name": "Hydrograph Validation",
                "description": "Plot comparing streamflow times series from best and control validation runs with observed streamflow",
                "location": "plot_valid",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.VALIDATION.value,
                "filename_mask": "{gage_id}_hydrograph_valid_run.png",
                "timeseries_available": True,
                "lstm_flag": True
            },
            {
                "name": "Streamflow Validation Precipitation",
                "display_name": "Streamflow Validation Precipitation",
                "description": "Same as Hydrograph Validation but with the precipitation time series added at the top using an inverted y-axis",
                "location": "plot_valid",
                "valid_optimizations": "[\"GWO\", \"PSO\", \"DDS\"]",
                "job_type": JobType.VALIDATION.value,
                "filename_mask": "{gage_id}_streamflow_precip_valid_run.png",
                "lstm_flag": True
            },
            {
                "name": "Forecast Hydrograph",
                "display_name": "Forecast Hydrograph",
                "description": "Time series of streamflow forecasts based on the calibrated formulation and parameters",
                "location": "forecast_output",
                "job_type": JobType.FORECAST.value,
                "filename_mask": "{gage_id}_hydrograph.png"
            },
            {
                "name": "Calibration Metrics",
                "display_name": "Calibration Metrics",
                "description": "Comparison of metrics from best validation runs for multiple calibration runs",
                "location": "",
                "job_type": JobType.COMPARISON.value,
                "filename_mask": "",
                "timeseries_available": False
            }
        ]

        for v in values:
            PlotDefinition.objects.update_or_create(name=v['name'], defaults={"display_name": v['display_name'],
                                                                              "is_active": v.get('is_active', True),
                                                                              "description": v['description'],
                                                                              "location": v['location'],
                                                                              "valid_optimizations": v.get('valid_optimizations'),
                                                                              "job_type": v['job_type'],
                                                                              "filename_mask": v['filename_mask'],
                                                                              "timeseries_available": v.get('timeseries_available', False),
                                                                              "lstm_flag": v.get('lstm_flag', False),
                                                                              "created_by": self.user})
