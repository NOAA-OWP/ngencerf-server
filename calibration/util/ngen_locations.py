import logging
import os
from typing import Literal

from django.conf import settings

from calibration.enums import ValidationType
from calibration.models import CalibrationRun, ForecastRun, ValidationRun
from cerfServer.settings import NGEN_ENVIRONMENT

logger = logging.getLogger(__name__)

static_dirs = [
    NWM_RETROSPECTIVE_DIR := os.path.join(settings.NGEN_STATIC_DIR, 'nwm_retrospective'),
    PARQUET_DIR := os.path.join(settings.NGEN_STATIC_DIR, 'parquet'),
    NGEN_MODULE_PARAMETERS := os.path.join(settings.NGEN_STATIC_DIR, 'module_parameter_files'),
    FORECAST_FORCING_TEMPLATES := os.path.join(settings.NGEN_STATIC_DIR, 'forecast_forcing_templates')
]

files = [
    NGEN_EXE := os.path.join(settings.NGEN_REPO_ROOT, 'cmake_build', 'ngen'),
    PARALLEL_NGEN_EXE := os.path.join(settings.NGEN_REPO_ROOT, 'cmake_build', 'ngen'),
    PARTITION_GENERATOR_EXE := os.path.join(settings.BASE_DIR, 'partitionGenerator'),
    CFE_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'cfe', 'cmake_build', 'libcfebmi.so'),
    SLOTH_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'sloth', 'cmake_build', 'libslothmodel.so'),
    TOPMD_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'topmodel', 'cmake_build', 'libtopmodelbmi.so'),
    NOAH_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'noah-owp-modular', 'cmake_build', 'libsurfacebmi.so'),
    SFT_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'SoilFreezeThaw', 'cmake_build', 'libsftbmi.so'),
    SMP_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'SoilMoistureProfiles', 'cmake_build', 'libsmpbmi.so'),
    LASAM_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'LASAM', 'cmake_build', 'liblasambmi.so'),
    PET_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'evapotranspiration', 'evapotranspiration', 'cmake_build', 'libpetbmi.so'),
    SNOW17_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'snow17', 'cmake_build', 'libsnow17bmi.so'),
    SAC_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'sac-sma', 'cmake_build', 'libsacbmi.so'),
    UEB_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'ueb-bmi', 'cmake_build', 'src', 'libbmiuebcxx.so')
]

forecast_forcing_scripts = [
    FORCING_MESH_SCRIPT_PATH := os.path.join(settings.NGEN_FORCING_REPO_ROOT, 'ESMF_Mesh_Domain_Configuration_Production',
                                             'NextGen_hyfab_to_ESMF_Mesh.py'),
    FORCING_EXTRACTION_SCRIPT_PATH := os.path.join(settings.NGEN_FORCING_REPO_ROOT, 'Forcing_Extraction_Scripts'),
    FORCING_BMI_SCRIPT_PATH := os.path.join(settings.NGEN_FORCING_REPO_ROOT, 'NextGen_Forcings_Engine_BMI', 'run_bmi_model.py')
]

forecast_work_directories = [
    FORCING_RAW_INPUT := os.path.join(settings.NGEN_FORCING_WORK_DIR, 'raw_input'),
    FORCING_ESMF_MESH := os.path.join(settings.NGEN_FORCING_WORK_DIR, 'esmf_mesh'),
    FORCING_HRRR := os.path.join(FORCING_RAW_INPUT, 'HRRR'),
    FORCING_RAP := os.path.join(FORCING_RAW_INPUT, 'RAP'),
]

for f in forecast_work_directories:
    os.makedirs(f, exist_ok=True)
    # On PW, the server runs as root, but the Slurm jobs do not, so we need to adjust the permissions
    os.chmod(f, 0o777)


def check_files():
    # If we are running locally,then ngen and ngen-cal files must be on our machine
    if NGEN_ENVIRONMENT == NGEN_ENVIRONMENT.LOCAL:
        for file in files:
            if not os.path.isfile(file):
                logger.warning(f'{file} does not exist')

    for directory in static_dirs:
        if not os.path.isdir(directory):
            logger.warning(f'{directory} does not exist')
        elif not os.listdir(directory):
            logger.warning(f'{directory} is empty')


# Construct the directory where the Input/Output is
def get_gage_dir(run: CalibrationRun) -> str:
    objective_function_name = run.objective_function.name if run.objective_function else 'None'
    optimization_name = run.optimization.name if run.optimization else 'None'
    return os.path.join(
        run.job_data_dir,
        f"{objective_function_name.lower()}_{optimization_name.lower()}",
        run.user_formulation_name,
        run.gage.gage_id
    )


def get_realization_file_path(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), f"{run.gage.gage_id}_realization_config_bmi_calib.json")


def get_forcing_filename_pattern() -> str:
    return r"^cat-\d+\.csv$"


def get_bmi_config_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'bmi_config')


def get_bmi_config_dir_for_module(run: CalibrationRun, module_name: str) -> str:
    return os.path.join(get_bmi_config_dir_for_job(run), module_name.lower())


def get_bmi_config_key(module_name: str) -> str:
    return f"{module_name.lower().replace('-', '_')}_bmi_dir"


# Job-specific forcing directory
def get_forcing_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'forcing')


# Job-specific observation directory
def get_observational_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'observation')


def get_observational_filename(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_hourly_discharge.csv"


# Job-specific observation file
def get_observational_file_for_job(run: CalibrationRun) -> str:
    return os.path.join(get_observational_dir_for_job(run), get_observational_filename(run)) if run.gage else None


# Job-specific geopackage directory
def get_geopackage_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'geopackage')


def get_ngen_stdout_log_filename() -> str:
    return 'ngen_stdout_stderr.log'


def get_ngen_log_path(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'logs', 'ngen.log')


def get_input_dir(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'Input')


def get_output_dir(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'Output')


def get_output_calibration_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Calibration_Run')


def get_output_validation_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Validation_Run')


def get_output_validation_plot_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'Plot_Valid')


def get_output_validation_iteration_plot_dir(run: CalibrationRun, iteration_num: int, worker_name: str) -> str:
    return os.path.join(get_output_validation_run_dir(run), f'Plot_Valid_{worker_name}_iter{iteration_num}')


def get_output_forecast_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Forecast_Run')


def get_full_worker_filename(short_worker_name: str) -> str:
    return f"ngen_{short_worker_name}_worker"


def get_calibration_worker_path(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_output_calibration_run_dir(run), get_full_worker_filename(short_worker_name))


def get_validation_output_valid(run: CalibrationRun, full_worker_name: str) -> str:
    return os.path.join(get_output_validation_run_dir(run), full_worker_name, 'Output_Valid')


def get_metrics_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_metrics_iteration.csv"


def get_output_last_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_last_iteration.csv"


def get_output_last_iteration_file(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_output_last_iteration_csv(run))


def get_output_best_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_best_iteration.csv"


def get_output_best_iteration_file(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_output_best_iteration_csv(run))


def get_output_valid_control_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_valid_control.csv"


def get_output_valid_control_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), get_output_valid_control_csv(run))


def get_output_valid_best_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_valid_best.csv"


def get_output_valid_best_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), get_output_valid_best_csv(run))


def get_output_valid_iteration_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_output_valid_{worker_name}_iter{iteration_num}.csv")


def get_output_iteration_csv(run: CalibrationRun, iteration_num: int) -> str:
    return f"{run.gage.gage_id}_output_iteration_{iteration_num:04d}.csv"


def get_output_iteration_file(run: CalibrationRun, iteration_num: int, worker_dir: str) -> str:
    return os.path.join(worker_dir, 'Output_Iteration', get_output_iteration_csv(run, iteration_num))


def get_metrics_iteration_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), get_metrics_iteration_csv(run))


def get_cost_hist_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"{run.gage.gage_id}_cost_hist.csv")


def get_metrics_iteration_file_from_worker_dir(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_metrics_iteration_csv(run))


def get_params_iteration_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), f"{run.gage.gage_id}_params_iteration.csv")


def get_objective_log_best_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), f"{run.gage.gage_id}_objective_log.txt")


def get_calibration_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), 'ngen-cal_calibration_stdout.log')


def get_calibration_performance_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), 'ngen-cal_calibration_performance.log')


def get_global_best_params_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"{run.gage.gage_id}_global_best_params.csv")


def get_calibration_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_input_dir(run), f"{run.gage.gage_id}_config_calib.yaml")


def get_validation_best_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_config_valid_best.yaml")


def get_validation_best_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'ngen-cal_validation_best_stdout.log')


def get_validation_control_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'ngen-cal_validation_control_stdout.log')


def get_validation_iteration_stdout_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"ngen-cal_validation_{worker_name}_iter{iteration_num}_stdout.log")


def get_forecast_dir(forecast_run: ForecastRun) -> str:
    return os.path.join(get_output_forecast_run_dir(forecast_run.calibration_run), f'forecast_{forecast_run.id}')


def get_forecast_output_dir(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'output')


def get_forecast_forcing_config_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'forecast_forcing_config.yaml')


# def get_forecast_forcing_cycle_config_file(forecast_run: ForecastRun) -> str:
#     return os.path.join(get_forecast_dir(forecast_run), f'{forecast_run.cycle.internal_name}_config.yaml')


def get_forecast_output_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_output_dir(forecast_run), f'{forecast_run.calibration_run.gage.gage_id}_output.csv')


#
# def get_forecast_forcing_download_stdout_file(forecast_run: ForecastRun) -> str:
#     return os.path.join(get_forecast_dir(forecast_run), 'forecast_forcing_download_stdout.log')


def get_forecast_stdout_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'forecast_stdout.log')


#
# def get_forecast_forcing_download_performance_file(forecast_run: ForecastRun) -> str:
#     return os.path.join(get_forecast_dir(forecast_run), 'forecast_forcing_download_performance.log')


def get_forecast_performance_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'forecast_performance.log')


def get_forecast_realization_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'{forecast_run.calibration_run.gage.gage_id}_realization_config_bmi_fcst.json')


def get_cold_start_realization_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'{forecast_run.calibration_run.gage.gage_id}_realization_config_bmi_cold_start.json')


#
# def get_forecast_forcing_download_path(forecast_run: ForecastRun) -> str:
#     return os.path.join(get_forecast_dir(forecast_run), f'forecast_forcing_{forecast_run.id}')


def get_forecast_temp_dir(forecast_run: ForecastRun) -> str:
    # TODO Need Kyle to create the directory, so we can use /tmp and not create it ourselves
    # temp_dir = os.path.join('/tmp', f'forcing_workdir_Calibration_{forecast_run.calibration_run.id}_Forecast_{forecast_run.id}')
    temp_dir = os.path.join(get_forecast_dir(forecast_run), 'scratch_dir')
    os.mkdir(temp_dir)
    return temp_dir


def get_validation_performance_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"ngen-cal_validation_{worker_name}_iter{iteration_num}_performance.log")


def get_swe_netcdf_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_swe.nc")


def get_validation_special_performance_file(run: CalibrationRun,
                                            validation_type: Literal[ValidationType.VALID_BEST, ValidationType.VALID_CONTROL]) -> str:
    validation_type_str = validation_type.value.split('_')[1].lower()
    return os.path.join(get_output_validation_run_dir(run), f"ngen-cal_validation_{validation_type_str}_performance.log")


def get_calibration_git_info_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"git_info_calibration.json")


def get_validation_special_git_info_file(run: ValidationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run.calibration_run), f"git_info_{run.validation_type}.json")


def get_validation_iteration_git_info_file(run: ValidationRun, worker_name: str, iteration_num: int):
    return os.path.join(get_output_validation_run_dir(run.calibration_run), f"git_info_{worker_name}_iter{iteration_num}.json")


#
# def get_forecast_download_git_info_file(forecast_forcing_download_run: ForecastForcingDownloadRun) -> str:
#     return os.path.join(get_forecast_dir(forecast_forcing_download_run.forecast_run), "git_info_forecast_download.json")


def get_forecast_git_info_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), "git_info_forecast.json")


def get_validation_metrics_valid_best_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_best.csv")


def get_validation_control_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_config_valid_control.yaml")


def get_validation_metrics_valid_control_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_control.csv")


def get_validation_metrics_nwm_retrospective_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_nwm_retro.csv")


def get_validation_metrics_valid_iteration_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_{worker_name}_iter{iteration_num}.csv")


def get_ngen_logging_basename() -> str:
    return "ngen_logging"


def get_ngen_logging_file(run: CalibrationRun | ValidationRun, import_flag: bool = False) -> str:
    calibration_run = run if isinstance(run, CalibrationRun) else run.calibration_run
    job_type = run.__class__.__name__.removesuffix('Run').lower()
    file_name = f"{get_ngen_logging_basename()}_{job_type}_{run.id}{'_import' if import_flag else ''}.json"
    return os.path.join(calibration_run.job_data_dir, file_name)
