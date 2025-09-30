import copy
import logging

from calibration.enums import StatusEnum
from calibration.models import ForecastRun
from calibration.util.ngen_locations import get_forecast_dir, FORECAST_FORCING_TEMPLATES
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
        "cycle_datetime": "",
        "forcing_template_dir": FORECAST_FORCING_TEMPLATES,
        "use_cold_start": False,
        "cold_start_datetime": ""
    }

}


def create_forecast_input(run: ForecastRun) -> tuple[ErrorReport | None, str | None]:
    """


    :param run: The CalibrationRun instance to validate and prepare.
    :return: A tuple (ErrorReport, config_file_path):
             - error_object: ErrorReport object with errors and warnings.
             - config_file_path: Path to the generated config file if build is successful, else None.
    """
    logger.info(called_from())

    error_object = ErrorReport()
    config: dict[str, dict[str, str | int | float | bool]] = {}
    config_file: str | None = None

    # -----------------------------
    # READ-ONLY PHASE
    # -----------------------------
    with readonly_transaction():
        allowed_status_names = [StatusEnum.SUBMITTED.value]
        if run.status.name not in allowed_status_names:
            error_object.add_warning(
                f'Forecast Job {run.id} is not in an allowed status: '
                f'{join_with_or(allowed_status_names)}. '
                f'Current status: {run.status.name}'
            )
            return error_object, None

        # Deepcopy config template
        config: dict[str, dict[str, str | int | float | bool]] = copy.deepcopy(CONFIG_TEMPLATE)

        forcing = config['Forcing']

        forcing['cycle_datetime'] = format_datetime(run.cycle_date)
        # TODO
        forcing['forecast_configuration'] = run.cycle.internal_name  # Need to get the internal name
        forcing['forecast_configuration'] = 'short_range'
        forcing['use_cold_start'] = run.cold_start_date is not None
        forcing['cold_start_datetime'] = format_datetime(run.cold_start_date) if run.cold_start_date else None

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    if not error_object.has_errors() and not error_object.has_warnings():
        config_file = build_config(config, get_forecast_dir(run), 'forecast-input.config')

    return error_object, config_file
