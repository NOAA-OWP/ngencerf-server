import copy
import csv
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Any

import toml
from datetimerange import DateTimeRange
from django.db import transaction
from django.db.models import F
from toml import TomlEncoder

from calibration.enums import StatusEnum, DataTypeEnum
from calibration.enums_vanilla import NgenEnvironmentEnum
from calibration.models import CalibrationOptimizationInput, CalibrationStopCriteria, CalibrationSlothParam, \
    CalibrationParameter, CalibrationFormulation, CalibrationRun, CalibrationModulePropertyValue
from calibration.util.caching import get_cached_optimization_inputs, have_LSTM, get_cached_modules_by_id, get_cached_module_properties
from calibration.util.geopkg import get_geometry_from_gpkg
from calibration.util.ngen_locations import CFE_LIB, TOPMD_LIB, SFT_LIB, SLOTH_LIB, SMP_LIB, LASAM_LIB, NOAH_LIB, NGEN_EXE, \
    get_observational_file_for_job, PET_LIB, SNOW17_LIB, SAC_LIB, NWM_RETROSPECTIVE_DIR, UEB_LIB, NGEN_MODULE_PARAMETERS, \
    PARALLEL_NGEN_EXE, PARTITION_GENERATOR_EXE, BMI_FORCING_TEMPLATES, get_geopackage_file_path, FORCING_STATIC_DIR
from calibration.views.calibration_formulation_views import validate_formulation
from calibration.views.calibration_secondary_data_views import should_generate_swe, should_generate_soil_moisture
from calibration.views.calibration_tuning_views import get_full_evaluation_date_range, validate_time_range_against_data, \
    validate_parameter_rules
from calibration.views.called_from import called_from
from calibration.views.common import TOKEN_NGEN_SCOPE, generate_custom_token, SLOTH, format_datetime, join_with_or, ErrorReport, readonly_transaction, \
    map_path_to_container
from calibration.views.data_services import get_observational_data_from_data_services
from calibration.views.mpi_rules import get_mpi_nodes
from cerfServer.settings import NGEN_ENVIRONMENT, NGEN_BMI_FORCING_WORK_DIR

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {

    "General": {
        "basin": "",
        "domain": "",
        "models": "",
        "formulation": "",
        "run_type": "calibration",
        "main_dir": "",
        # Snow Water equivalent output - Only True for snow models
        "output_swe": False,
        # Soil Moisture output - Only True for soil moisture modules
        "output_sm": False,
        # Always true
        "output_precip": True,
    },

    "ModuleProperties": {
        # Properties such as aet_rootzone will be automatically generated
    },

    "Calibration": {
        "calibration_run_id": 0,
        "ngen_cerf": True,  # Indicate that we came from the ngenCerf server - Always true
        "auth_token": "",
        "optimization_algorithm": None,
        "swarm_size": 0,
        "c1": 0,
        "c2": 0,
        "w": 0,
        "r": 0,
        "objective_function": None,
        "start_iteration": 0,
        "number_iteration": 0,
        # Whether restart calibration from the stopped iteration
        # 0: Not
        # 1: Yes
        # It should be 0 if start_interation entry is 0.
        "restart": 0,
        "calib_start_period": "",
        "calib_end_period": "",
        "calib_eval_start_period": "",
        "calib_eval_end_period": "",
        # If we're not doing automatic validation, create_input still expects a valid date/time here
        "valid_start_period": format_datetime(datetime.now()),
        "valid_end_period": format_datetime(datetime.now()),
        "valid_eval_start_period": format_datetime(datetime.now()),
        "valid_eval_end_period": format_datetime(datetime.now()),
        "full_eval_start_period": format_datetime(datetime.now()),
        "full_eval_end_period": format_datetime(datetime.now()),
        # Save streamflow output and plot at the specified iteration
        # These entries are optional and specified with the default values.
        # 1: Filename is distinguished by the iteration number.
        # 0: Filename is same at different iteration, i.e., overwritten by file from last iteration.
        "save_output_iter": 0,
        "save_plot_iter": 0,

        # Iteration interval to save plots
        # This entry is optional and specified with the default value.
        "save_plot_iter_freq": 0,
        "streamflow_threshold": 0.0,
        "peak_flow_threshold": 0.0,
        "station_name": "",

        # Parameter file, dynamically built based on user input
        "calib_parameter_file": "",

        "user_email": "",
    },

    "Forcing": {
        "forcing_provider": "",
        "root_dir": NGEN_BMI_FORCING_WORK_DIR,
        "forcing_configuration": "",
        "forcing_dir": "",
        "forcing_static_dir": FORCING_STATIC_DIR,
        "forcing_template_dir": BMI_FORCING_TEMPLATES,

    },

    "DataFile": {
        "obs_dir": "",
        "nwmretro_file": "",
        "hydrofab_file": "",

        # Static file
        "noah_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'noah-owp-modular'),
        "ueb_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'ueb'),
        "lasam_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'lasam'),
        "lstm_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'lstm'),
        "sac_sma_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'sac-sma'),
        "snow_17_parameter_dir": os.path.join(NGEN_MODULE_PARAMETERS, 'snow-17'),

        "sloth_parameter_file": "",

        "ngen_exe_file": NGEN_EXE,
        "cfe_lib": CFE_LIB,
        "sloth_lib": SLOTH_LIB,
        "topmodel_lib": TOPMD_LIB,
        "noah_owp_modular_lib": NOAH_LIB,
        "sft_lib": SFT_LIB,
        "smp_lib": SMP_LIB,
        "lasam_lib": LASAM_LIB,
        "pet_lib": PET_LIB,
        "snow_17_lib": SNOW17_LIB,
        "sac_sma_lib": SAC_LIB,
        "ueb_lib": UEB_LIB
    }
}


def ready_to_run(run: CalibrationRun, build: bool = False) -> tuple[ErrorReport, str | None]:
    """
    Validate the given CalibrationRun and prepare it for execution.

    This function checks for missing or invalid configuration in the CalibrationRun,
    and optionally builds necessary input files and directories if `build=True`.

    Splits into two database phases:
      - A READ ONLY block for validation and config building.
      - A WRITE block for updating status, persisting fields, and saving.

    It returns an ErrorReport object (containing `errors` and `fatal`), along with
    the path to the generated configuration file if applicable.

    - `errors`: Problems the user can fix. These prevent the job from being marked as ready.
    - `fatal`: Critical issues that may require intervention beyond user correction (e.g., broken CSV format).

    The job status is updated to:
    - `READY` if no issues are found,
    - `SAVED` if there are non-fatal issues.

    :param run: The CalibrationRun instance to validate and prepare.
    :param build: If True, generate configuration files and other runtime input artifacts.
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
        allowed_status_names = [StatusEnum.SAVED.value, StatusEnum.READY.value]
        if build:
            allowed_status_names.append(StatusEnum.SUBMITTED.value)
        if run.status.name not in allowed_status_names:
            error_object.add_warning(
                f'Calibration Job {run.id} is not in an allowed status: '
                f'{join_with_or(allowed_status_names)}. '
                f'Current status: {run.status.name}'
            )
            return error_object, None

        # Deepcopy config template
        config: dict[str, dict[str, str | int | float | bool]] = copy.deepcopy(CONFIG_TEMPLATE)

        general = config['General']
        module_properties = config['ModuleProperties']
        calibration = config['Calibration']
        datafile = config['DataFile']
        forcing = config['Forcing']

        parallel = {
            "parallel_ngen_exe": PARALLEL_NGEN_EXE,
            "partition_generator_exe": PARTITION_GENERATOR_EXE,
            "nprocs": None
        }

        # Initialize general configuration settings for the run
        calibration['calibration_run_id'] = run.id
        calibration['auth_token'] = generate_custom_token(run.owner, TOKEN_NGEN_SCOPE)

        have_LSTM_flag = have_LSTM(run)

        # --- Canonical module cache (shared Django file cache + per-worker @lru_cache) ---
        modules_by_id = get_cached_modules_by_id()  # authoritative, no DB or disk after first hit
        modules_by_name = {m.name: m for m in modules_by_id.values()}  # lightweight derived view for name-based lookups

        # Validate and configure the gage ID and station name
        if not is_missing(run.gage, 'gage_id', error_object):
            general['basin'] = run.gage.gage_id
            general['domain'] = run.gage.domain.name
            calibration['station_name'] = run.gage.station_name

            if not is_missing(run.geopackage_source, 'Geopackage source', error_object):
                datafile['hydrofab_file'] = get_geopackage_file_path(run)

            # Determine the source of the forcing data
            if not is_missing(run.forcing_source, 'Forcing source', error_object):
                forcing_provider = 'bmi'
                forcing_configuration = run.forcing_source.name.lower()

                # if not use_bmi:
                #     # CSV forcing path rules apply
                #     if not is_missing(run.forcing_eds_dir_path, "Forcing directory", error_object):
                #         pass
                #
                forcing['forcing_provider'] = forcing_provider
                forcing['forcing_configuration'] = forcing_configuration.lower()

            if not is_missing(run.observational_source, 'Observational source', error_object):
                if build:
                    # Get the observational data
                    date_time_range = DateTimeRange(
                        min(run.calibration_start_period, run.validation_start_period),
                        max(run.calibration_end_period, run.validation_end_period),
                    )
                    obs_csv = get_observational_data_from_data_services(run, date_time_range)
                    obs_path = get_observational_file_for_job(run)
                    obs_dir = os.path.dirname(obs_path)
                    os.makedirs(obs_dir, exist_ok=True)

                    with open(obs_path, "w", encoding="utf-8", newline="") as f:
                        f.write(obs_csv)

                    datafile['obs_dir'] = obs_dir

            nwm_retro = os.path.join(NWM_RETROSPECTIVE_DIR, f'{run.gage.gage_id}.csv')
            if os.path.exists(nwm_retro):
                datafile['nwmretro_file'] = nwm_retro

            error_message = validate_time_range_against_data(run)
            if error_message:
                error_object.add_warning(error_message)

            general['formulation'] = run.job_name

        formulations = CalibrationFormulation.objects.filter(calibration_run=run).only("module_id")

        # Compute once and reuse everywhere below.
        module_names_for_job: set[str] = {
            modules_by_id[f.module_id].name
            for f in formulations
        }

        if not is_missing(formulations, 'Modules', error_object) and not is_missing(run.job_name, 'Formulation name', error_object):

            # Build the proper { name → Module } filter for just this job
            modules_by_name_for_job = {name: modules_by_name[name] for name in module_names_for_job}

            # Store resolved model list in 'general' for logging/display
            general['models'] = ', '.join(module_names_for_job)

            # Validate using only modules actually present in this job
            formulation_errors, _, _ = validate_formulation(module_names_for_job, get_geopackage_file_path(run))
            for f in formulation_errors:
                error_object.add_error(f)

            general['output_swe'] = should_generate_swe(modules_by_name_for_job)
            general['output_sm'] = should_generate_soil_moisture(modules_by_name_for_job)

            if run.use_sloth:
                general['models'] += f', {SLOTH}'

            # -------------------------------------------------------
            # ModuleProperties
            #   module.<module_name>.<property_name> = <typed value>
            #
            # Requirement:
            #   - Always emit ALL module properties for the job's modules.
            #   - If a value is saved in CalibrationModulePropertyValue, use it.
            #   - Otherwise, use ModuleProperty.default_value (string) converted to the typed value.
            # -------------------------------------------------------

            def _parse_default_value(raw: str, data_type: str) -> tuple[bool | int | float | str, str | None]:
                """
                Parse ModuleProperty.default_value (stored as a string) into a typed Python value.
                Returns (value, error_message_or_none).
                """
                try:
                    if data_type == DataTypeEnum.BOOLEAN.value:
                        v = (raw or "").strip().lower()
                        if v in ("true", "1", "yes", "y", "on"):
                            return True, None
                        if v in ("false", "0", "no", "n", "off"):
                            return False, None
                        return False, f"Invalid boolean default '{raw}'"

                    if data_type == DataTypeEnum.INTEGER.value:
                        return int((raw or "").strip()), None

                    if data_type == DataTypeEnum.DOUBLE.value:
                        return float((raw or "").strip()), None

                    # STRING (or unknown fallback)
                    return "" if raw is None else str(raw), None

                except (ValueError, TypeError) as e:
                    return ("" if data_type == DataTypeEnum.STRING.value else 0), f"Invalid default '{raw}' for {data_type}: {e}"

            # 1) Pull ALL ModuleProperty defs for modules in this job from cache.

            all_prop_defs = get_cached_module_properties()
            prop_defs_for_job = [p for p in all_prop_defs if p.module_id in {modules_by_name[name].id for name in module_names_for_job}]

            # 2) Pull current saved values for this run (single query) and map by (formulation_id, property_id).
            module_property_values = list(
                CalibrationModulePropertyValue.objects
                .filter(calibration_formulation__calibration_run=run)
                .select_related("calibration_formulation", "module_property")
                .only(
                    "id",
                    "calibration_formulation_id",
                    "calibration_formulation__module_id",
                    "module_property_id",
                    "module_property__name",
                    "module_property__data_type",
                    "value_bool",
                    "value_int",
                    "value_double",
                    "value_str",
                )
            )

            saved_value_by_module_id_and_prop_id: dict[tuple[int, int], CalibrationModulePropertyValue] = {}
            for mpv in module_property_values:
                saved_value_by_module_id_and_prop_id[(mpv.calibration_formulation.module_id, mpv.module_property_id)] = mpv

            # 3) Emit every property key for each job module:
            #    saved value wins; otherwise default_value.
            for prop_def in prop_defs_for_job:
                module_id = prop_def.module_id
                module_obj = modules_by_id.get(module_id)
                if not module_obj:
                    continue

                module_name = module_obj.name
                prop_name = prop_def.name
                key = f"{module_name.lower()}_{prop_name}"

                # Look up the saved value row (if any) for this module+property
                mpv = saved_value_by_module_id_and_prop_id.get((module_id, prop_def.id))

                value: str | int | float | bool

                if mpv is not None:
                    # Pick the single stored value (constraint enforces exactly one)
                    if mpv.value_bool is not None:
                        value = mpv.value_bool
                    elif mpv.value_int is not None:
                        value = mpv.value_int
                    elif mpv.value_double is not None:
                        value = mpv.value_double
                    elif mpv.value_str is not None:
                        value = mpv.value_str
                    else:
                        error_object.add_warning(
                            f"Missing saved value for module property {key} (row id={mpv.id}); using default"
                        )
                        value, err = _parse_default_value(prop_def.default_value, prop_def.data_type)
                        if err:
                            error_object.add_warning(f"{key}: {err}")
                else:
                    # No saved value -> use default_value
                    value, err = _parse_default_value(prop_def.default_value, prop_def.data_type)
                    if err:
                        error_object.add_warning(f"{key}: {err}")

                module_properties[key] = value
            # End of module properties section

        job_data_dir = run.job_data_dir
        general['main_dir'] = job_data_dir

        # Validate required calibration fields
        required_calibration_fields = {
            "calibration_start_period": run.calibration_start_period,
            "calibration_end_period": run.calibration_end_period,
            "calibration_eval_start_period": run.calibration_eval_start_period,
            "calibration_eval_end_period": run.calibration_eval_end_period,
        }
        missing_calibration_fields = [name for name, value in required_calibration_fields.items() if value is None]

        if missing_calibration_fields:
            error_object.add_warning(f"Missing required calibration fields: {', '.join(missing_calibration_fields)}")
        else:
            calibration.update({
                'calib_start_period': format_datetime(run.calibration_start_period),
                'calib_end_period': format_datetime(run.calibration_end_period),
                'calib_eval_start_period': format_datetime(run.calibration_eval_start_period),
                'calib_eval_end_period': format_datetime(run.calibration_eval_end_period),
            })

        if run.automatic_validation:
            # Validate required validation fields
            required_validation_fields = {
                "validation_start_period": run.validation_start_period,
                "validation_end_period": run.validation_end_period,
                "validation_eval_start_period": run.validation_eval_start_period,
                "validation_eval_end_period": run.validation_eval_end_period,
            }
            missing_validation_fields = [name for name, value in required_validation_fields.items() if value is None]

            if missing_validation_fields:
                error_object.add_warning(f"Missing required validation fields: {', '.join(missing_validation_fields)}")
            else:
                calibration.update({
                    'valid_start_period': format_datetime(run.validation_start_period),
                    'valid_end_period': format_datetime(run.validation_end_period),
                    'valid_eval_start_period': format_datetime(run.validation_eval_start_period),
                    'valid_eval_end_period': format_datetime(run.validation_eval_end_period),
                })

                # Set full evaluation periods if both calibration and validation evaluation periods are present
                if run.calibration_eval_start_period and run.calibration_eval_end_period:
                    full_eval_start, full_eval_end = get_full_evaluation_date_range(
                        run.calibration_eval_start_period, run.calibration_eval_end_period,
                        run.validation_eval_start_period, run.validation_eval_end_period)

                    calibration['full_eval_start_period'] = format_datetime(full_eval_start)
                    calibration['full_eval_end_period'] = format_datetime(full_eval_end)

        if not is_missing(run.objective_function, 'Objective function', error_object, have_LSTM_flag=have_LSTM_flag):
            calibration['objective_function'] = run.objective_function.name.lower()

        if not is_missing(run.optimization, 'Optimization', error_object, have_LSTM_flag=have_LSTM_flag):
            calibration['optimization_algorithm'] = run.optimization.name.lower()

            # Validate if all inputs are provided
            cached_inputs_list = get_cached_optimization_inputs(run.optimization.name)
            all_input_names = {opt['name'] for opt in cached_inputs_list}

            inputs = CalibrationOptimizationInput.objects.filter(calibration_run=run).values(
                'value', data_type=F('optimization_input__data_type'), name=F('optimization_input__name')
            )

            for opt_input in inputs:
                converted_value = (
                    int(opt_input['value']) if opt_input['data_type'] == DataTypeEnum.INTEGER.value else opt_input['value']
                )
                calibration[opt_input['name']] = converted_value
                all_input_names.discard(opt_input['name'])

            # Check if any required inputs are missing
            if all_input_names:
                error_object.add_warning(f'Missing required optimization inputs for {run.optimization.name} - {list(all_input_names)}')

        if not is_missing(run.save_plot_iteration_frequency, 'Plot iteration frequency', error_object, have_LSTM_flag=have_LSTM_flag):
            calibration['save_plot_iter_freq'] = run.save_plot_iteration_frequency

        # This field is not required from user
        calibration['save_output_iter'] = int(run.save_output_iteration or 0)

        calibration['restart'] = 0

        stop_criteria = CalibrationStopCriteria.objects.filter(calibration_run=run).first()
        if not is_missing(stop_criteria, 'Stop criteria (number of iterations)', error_object, have_LSTM_flag=have_LSTM_flag):
            # We're assuming there is only 1 stop criteria record for now
            calibration['number_iteration'] = stop_criteria.value

        if stop_criteria and run.save_plot_iteration_frequency is not None and (stop_criteria.value < run.save_plot_iteration_frequency):
            error_object.add_warning(
                f"The plot iteration frequency, {run.save_plot_iteration_frequency}, must be <= the stop criteria (number of iteration) {stop_criteria.value}")

        calibration['start_iteration'] = 0

        if run.streamflow_threshold:
            calibration['streamflow_threshold'] = run.streamflow_threshold

        if run.peak_flow_threshold:
            calibration['peak_flow_threshold'] = run.peak_flow_threshold

        if run.use_sloth:
            sloth_params = list(
                CalibrationSlothParam.objects.filter(calibration_run=run)
                .values('param_name', 'param_count', 'param_units', 'param_location',
                        'param_value', 'maps_to_variable_name', 'maps_to_module_id')
            )

            # Required fields for sloth parameters
            required_fields = [
                'param_name', 'param_count', 'param_units', 'param_location',
                'param_value', 'maps_to_module_id', 'maps_to_variable_name'
            ]

            sloth_error = False
            sloth_lines = []
            header_format = '{:30s} {:>10s} {:8s} {:8s} {:>10s} {:15s} {:30s}\n'
            line_format = '{:30s} {:10d} {:8s} {:8s} {:10.5g} {:15s} {:30s}\n'

            for s in sloth_params:
                missing_fields = [field for field in required_fields if s.get(field) is None]
                if missing_fields:
                    sloth_error = True
                    error_object.add_warning(f"Missing fields {', '.join(missing_fields)} for sloth parameter '{s['param_name']}'")
                else:
                    module_name = modules_by_id[s['maps_to_module_id']].name
                    sloth_lines.append(
                        line_format.format(
                            s['param_name'], s['param_count'], s['param_units'],
                            s['param_location'], s['param_value'],
                            module_name, s['maps_to_variable_name']
                        )
                    )

            # If no errors and build is True, write the sloth parameters to a file
            if not sloth_error and build:
                sloth_parameter_file = os.path.join(job_data_dir, 'sloth_parameters.txt')
                sloth_parameter_content = header_format.format(
                    'name', 'count', 'units', 'location', 'value ', 'maps_to_module', 'maps_to_variable_name'
                ) + '\n'.join(sloth_lines)
                with open(sloth_parameter_file, 'w') as f:
                    f.write(sloth_parameter_content)
                datafile['sloth_parameter_file'] = sloth_parameter_file

        # --- Calibration parameters (use cache) ---
        params = list(
            CalibrationParameter.objects
            .filter(calibration_formulation__calibration_run=run, user_selected_for_tuning=True)
            .values('name', 'initial_value', 'minimum', 'maximum', 'calibration_formulation__module_id')
        )

        # Pre-attach model names so the validator can reason about Topoflow vs non-Topoflow.
        for p in params:
            p["model"] = modules_by_id[p["calibration_formulation__module_id"]].name

        selected_module_names = {p["model"] for p in params}  # after you set model

        validate_parameter_rules(
            module_names_for_job=module_names_for_job,
            selected_module_names=selected_module_names,
            have_LSTM_flag=have_LSTM_flag,
            has_any_params=bool(params),
            error_object=error_object,
        )

        param_error = False
        if not have_LSTM_flag:
            # Validate parameter values and write parameter files
            for p in params:
                if not p['name'] or p['initial_value'] is None or p['minimum'] is None or p['maximum'] is None:
                    param_error = True
                    error_object.add_warning(
                        f"value ({p['initial_value']}), min ({p['minimum']}) and max ({p['maximum']}) "
                        f"must be specified for parameter '{p['name']}' (module {p['model']})"
                    )

        # Write parameter files when build is true and there were no param errors.
        # This intentionally allows writing with an empty params list (e.g., LSTM jobs),
        if not param_error and build:
            calibration['calib_parameter_file'] = os.path.join(job_data_dir, 'calib_parameter_dir')
            write_parameter_files(params, calibration['calib_parameter_file'])

        if build and NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
            if run.num_catchments is None:
                # Handle old jobs which might not have saved num_catchments
                geopackage_path = get_geopackage_file_path(run)
                catchments = list(get_geometry_from_gpkg(geopackage_path)['catchments'].keys())
                run.num_catchments = len(catchments)

            config['Parallel'] = parallel
            run.mpi_nprocs = get_mpi_nodes(run.num_catchments)
            parallel['nprocs'] = run.mpi_nprocs
            run.node_type = get_node_type(run.num_catchments)

    # -----------------------------
    # WRITE PHASE
    # -----------------------------
    with transaction.atomic():
        # If no errors, then leave the status alone, either READY or SUBMITTED

        if run.status in [StatusEnum.SAVED.db_instance, StatusEnum.READY.db_instance]:
            if run.status == StatusEnum.SAVED.db_instance and not error_object.has_errors() and not error_object.has_warnings():
                run.status = StatusEnum.READY.db_instance
            elif run.status == StatusEnum.READY.db_instance and (error_object.has_errors() or error_object.has_warnings()):
                run.status = StatusEnum.SAVED.db_instance

        run.save()

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    if build and not error_object.has_errors() and not error_object.has_warnings():
        config_file = build_config(config, run.job_data_dir, 'ngen-cal.config')

    return error_object, config_file


def write_parameter_files(params: list[dict[str, str | float]], parameter_dir: str) -> None:
    """
    Writes parameter files for each model in `params` as CSV files.

    :param params: A list of dictionaries containing information about the parameters for a specific model.
    :param parameter_dir: The directory where the parameter files should be written.
    :return: None
    """
    # Ensure the directory exists
    os.makedirs(parameter_dir, exist_ok=True)

    # Group parameters by model
    params_by_model = defaultdict(list)
    for param in params:
        params_by_model[param['model']].append(param)

    # Write a separate CSV file for each model
    for model, model_params in params_by_model.items():
        parameter_file = os.path.join(parameter_dir, f'calib_params_{model.lower()}.csv')

        # Write the CSV file
        with open(parameter_file, mode='w', newline='') as param_file:
            # noinspection PyTypeChecker
            writer = csv.DictWriter(param_file, fieldnames=['param', 'min', 'max', 'init'])
            writer.writeheader()
            for p in model_params:
                writer.writerow({
                    'param': p['name'],
                    'min': p['minimum'],
                    'max': p['maximum'],
                    'init': p['initial_value']
                })

        logger.info(f'CSV parameter file for model {model} saved to {parameter_file}')


class CustomTomlEncoder(TomlEncoder):
    def __init__(self):
        super().__init__()
        self._dict = dict  # Ensure TOML dictionaries serialize properly

    def dump_value(self, v):
        """Override default behavior to avoid quotes around any values, as required by ngen-cal."""
        if isinstance(v, str):
            return v  # Always return the raw string without quotes
        if isinstance(v, bool):  # Ensure booleans remain lowercase as per TOML spec
            return "true" if v else "false"
        return super().dump_value(v)


def build_config(config: dict, directory: str, filename: str) -> str:
    """
    Builds the configuration file for the run and saves it to the specified directory.

    :param config: The configuration dictionary to be saved.
    :param directory: The directory in which to save the configuration file.
    :param filename: The filename in which to save the configuration file.
    :return: The path to the saved configuration file.
    """
    config_file = os.path.join(directory, filename)

    logger.info(f'Saving config to {config_file}')

    # Use the custom encoder to format TOML correctly without quotes
    toml_string = toml.dumps(config, encoder=CustomTomlEncoder())

    with open(config_file, 'w', encoding='utf-8') as file:
        file.write(toml_string)

    return config_file


def is_missing(value: Any, label: str, report: ErrorReport, have_LSTM_flag: bool = False) -> bool:
    """
    Checks if a required value is missing, and logs an error if so.

    :param value: The value to check.
    :param label: A descriptive name of the value (used in the error message).
    :param report: ErrorReport instance to record the error.
    :param have_LSTM_flag If true, then we don't check this field
    :return: True if the value is None, False otherwise.
    """
    if value is None:
        if not have_LSTM_flag:
            report.add_warning(f'{label} is required')
        return True
    return False


# Global table of node type rules.
# Each pair represents [max_catchments, node_type]
NODE_TYPE_RULES = [
    [500, 'c5n-9xlarge'],
    [-1, 'r8a-12xlarge']
]


def get_node_type(num_catchments: int) -> str:
    node_type = None
    for max_catchments, node_type in NODE_TYPE_RULES:
        if max_catchments == -1 or num_catchments <= max_catchments:
            break

    logger.info(f'{num_catchments} catchments using node type {node_type}')
    return node_type
