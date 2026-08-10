import copy
import logging
from typing import Any

import yaml

from calibration.enums import StatusEnum
from calibration.models import VerificationRun
from calibration.util.caching import generate_forecast_config_yaml
from calibration.util.ngen_locations import get_forecast_output_file, get_verification_run_dir, \
    get_verification_yaml_config_file, VERF_CROSSWALK_NGEN_FILE, VERF_GAGE_HYDROFABRIC_FILE
from calibration.views.called_from import called_from
from calibration.views.common import join_with_or, ErrorReport, readonly_transaction

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


def create_verification_input(run: VerificationRun) -> ErrorReport | None:
    """
    :param run: The VerificationRun instance to validate and prepare.
    :return: A tuple (ErrorReport, config_file_path):
             - error_object: ErrorReport object with errors and warnings.
             - config_file_path: Path to the generated config file if build is successful, else None.
    """
    logger.info(called_from())

    error_object = ErrorReport()
    config = copy.deepcopy(CONFIG_TEMPLATE)

    # -----------------------------
    # READ-ONLY PHASE
    # -----------------------------
    with readonly_transaction():
        allowed_status_names = [StatusEnum.SAVED.value, StatusEnum.READY.value]
        if run.status.name not in allowed_status_names:
            job_name = 'Verification'
            error_object.add_warning(
                f'{job_name} Job {run.id} is not in an allowed status: '
                f'{join_with_or(allowed_status_names)}. '
                f'Current status: {run.status.name}'
            )
            return error_object

    # Add hard-coded file paths to YAML
    config['file_paths'] = {
        'base_dir': get_verification_run_dir(run),
        'fcst_config_file': generate_forecast_config_yaml(),
        'gage_hydrofabric_file': VERF_GAGE_HYDROFABRIC_FILE,
        'output_dir': get_verification_run_dir(run),
    }

    general = config['general']
    file_paths: dict[str, Any] = config['file_paths']

    # Override values in YAML with info from our forecast/calibration runs
    general['location_set_name'] = 'usgs_' + run.forecast_run.calibration_run.gage.gage_id
    general['location_list'] = [run.forecast_run.calibration_run.gage.gage_id]
    general['location_type'] = 'usgs_gage'
    general['nwm_configuration'] = run.forecast_run.configuration.internal_name
    general['dataset_name'] = [run.forecast_run.calibration_run.user_formulation_name]
    general['nwm_version'] = ['ngen']
    general['forecast_start_date'] = [run.forecast_run.cycle_date.strftime("%Y-%m-%d")]
    general['forecast_end_date'] = [run.forecast_run.cycle_date.strftime("%Y-%m-%d")]
    config['nwm_forecast']['data_source'] = 'ngenCERF'
    file_paths['crosswalk_file'] = {'ngen': VERF_CROSSWALK_NGEN_FILE}
    file_paths['fcst_data_file'] = {}
    file_paths['fcst_data_file'][run.forecast_run.calibration_run.user_formulation_name] = get_forecast_output_file(run.forecast_run)
    
    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    config_location = get_verification_yaml_config_file(run)
    if not error_object.has_errors() and not error_object.has_warnings():
        with open(config_location, 'w') as config_file:
            yaml.dump(config, config_file, default_flow_style=False)
            logger.info(f"Writing new YAML file to {config_location}")

    return error_object
