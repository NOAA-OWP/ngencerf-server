import logging
import time
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet

from calibration.enums import ForcingSourceEnum, DomainEnum
from calibration.models import CalibrationParameter, CalibrationFormulation, CalibrationRun
from calibration.util.caching import get_cached_module_by_name, get_cached_modules_by_id
from calibration.util.calibration_validators import ModuleDataListSerializer, S3FileValidator
from calibration.util.cloud_util import copy_tree, path_exists, join_url, is_dir
from calibration.util.ngen_locations import get_bmi_config_dir_for_module
from calibration.views.common import validate_response_data
from data_services_test_data import data_services_test_data

logger = logging.getLogger(__name__)

default_headers = {
    "Content-Type": "application/json"
}


def fetch_from_data_services(method: str, url: str, headers: dict = None, payload: dict = None) -> dict:
    """
    Sends an HTTP request to Data Services and processes the response.

    :param method: HTTP method (e.g., 'GET' or 'POST').
    :param url: The full URL of the Data Services endpoint.
    :param headers: Optional HTTP headers to include.
    :param payload: Optional JSON payload for POST requests.
    :return: The response JSON data as a dictionary.
    :raises: DataServicesException: For any HTTP or connection-related errors.
    """
    status_code = None
    response_text = None
    logger.info(f'Sending request to {url}')
    if payload:
        logger.info(f"Data Services payload: {payload}")

    try:
        start_time = time.perf_counter()  # Record the start time for performance tracking

        # Send the appropriate HTTP request based on the method
        if method == 'GET':
            response = requests.get(url, headers=headers)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=payload)
        else:
            raise DataServicesException(f"Unsupported HTTP method: {method}")

        # Log the time taken for the request
        elapsed_time = time.perf_counter() - start_time
        minutes, seconds = divmod(elapsed_time, 60)  # Convert to minutes and seconds
        logger.info(f"Request to {url} took {int(minutes)}:{int(seconds):02} (minutes:seconds).")

        # Capture status code and response content before raising an exception
        status_code = response.status_code
        response_text = response.text

        # Handle potential errors based on the status code
        if 400 <= status_code < 500:
            logger.error(f"Client error while accessing {url}: {status_code} - {response_text}")
        elif 500 <= status_code:
            logger.error(f"Server error while accessing {url}: {status_code} - {response_text[:1000] + '... (truncated)'}")

        # Check if the response is HTML instead of JSON (indicating an error page)
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' in content_type:
            msg = f"Call to {url} returned HTML error from Data Services"
            logger.error(msg)
            raise DataServicesException(msg, status_code)

        response.raise_for_status()  # Raise HTTPError for bad responses

        # Parse the response JSON and validate its format
        response_data = response.json()

        # Ensure the response is a dictionary
        if not isinstance(response_data, dict):
            raise DataServicesException(
                f"Unexpected response format: Expected a dictionary but got {type(response_data).__name__}. Response: {response_data}"
            )

        return response_data

    except requests.exceptions.HTTPError as e:
        message = f"Call to {url} failed with {status_code}. Response text: {response_text if response_text else 'No response received'}"
        logger.error(message)
        raise DataServicesException(message, status_code) from e

    except requests.exceptions.RequestException as e:
        # Handle connection, timeout, or other request errors
        message = f"Call to {url} failed to connect or timed out"
        logger.error(message)
        raise DataServicesException(message) from e

    except ValueError as e:
        # Handle invalid JSON responses
        logger.error(f"Invalid JSON response from {url}: {response_text}")
        raise DataServicesException("Invalid JSON received from Data Services") from e


class DataServicesException(Exception):
    """
    Custom exception for errors related to Data Services.

    :param message: Description of the error.
    :param status_code: Optional HTTP status code associated with the error.
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_geopackage_from_data_services(run: CalibrationRun):
    """
    Retrieves GeoPackage data from Data Services and updates the CalibrationRun instance.

    :param run: A CalibrationRun object with associated gage information.
    """
    if run.gage:
        if settings.ENTERPRISE_DATA_GEOPACKAGE_ENDPOINT[0]:
            logger.info('Getting geopackage from Data Services')
            url = urljoin(settings.ENTERPRISE_DATA_URL, settings.ENTERPRISE_DATA_GEOPACKAGE_ENDPOINT[1].format(
                gage_id=run.gage.gage_id,
                source=run.gage.agency,
                domain=run.gage.domain.name,
                version=settings.ENTERPRISE_DATA_VERSION
            ))
            geopackage_json = fetch_from_data_services('GET', url, headers=default_headers)
        else:
            logger.info('Getting dummy geopackage data')
            geopackage_json = data_services_test_data.geopackage_sample_data

        eds_data = validate_response_data(S3FileValidator, geopackage_json, 'Geopackage data from Data Services is not in the expected format')

        run.geopackage_eds_file_path = eds_data.get('uri')
        if run.geopackage_eds_file_path and not path_exists(run.geopackage_eds_file_path):
            raise DataServicesException(f"Geopackage from Data Services, {run.geopackage_eds_file_path} does not exist")
        logger.info(f'Setting run.geopackage_eds_file_path to {run.geopackage_eds_file_path}')


def get_observational_data_from_data_services(run: CalibrationRun):
    """
    Retrieves observational data from Data Services and updates the CalibrationRun instance.

    :param run: A CalibrationRun object with associated gage information.
    """
    if settings.ENTERPRISE_DATA_OBSERVATION_DATA_ENDPOINT[0]:
        logger.info('Getting observational data from Data Services')
        url = urljoin(settings.ENTERPRISE_DATA_URL,
                      settings.ENTERPRISE_DATA_OBSERVATION_DATA_ENDPOINT[1].format(
                          gage_id=run.gage.gage_id,
                          agency=run.gage.agency,
                          domain=run.gage.domain.name))
        observational_json = fetch_from_data_services('GET', url, headers=default_headers)
    else:
        logger.info('Getting dummy observational data')
        observational_json = data_services_test_data.observational_sample_data

    observational_data = validate_response_data(S3FileValidator, observational_json,
                                                'Observational data from Data Services is not in the expected format')

    run.observational_eds_file_path = observational_data.get('uri')
    if run.observational_eds_file_path and not path_exists(run.observational_eds_file_path):
        logger.error(f"Observational file from Data Services, {run.observational_eds_file_path} does not exist")
    clear_times(run)
    logger.info(f'Setting run.observational_eds_file_path to {run.observational_eds_file_path}')


def clear_times(run: CalibrationRun, cli: bool = False):
    """
    Clears the time-related fields of a CalibrationRun instance, forcing a recalculation later.

    This function resets all time and period fields to None, which is useful when the GUI triggers
    a recalculation of these time boundaries. If the operation is initiated via the CLI (cli=True),
    the time fields are preserved because it is assumed that the user intends to keep them as set.

    :param run: A CalibrationRun instance whose time-related fields will be cleared.
    :param cli: A boolean flag indicating if the process is running from the CLI.
                If True, the time fields are not cleared.
    """
    if not cli:
        run.time_range_start = None
        run.time_range_end = None
        run.calibration_start_period = None
        run.calibration_end_period = None
        run.validation_start_period = None
        run.validation_end_period = None
        run.calibration_eval_start_period = None
        run.calibration_eval_end_period = None
        run.validation_eval_start_period = None
        run.validation_eval_end_period = None


def should_use_bmi_forcing(run: CalibrationRun) -> bool:
    # Use BMI forcing only if CONUS + AORC and a gage is present
    if run.gage is None:
        return False

    # Use BMI forcing only if Conus and AORC
    return settings.USE_BMI_FORCING and run.gage.domain == DomainEnum.CONUS.db_instance and run.forcing_source_requested == ForcingSourceEnum.AORC.db_instance


def get_forcing_data_from_s3(run: CalibrationRun, forcing_source_name: str):
    """
    Attempts to retrieve forcing data from configured S3 directories.

    settings.FORCING_DATA_DIRS_xxx is a dict of S3 URLs (prefixes).

    :param run: A CalibrationRun object with associated gage information.
    :param forcing_source_name: The name of the forcing source to retrieve data for.
    :raises DataServicesException: If the forcing data cannot be found in the local S3 directories.
    """
    if should_use_bmi_forcing(run):
        logger.info("Skipping forcing retrieval for CONUS and AORC")
        return

    forcing_containers = (
        settings.FORCING_DATA_DIRS_AORC
        if forcing_source_name == ForcingSourceEnum.AORC.value
        else settings.FORCING_DATA_DIRS_RETRO
    )

    for src_key, s3_uri in forcing_containers.items():
        # <prefix>/<domain>/Gage_<gage_id>
        forcing_dir = join_url(s3_uri, run.gage.domain.name, f"Gage_{run.gage.gage_id}")

        if is_dir(forcing_dir):
            logger.info(f"Found forcing directory {forcing_dir}")
            run.forcing_eds_dir_path = forcing_dir
            run.forcing_source_actual = ForcingSourceEnum.get_instance(src_key)
            clear_times(run)
            logger.info(
                "Setting run.forcing_eds_dir_path to %s; forcing_source_actual=%s",
                run.forcing_eds_dir_path, run.forcing_source_actual
            )
            return
        else:
            logger.info(
                "Forcing directory for gage %s doesn't exist at %s (key: %s)",
                run.gage.gage_id, forcing_dir, src_key
            )

    raise DataServicesException(f"Could not find forcing data for gage {run.gage.gage_id}")


def get_module_metadata_from_data_services(run: CalibrationRun,
                                           calibration_formulations: QuerySet[CalibrationFormulation],
                                           gage_changed: bool = False) -> list[dict]:
    """
    Retrieves module metadata from Data Services and updates the database with module parameters and output variables.

    :param run: A CalibrationRun object with associated gage information.
    :param calibration_formulations: QuerySet of CalibrationFormulations for the run.
    :param gage_changed: Boolean indicating whether the gage has changed:
                         - If False: Indicates the modules have changed.
                         - If True: Indicates the gage has changed, and we want to retain the min/max values
                           for existing parameters while updating their initial values.
    :return: A list of dictionaries containing potential errors.
    :raises DataServicesException: If required module metadata is missing.
    """
    gage = run.gage

    # Use cached modules to resolve names (avoid DB hit)
    modules_by_id = get_cached_modules_by_id()

    # Only fetch module_id from the DB
    my_module_names_set = [
        modules_by_id[f.module_id].name
        for f in calibration_formulations.only("module_id")
        if f.module_id in modules_by_id
    ]

    # Fetch module metadata from Data Services or use test data
    if settings.ENTERPRISE_DATA_MODULE_METADATA_ENDPOINT[0]:
        logger.info('Fetching module metadata from Data Services')
        url = urljoin(settings.ENTERPRISE_DATA_URL, settings.ENTERPRISE_DATA_MODULE_METADATA_ENDPOINT[1])

        module_json = fetch_from_data_services(
            'POST',
            url,
            headers=default_headers,
            payload={'modules': my_module_names_set,
                     'gage_id': gage.gage_id,
                     'domain': gage.domain.name,
                     'source': gage.agency,
                     'version': settings.ENTERPRISE_DATA_VERSION})
    else:
        logger.info('Using dummy module metadata')
        module_json = data_services_test_data.eds_module_metadata_real_data

    module_metadata = validate_response_data(
        ModuleDataListSerializer, module_json,
        'Module metadata from Data Services is not in the expected format')

    fix_module_metadata(module_metadata)

    # Extract module names from the response for comparison
    eds_module_names = {module['module_name'] for module in module_metadata['modules']}
    my_module_names_set = set(my_module_names_set)

    # Determine discrepancies between requested and returned modules
    missing_names = my_module_names_set - eds_module_names
    extra_names = eds_module_names - my_module_names_set

    eds_errors = []

    # Preload formulations into a dict (avoid per-loop .get())
    formulation_map = {
        f.module_id: f for f in calibration_formulations
    }

    new_params: list[CalibrationParameter] = []
    to_update: list[CalibrationParameter] = []

    # Save module parameters to the database
    for module in module_metadata.get('modules'):
        module_name = module['module_name']
        # See if we have optional field
        error = module.get('error')
        if error:
            eds_errors.append({
                'name': 'parameters',
                'message': error,
                'status_code': None
            })
            continue

        if module_name in extra_names:
            # Ignore any extra names that Data Services sent us
            logger.warning(f'Ignoring extra module from Data Services - {module_name}')
            continue

        # Resolve module via cache
        module_instance = get_cached_module_by_name(module_name)
        calibration_formulation = formulation_map.get(module_instance.id if module_instance else None)
        if not calibration_formulation:
            raise DataServicesException(f"No formulation found for module {module_name}")

        # New (cloud-agnostic, no FUSE mount needed):
        src_prefix = module['parameter_file']['uri']  # e.g. "s3://bucket/path/to/dir/"
        dst_dir = get_bmi_config_dir_for_module(run, module_name)  # local directory path

        # copy the BMI parameters
        _ = copy_tree(src_prefix, dst_dir)

        # Save or update parameters for the module
        parameters = module.get('calibrate_parameters', [])
        if not parameters:
            logger.warning(f"Module '{module_name}' has no calibratable parameters.")
        else:
            for param in parameters:
                # Data Services gives us initial_value, min and max as Strings because sometimes crap appears.

                # Convert values to floats safely
                initial_value = safe_float(param.get('initial_value'), "Initial value", param.get('name'), module_name)
                min_value = safe_float(param.get('min'), "Minimum value", param.get('name'), module_name)
                max_value = safe_float(param.get('max'), "Maximum value", param.get('name'), module_name)

                new_param = CalibrationParameter(
                    name=param['name'],
                    calibration_formulation=calibration_formulation,
                    data_type=param['data_type'],
                    description=param['description'],
                    initial_value=initial_value,
                    minimum=min_value,
                    maximum=max_value,
                    units=param['units']
                )
                new_params.append(new_param)

        # Bulk insert (ignore_conflicts ensures no crash if they already exist)
        if new_params:
            CalibrationParameter.objects.bulk_create(new_params, ignore_conflicts=True)

        # If gage_changed, bulk update initial_value for existing params
        if gage_changed and new_params:
            existing_params = CalibrationParameter.objects.filter(
                calibration_formulation__in=formulation_map.values(),
                name__in=[p.name for p in new_params]
            )
            existing_lookup = {(p.calibration_formulation_id, p.name): p for p in existing_params}
            for param in new_params:
                key = (param.calibration_formulation.id, param.name)
                if key in existing_lookup:
                    existing_lookup[key].initial_value = param.initial_value
                    to_update.append(existing_lookup[key])

            if to_update:
                CalibrationParameter.objects.bulk_update(to_update, ['initial_value'])

    # Raise an exception if any requested modules are missing in the response
    if missing_names:
        raise DataServicesException(f'Response from Data Services is missing entries for: {missing_names}')

    return eds_errors


translation_map = {
    ("CFE-S", "soil_params.smcmax"): "maxsmc",
    ("CFE-S", "soil_params.satdk"): "satdk",
    ("CFE-S", "soil_params.slop"): "slope",
    ("CFE-S", "soil_params.b"): "b",
    ("CFE-S", "K_lf"): "Klf",
    ("CFE-S", "K_nash"): "Kn",
    ("CFE-S", "soil_params.satpsi"): "satpsi",
    ("CFE-S", "soil_params.wltsmc"): "wltsmc",

    ("CFE-X", "soil_params.smcmax"): "maxsmc",
    ("CFE-X", "soil_params.satdk"): "satdk",
    ("CFE-X", "soil_params.slop"): "slope",
    ("CFE-X", "soil_params.b"): "b",
    ("CFE-X", "K_lf"): "Klf",
    ("CFE-X", "K_nash"): "Kn",
    ("CFE-X", "soil_params.satpsi"): "satpsi",
    ("CFE-X", "soil_params.wltsmc"): "wltsmc",

    ("Noah-OWP-Modular", "MAXSMC"): "SMCMAX",
    ("Noah-OWP-Modular", "CWPVT"): "CWP",
    ("Noah-OWP-Modular", "SATDK"): "DKSAT",

    ("LASAM", "theta_e"): "smcmax",
    ("LASAM", "theta_r"): "smcmin",
    ("LASAM", "n"): "van_genuchten_n",
    ("LASAM", "alpha"): "van_genuchten_alpha",
    ("LASAM", "Ks"): "hydraulic_conductivity",
    ("LASAM", "field_capacity_psi"): "field_capacity",

    ("SFT", "soil_params.smcmax"): "smcmax",
    ("SFT", "soil_params.b"): "b",
    ("SFT", "soil_params.satpsi"): "satpsi",
    ("SFT", "soil_params.quartz"): "quartz",
    ("SFT", "soil_temperature"): "soil_temperature_profile",

    ("SMP", "soil_params.smcmax"): "smcmax",
    ("SMP", "soil_params.b"): "b",
    ("SMP", "soil_params.satpsi"): "satpsi",
}


def fix_module_metadata(metadata):
    """
    Translates parameter names in module metadata based on a translation map.

    :param metadata: Dictionary containing module metadata.
                     Example structure:
                     {
                         "modules": [
                             {
                                 "module_name": "module_name",
                                 "calibrate_parameters": [
                                     {"name": "full_param_name", "value": 123}
                                 ]
                             }
                         ]
                     }
    """
    for module in metadata["modules"]:
        module_name = module["module_name"]  # Extract the module name
        for param in module["calibrate_parameters"]:
            param_name = param["name"]  # Extract the parameter name
            key = (module_name, param_name)  # Create a tuple key
            # Check if the key exists in the translation_map
            if key in translation_map:
                logger.info(f"Translating {key} to {translation_map[key]}")
                param["name"] = translation_map[key]


def safe_float(value, label, param_name, module_name):
    """
    Attempt to convert a value to float. Log a warning and return None if the conversion fails.
    """
    try:
        return float(value) if value else None
    except (ValueError, TypeError):
        logger.warning(f"{label} '{value}' for parameter '{param_name}' for module '{module_name}' is not a valid float")
        return None
