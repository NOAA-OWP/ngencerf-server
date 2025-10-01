import copy
import logging

from calibration.enums import StatusEnum
from calibration.models import ForecastRun, ColdStartRun
from calibration.util.ngen_locations import get_forecast_dir, FORECAST_FORCING_TEMPLATES, get_cold_start_dir
from calibration.views.called_from import called_from
from calibration.views.common import format_datetime, join_with_or, ErrorReport, readonly_transaction
from calibration.views.ngen_cal_input import build_config

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {

    "Forcing": {
        "forcing_provider": "bmi",
        "forecast_configuration": "",
        "cycle_datetime": None,
        "forcing_template_dir": FORECAST_FORCING_TEMPLATES,
        "use_cold_start": False,
        "cold_start_datetime": None
    }

}


def create_forecast_input(run: ForecastRun | ColdStartRun) -> tuple[ErrorReport | None, str | None]:
    """


    :param run: The ForecatRun or ColdStartRun instance to validate and prepare.
    :return: A tuple (ErrorReport, config_file_path):
             - error_object: ErrorReport object with errors and warnings.
             - config_file_path: Path to the generated config file if build is successful, else None.
    """
    logger.info(called_from())

    error_object = ErrorReport()
    config_file: str | None = None

    # -----------------------------
    # READ-ONLY PHASE
    # -----------------------------
    with readonly_transaction():
        allowed_status_names = [StatusEnum.SUBMITTED.value]
        if run.status.name not in allowed_status_names:
            job_name = 'Forecast' if isinstance(run, ForecastRun) else 'Cold Start'
            error_object.add_warning(
                f'{job_name} Job {run.id} is not in an allowed status: '
                f'{join_with_or(allowed_status_names)}. '
                f'Current status: {run.status.name}'
            )
            return error_object, None

    # Deepcopy config template
    config: dict[str, dict[str, str | int | float | bool]] = copy.deepcopy(CONFIG_TEMPLATE)

    forcing = config['Forcing']

    forcing['forecast_configuration'] = run.configuration.internal_name
    if isinstance(run, ForecastRun):
        forcing['cycle_datetime'] = format_datetime(run.cycle_date)
        # forcing.pop('cold_start_datetime')
    else:
        forcing['cold_start_datetime'] = format_datetime(run.cold_start_date)
        # TODO Setting this temporarily to avoid parsing error
        forcing['cycle_datetime'] = format_datetime(run.cold_start_date)
        # forcing.pop('cycle_datetime')

    forcing['use_cold_start'] = isinstance(run, ColdStartRun)

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    config_location = get_forecast_dir(run) if isinstance(run, ForecastRun) else get_cold_start_dir(run)
    if not error_object.has_errors() and not error_object.has_warnings():
        config_file = build_config(config, config_location, 'forecast-input.config')

    return error_object, config_file
