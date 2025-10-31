import copy
import logging

from calibration.enums import StatusEnum
from calibration.models import ForecastRun, ColdStartRun
from calibration.util.ngen_locations import get_forecast_dir, FORECAST_FORCING_TEMPLATES, get_cold_start_dir
from calibration.views.called_from import called_from
from calibration.views.common import format_datetime, join_with_or, ErrorReport, readonly_transaction
from calibration.views.ngen_cal_input import build_config
from cerfServer.settings import NGEN_FORECAST_WORK_DIR

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {

    "Forcing": {
        "forcing_provider": "bmi",
        "root_dir": NGEN_FORECAST_WORK_DIR,
        "forcing_configuration": "",
        "cycle_datetime": None,
        "forcing_template_dir": FORECAST_FORCING_TEMPLATES,
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

    forcing['forcing_configuration'] = run.configuration.internal_name
    forcing['cycle_datetime'] = format_datetime(run.cycle_date)

    if isinstance(run, ColdStartRun):
        forcing['cold_start_datetime'] = format_datetime(run.cold_start_date)

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    config_location = get_forecast_dir(run) if isinstance(run, ForecastRun) else get_cold_start_dir(run)
    if not error_object.has_errors() and not error_object.has_warnings():
        config_name = 'forecast-input.config' if isinstance(run, ForecastRun) else 'cold-start-input.config'
        config_file = build_config(config, config_location, config_name)

    return error_object, config_file
