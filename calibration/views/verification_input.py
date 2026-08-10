import copy
import logging
from datetime import timedelta
from typing import Any

import yaml

from calibration.enums import HindcastConfigEnum, ForecastConfigEnum
from calibration.models import VerificationRun
from calibration.util.caching import generate_forecast_config_yaml
from calibration.util.ngen_locations import get_verification_run_dir, VERF_CROSSWALK_NGEN_FILE, get_forecast_output_file_path, \
    get_verification_yaml_config_file, get_hindcast_output_file_name, get_hindcast_dir
from calibration.views.called_from import called_from
from calibration.views.common import format_datetime

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {
    "general": {
        "steps": {
            "fetch_fcst_data": True,
            "fetch_obs_data": True,
            "pair_data": True,
            "compute_metrics": True,
            "plot_metrics": True,
        },
        "location_set_name": "",
        "location_list": [],
        "location_type": "usgs_gage",
        "variable_name": "streamflow",
        "nwm_configuration": "",
        "dataset_name": [],
        "nwm_version": [],
        "forecast_start_date": [],
        "forecast_end_date": []
    },

    "file_paths": {},

    "nwm_forecast": {
        "data_source": ""
    },

    "flow_observation": {
        "usgs": {
            "chunk_by": "month",
            "overwrite_output": True,
            "memory_per_worker_gb": 3
        }
    },

    "pair_data": {
        "overwrite": True,
        "group_size": 200
    },

    "metrics": {
        "overwrite": True,
        "library": "nwm.eval",
        "metric_subset": "all",
        "flow_threshold_categorical": 0.9,
        "flow_threshold_event": 0.9,
        "lead_times": ['all_aggregated'],
        "file_format": "parquet"
    },

    "plots": {
        "time_series": {
            "plot": True
        },
        "metric_table": {
            "plot": True
        },
        "barchart": {
            "plot": True
        }
    }

}


def create_verification_input(run: VerificationRun) -> str:
    """
    :param run: The VerificationRun instance to validate and prepare.
    :return: Path to the generated config file.
    """
    logger.info(called_from())

    is_hindcast = run.hindcast_run_id is not None
    parent_run = run.parent_run
    calibration_run = parent_run.calibration_run
    configuration_internal_name = parent_run.configuration.internal_name

    short_range = configuration_internal_name.startswith('short_range')
    medium_range = configuration_internal_name.startswith('medium_range')
    long_range = configuration_internal_name.startswith('long_range')

    metrics_lead_time_short_range = ['all', '1-5', '6-10', '11-18', 'all_aggregated']
    metrics_lead_time_medium_range = [24, 48, 72, 96, 120, 144, 168, 192, 216, 240, '1-72', '73-144', '145-240', 'all_aggregated']
    metrics_lead_time_long_range = ['1-120', '121-240', '241-360', '361-480', '481-600', '601-720', '1-360', '361-720', 'all_aggregated']

    time_series_lead_times_short_range = [1, 6, 12, 18]
    time_series_lead_times_medium_range = [24, 48, 120, 240]
    time_series_lead_times_long_range = [120, 240, 360, 720]

    bar_chart_lead_times_short_range = [1, 5, 10, 18, '1-5', '6-10', '11-18', 'all_aggregated']
    bar_chart_lead_times_medium_range = [24, 72, 120, 168, 240, '1-72', '73-144', '145-240', 'all_aggregated']
    bar_chart_lead_times_long_range = ['1-120', '121-240', '241-360', '361-480', '481-600', '601-720', '1-360', '361-720', 'all_aggregated']

    config = copy.deepcopy(CONFIG_TEMPLATE)

    # Add hard-coded file paths to YAML
    file_paths: dict[str, Any] = config['file_paths']
    file_paths['base_dir'] = get_verification_run_dir(run)
    file_paths['crosswalk_file'] = {'ngen': VERF_CROSSWALK_NGEN_FILE}
    file_paths['fcst_config_file'] = generate_forecast_config_yaml(
        enum_class=HindcastConfigEnum if is_hindcast else ForecastConfigEnum
    )

    if is_hindcast:
        hindcast_run = run.hindcast_run
        assert hindcast_run is not None

        file_paths['fcst_data_dir'] = {
            calibration_run.job_name: get_hindcast_dir(hindcast_run)
        }
        file_paths['fcst_data_file'] = get_hindcast_output_file_name(hindcast_run)
    else:
        forecast_run = run.forecast_run
        assert forecast_run is not None

        file_paths['fcst_data_file'] = {
            calibration_run.job_name: get_forecast_output_file_path(forecast_run)
        }

    file_paths['output_dir'] = get_verification_run_dir(run)

    general: dict[str, Any] = config['general']

    # Override values in YAML with info from our parent run / calibration run
    general['location_set_name'] = 'usgs_' + calibration_run.gage.gage_id
    general['location_list'] = [calibration_run.gage.gage_id]
    general['nwm_configuration'] = configuration_internal_name
    general['dataset_name'] = [calibration_run.job_name]
    general['nwm_version'] = ['ngen']
    general['forecast_start_date'] = [format_datetime(parent_run.cycle_date)]

    # Forecast verification uses one cycle, so the end date is the same as the start date.
    #
    # Hindcast verification spans multiple cycles. For hindcast, forecast_end_date should be
    # the start time of the last hindcast cycle.
    if is_hindcast:
        hindcast_run = run.hindcast_run
        assert hindcast_run is not None

        last_cycle_date = hindcast_run.cycle_date + timedelta(
            hours=hindcast_run.interval_cycle * (hindcast_run.num_iterations - 1)
        )
        general['forecast_end_date'] = [format_datetime(last_cycle_date)]
    else:
        general['forecast_end_date'] = [format_datetime(parent_run.cycle_date)]

    nwm_forecast: dict[str, Any] = config['nwm_forecast']
    nwm_forecast['data_source'] = 'hindcast' if is_hindcast else 'ngenCERF'

    metrics: dict[str, Any] = config['metrics']
    metrics['lead_times'] = ['all', '1-5', '6-10', '11-18', 'all_aggregated'] if is_hindcast else ['all_aggregated']

    plots: dict[str, Any] = config['plots']

    if is_hindcast:
        if short_range:
            metrics_lead_times = metrics_lead_time_short_range
            time_series_lead_times = time_series_lead_times_short_range
            bar_chart_lead_times = bar_chart_lead_times_short_range
        elif medium_range:
            metrics_lead_times = metrics_lead_time_medium_range
            time_series_lead_times = time_series_lead_times_medium_range
            bar_chart_lead_times = bar_chart_lead_times_medium_range
        elif long_range:
            metrics_lead_times = metrics_lead_time_long_range
            time_series_lead_times = time_series_lead_times_long_range
            bar_chart_lead_times = bar_chart_lead_times_long_range
        else:
            raise ValueError(f"Unsupported hindcast configuration: {configuration_internal_name}")

        metrics['lead_times'] = metrics_lead_times
        plots['time_series']['lead_times'] = time_series_lead_times
        plots['metric_table']['lead_times'] = bar_chart_lead_times  # Use same as bar chart
        plots['barchart']['lead_times'] = bar_chart_lead_times
        plots['barchart']['metric_subset'] = ['KGE', 'NSE', 'CORR', 'NNSE', 'PBIAS']
    else:
        metrics['lead_times'] = ['all_aggregated']

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    config_location = get_verification_yaml_config_file(run)

    with open(config_location, 'w', encoding='utf-8') as config_file:
        yaml.safe_dump(
            config,
            config_file,
            default_flow_style=False,
            sort_keys=False
        )
        logger.info(f"Writing new YAML file to {config_location}")

    return config_location
