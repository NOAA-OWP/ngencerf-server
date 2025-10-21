import json
import logging

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from calibration.models import CalibrationFormulation, CalibrationSlothParam, CalibrationParameter, CalibrationRun, \
    CalibrationStopCriteria
from calibration.util.caching import get_cached_module_by_name, get_cached_modules_with_groups, get_cached_module_groups, get_cached_modules_by_id
from calibration.util.calibration_validators import ValidateFormulationRequestSerializer, \
    SaveFormulationRequestSerializer, ErrorResponseSerializer, ValidateFormulationResponseSerializer, \
    SaveFormulationResponseSerializer, EmptySerializer, GetModulesResponseSerializer
from calibration.views import ngen_cal_input
from calibration.views.calibration_optimization_views import write_optimization_inputs
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, validate_request, SLOTH, \
    get_user_email, join_with_or, get_elapsed_str
from calibration.views.data_services import get_module_metadata_from_data_services, DataServicesException

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetModulesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get static list of modules and groups"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_modules(request) -> Response:
    """
    Retrieve module and group information

    :param request: The HTTP request containing either POST data or query parameters.
    :return: A JSON response with the calibration run ID, status, modules, and module groups.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    # Retrieve all modules with their groups from the cache
    cached_modules = get_cached_modules_with_groups()

    # Prepare modules list as dictionaries for response serialization
    module_groups_list = [
        {
            "name": module.name,
            "display_name": module.display_name,
            "description": module.description,
            "is_active": module.is_active,
            "groups": sorted([g.name for g in module.groups.all()], key=lambda n: n)

        }
        for module in cached_modules.values()
    ]

    # Retrieve ordered list of module groups from cache
    module_groups = get_cached_module_groups()

    response = {'modules': module_groups_list, 'module_groups': module_groups}

    response_validator, error_response = validate_response(GetModulesResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def get_sloth_parameters(run: CalibrationRun) -> list[dict[str, str]]:
    """
    Retrieve Sloth parameters for a given calibration run.
    Uses cached modules keyed by ID to resolve maps_to_module names.

    :param run: The calibration run instance.
    :return: A list of Sloth parameters formatted as dictionaries.
    """
    sloth_parameters = list(
        CalibrationSlothParam.objects.filter(calibration_run=run).values(
            'param_name', 'param_count', 'param_type', 'param_units',
            'param_location', 'param_value', 'maps_to_module_id', 'maps_to_variable_name'
        )
    )

    modules_by_id = get_cached_modules_by_id()
    for sp in sloth_parameters:
        module = modules_by_id.get(sp['maps_to_module_id'])
        sp['maps_to_module'] = module.name if module else None
        del sp['maps_to_module_id']

    return sloth_parameters


@extend_schema(
    request=ValidateFormulationRequestSerializer,
    responses={
        200: ValidateFormulationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Validate the module list from the formulation tab."
)
@api_view(['POST'])
@handle_exceptions
def validate_formulation_tab(request) -> Response:
    """
    Validate the module list from the formulation tab.

    :param request: The HTTP request containing POST data with a list of modules.
    :return: A JSON response with any warnings or errors.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ValidateFormulationRequestSerializer, data)
    if error_return:
        return error_return

    new_module_names = set(validator.get('modules'))

    formulation_errors, formulation_warnings, formulation_messages = validate_formulation(new_module_names)

    response = {}
    if formulation_warnings:
        response['formulation_warnings'] = formulation_warnings
    if formulation_errors:
        response['formulation_errors'] = formulation_errors
    if formulation_messages:
        response['formulation_messages'] = formulation_messages

    response_validator, error_response = validate_response(ValidateFormulationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=SaveFormulationRequestSerializer,
    responses={
        200: SaveFormulationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Save formulation tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_formulation_tab(request) -> Response:
    """
    Save or update calibration formulations for a calibration run.

    Uses cached modules to avoid repeated SELECT queries on the Module table.
    The formulations are runtime data, but module lookups are resolved via
    the cache, eliminating ORM joins.

    :param request: The HTTP request containing POST data with formulation details.
    :return: A JSON response confirming the update along with any warnings or errors.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveFormulationRequestSerializer, data)
    if error_return:
        return error_return

    new_module_names = set(validator.get('modules'))
    calibration_run_id = validator.get('calibration_run_id')
    use_sloth = validator.get('use_sloth')
    sloth_parameters = validator.get('sloth_parameters')
    have_lstm = 'LSTM' in new_module_names
    if have_lstm and (sloth_parameters or use_sloth):
        return ResponseError("You cannot specify sloth_parameters or use_sloth when using LSTM")

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return

    run.user_formulation_name = validator.get('formulation_name')

    # Validate modules and formulation constraints
    error_message = validate_modules(new_module_names)
    if error_message:
        return ResponseError(error_message)

    # TODO Eventually, we will have more user properties that are specific to certain modules
    # so we'll need a separate table to control those.
    # For now, we are forced to hard-code module names and specific flags
    # Only allow AET Rootzone to be True if CFE is included in the formulation
    run.is_aet_rootzone = validator.get('is_aet_rootzone', False)
    if run.is_aet_rootzone and not any(cfe in new_module_names for cfe in ('CFE-S', 'CFE-X')):
        return ResponseError('AET Rootzone cannot be True for formulations not using CFE.')

    formulation_errors, formulation_warnings, _ = validate_formulation(new_module_names)

    if not use_sloth and sloth_parameters:
        return ResponseError(f'You must check the box to allow {SLOTH} parameters to be specified')

    # Set run.use_sloth and handle Sloth parameters
    run.use_sloth = use_sloth
    # Initialize the eds_errors list
    eds_errors: list[dict] = []

    # Fetch all formulations and determine changes
    existing_formulations_qs = CalibrationFormulation.objects.filter(calibration_run=run)
    existing_formulations_list = list(existing_formulations_qs.select_related('module'))
    existing_module_names = {f.module.name for f in existing_formulations_list}

    # Determine which modules to delete and add
    to_be_added = new_module_names - existing_module_names
    to_be_unused = existing_module_names - new_module_names

    with transaction.atomic():
        # Delete unused formulations
        if to_be_unused:
            logger.info(f"Deleting unused modules: {to_be_unused}")
            delete_unused_formulations(to_be_unused, run)

        # Add new formulations
        for module_name in to_be_added:
            module_instance = get_cached_module_by_name(module_name)
            # Only create if it doesn't already exist to avoid expensive indexing
            if not any(f.module_id == module_instance.id for f in existing_formulations_list):
                CalibrationFormulation.objects.create(calibration_run=run, module=module_instance)

        # Identify formulations without any calibration parameters, in case there was an error retrieving them
        param_formulation_ids = set(
            CalibrationParameter.objects
            .filter(calibration_formulation__in=existing_formulations_list)
            .values_list('calibration_formulation_id', flat=True)
        )

        formulations_without_params_qs = existing_formulations_qs.exclude(id__in=param_formulation_ids)

        required_formulations_qs = existing_formulations_qs.filter(
            module__name__in=to_be_added
        ) | formulations_without_params_qs  # type: ignore

        # Retrieve metadata for required formulations
        if required_formulations_qs.exists() and run.gage:
            logger.info(f"Fetching metadata for modules: {list(to_be_added)}")
            try:
                # Append new errors to the existing list
                eds_errors.extend(get_module_metadata_from_data_services(run, required_formulations_qs))
            except DataServicesException as e:
                logger.exception("Error retrieving module parameter data from Data Services")
                eds_errors.append({
                    'name': 'parameters',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })

        # Delete existing Sloth params for this run and re-add them
        CalibrationSlothParam.objects.filter(calibration_run=run).delete()

        error_message = add_sloth_parameters(run, sloth_parameters, new_module_names)
        if error_message:
            logger.error(f"Error adding Sloth parameters: {error_message}")
            return ResponseError(error_message)

        # If formulation uses LSTM, we need to clear all irrelevant fields
        if have_lstm:
            # clear core CalibrationRun fields
            run.optimization = None
            run.objective_function = None
            run.streamflow_threshold = None
            run.peak_flow_threshold = None
            run.save_plot_iteration_frequency = None
            run.save_output_iteration = False

            # remove stop criteria
            CalibrationStopCriteria.objects.filter(calibration_run=run).delete()

            # No optimization inputs
            write_optimization_inputs(run, [])

        run.save()

    ngen_cal_input.ready_to_run(run)

    response = {
        'message': f'Calibration Job {run.id} updated',
        'calibration_run_id': run.id,
        'status': run.status.name,
    }
    if formulation_warnings:
        response['formulation_warnings'] = formulation_warnings
    if formulation_errors:
        response['formulation_errors'] = formulation_errors
    if eds_errors:
        response['eds_errors'] = eds_errors

    response_validator, error_response = validate_response(SaveFormulationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def delete_unused_formulations(to_delete_modules: set[str], run: CalibrationRun) -> None:
    """
    Delete unused formulations and related parameters for a given calibration run.

    :param to_delete_modules: A set of module names for formulations to delete.
    :param run: The calibration run instance.
    :return: None.
    """
    formulations_to_delete_qs = CalibrationFormulation.objects.filter(
        calibration_run=run,
        module__name__in=to_delete_modules
    ).only("id")

    # Delete CalibrationParameters related to the formulations_to_delete
    param_qs = CalibrationParameter.objects.filter(calibration_formulation__in=formulations_to_delete_qs)
    while True:
        batch_ids = list(param_qs.values_list("id", flat=True)[:500])
        if not batch_ids:
            break
        CalibrationParameter.objects.filter(id__in=batch_ids).delete()

    # Finally, delete the formulations
    formulations_to_delete_qs.delete()


def validate_modules(module_names: set[str]) -> str | None:
    """
    Validate that all the provided module names exist in the cached modules.

    :param module_names: A set of module names to validate.
    :return: An error message if any module name is invalid; otherwise, None.
    """
    valid_names = {name for name in module_names if get_cached_module_by_name(name)}
    if module_names - valid_names:
        return f'Invalid modules - {module_names - valid_names}'
    return None


formulation_validations = {
    "formulation_rules": {
        "group_requirements": {
            "Glacier": {
                "expected_counts": [0, 1],
                "fatal": True
            },
            "Snowmelt": {
                "expected_counts": [0, 1],
                "fatal": False
            },
            "Evapotranspiration": {
                "expected_counts": [1],
                "fatal": True
            },
            "Rainfall Runoff": {
                "expected_counts": [1],
                "fatal": True
            },
            "Soil Moisture": {
                "expected_counts": [0, 2],
                "fatal": True
            },
            "Routing": {
                "expected_counts": [1],
                "fatal": True
            }
        },
        "module_exclusions": {
            "SMP": {
                "must_have": ["CFE-S", "CFE-X", "LASAM", "TopModel"],
                "fatal": True
            },
            "SFT": {
                "must_have": ["CFE-S", "CFE-X", "LASAM", "TopModel"],
                "fatal": True
            }
        }
    }
}


def validate_formulation(module_names: set[str]) -> tuple[list[str], list[str], list[str]]:
    """
    Validate formulation rules based on group requirements and exclusions.

    Uses cached modules/groups to avoid repeated DB hits.

    :param module_names: A set of module names to validate.
    :return: A tuple of lists (fatal_errors, nonfatal_errors, info_messages).
             Each list contains validation messages of the corresponding severity.
             If there are no messages of a given severity, that list will be empty.
    """

    # Prepare containers for fatal vs. non-fatal vs. info messages
    fatal_errors: list[str] = []
    nonfatal_errors: list[str] = []
    info_messages: list[str] = []

    cached_modules = get_cached_modules_with_groups()

    # --- Special case: if LSTM is present, enforce LSTM-specific rules and skip the rest ---
    if "LSTM" in module_names:
        if len(module_names) > 2:
            # More than two modules with LSTM is not allowed
            fatal_errors.append("LSTM cannot be combined with more than one other module.")
            return fatal_errors, nonfatal_errors, info_messages

        if len(module_names) < 2:
            # LSTM alone (no other module) is not allowed
            fatal_errors.append(
                "When LSTM is specified, exactly one other Routing module must be included."
            )
            return fatal_errors, nonfatal_errors, info_messages

        # At this point, len(module_names) == 2 and one of them is LSTM
        other_name = next(name for name in module_names if name != "LSTM")
        other_module = cached_modules.get(other_name)
        if not other_module:
            fatal_errors.append(f"Unknown module '{other_name}' in LSTM formulation.")
            return fatal_errors, nonfatal_errors, info_messages

        other_groups = [g.name for g in other_module.groups.all()]
        if "Routing" not in other_groups:
            fatal_errors.append(
                f"When LSTM is specified, the other module must be in the Routing group; found: {other_name}"
            )
            return fatal_errors, nonfatal_errors, info_messages

        # Check for completeness
        check_completeness(module_names, fatal_errors, nonfatal_errors, info_messages)

        if not fatal_errors:
            info_messages.append('Formulation is Calibratable.')
        else:
            fatal_errors.append('Formulation is not Calibratable.')

        return fatal_errors, nonfatal_errors, info_messages

    # --- End of LSTM special case. All further checks assume LSTM is NOT present. ---

    # Perform checks for non-LSTM case
    my_modules = [cached_modules[name] for name in module_names if name in cached_modules]

    # Count how many selected modules belong to each group
    group_defs = formulation_validations["formulation_rules"]["group_requirements"]
    group_counts = {grp_name: 0 for grp_name in group_defs}

    # Parse the groups for each module once and update the group counts
    for module in my_modules:
        for group in module.groups.all():
            if group.name in group_counts:  # Only count groups that are in the group_requirements
                group_counts[group.name] += 1

    # 1) Check module_exclusions
    excl_defs = formulation_validations["formulation_rules"].get("module_exclusions", {})
    for excluded_module, rules in excl_defs.items():
        if excluded_module in module_names:
            must_have_modules = rules.get("must_have", [])
            # Check if any of the required modules are present
            if not any(m in module_names for m in must_have_modules):
                msg = f"{excluded_module} module cannot exist without one of: {', '.join(must_have_modules)}"  # type: ignore[arg-type]
                logger.warning(msg)
                if rules.get("fatal", True):
                    fatal_errors.append(msg)
                else:
                    nonfatal_errors.append(msg)

    # 2) Check group_requirements
    for group_name, group_rules in group_defs.items():
        expected_counts = group_rules.get("expected_counts", [])
        count = group_counts.get(group_name, 0)

        # Validate the count against expected_counts
        if count not in expected_counts:
            # Build the “1” vs “0 or 2” string
            expected_str = join_with_or([str(c) for c in expected_counts])
            # Choose singular if exactly [1], otherwise plural
            word = "module" if len(expected_counts) == 1 and expected_counts[0] == 1 else "modules"
            msg = f"{group_name} group is expected to have {expected_str} {word}, but it has {count}."
            if count > 1 and 'Noah-OWP-Modular' in module_names:
                msg += f" Noah-OWP-Modular will not be used for {group_name}."
            logger.warning(msg)
            if group_rules.get("fatal", False):
                fatal_errors.append(msg)
            else:
                nonfatal_errors.append(msg)

    # 3) Check for completeness
    check_completeness(module_names, fatal_errors, nonfatal_errors, info_messages)

    # 4) If no fatal errors, indicate that the formulation is Calibratable
    if not fatal_errors:
        info_messages.append('Formulation is Calibratable.')
    else:
        fatal_errors.append('Formulation is not Calibratable.')

    return fatal_errors, nonfatal_errors, info_messages


def check_completeness(module_names: set[str], fatal_errors: list[str], nonfatal_errors: list[str], info_messages: list[str]) -> None:
    """
    Check if the formulation is complete by ensuring all necessary modules are included.

    Uses cached modules and output variables to avoid extra DB queries.

    :param module_names: A set of module names to check for completeness.
    :param fatal_errors: List to append fatal errors.
    :param nonfatal_errors: List to append nonfatal errors.
    :param info_messages: List to append informational messages.
    :return: None.
    """
    cached_modules = get_cached_modules_with_groups()
    modules_included = [cached_modules[name] for name in module_names if name in cached_modules]

    # Get all output variable names from cacheable modules
    all_output_vars = {ov.name for m in cached_modules.values() for ov in m.output_variables.all()}
    included_output_vars = {ov.name for m in modules_included for ov in m.output_variables.all()}

    missing_output_vars = sorted(all_output_vars - included_output_vars)
    produced_output_vars = sorted(included_output_vars)

    if missing_output_vars:
        nonfatal_errors.append('Formulation Incomplete. Not all NWM v3 Output Variables can be produced.')
        nonfatal_errors.append('Missing NWM v3 Output Variables: ' + ", ".join(missing_output_vars))
    else:
        info_messages.append('Formulation Complete. All NWM v3 Output Variables can be produced.')

    if produced_output_vars:
        info_messages.append('NWM v3 Output Variables Produced: ' + ", ".join(produced_output_vars))


def add_sloth_parameters(run: CalibrationRun, sloth_parameters: list[dict], module_names: set[str]) -> str | None:
    """
    Add Sloth parameters to a calibration run, validating module associations.

    :param run: The calibration run instance.
    :param sloth_parameters: A list of dictionaries containing Sloth parameter data.
    :param module_names: A set of module names included in the run.
    :return: An error message if a Sloth parameter is invalid; otherwise, None.
    """
    sloth_param_objects = []
    if sloth_parameters is not None:
        for s in sloth_parameters:
            module = get_cached_module_by_name(s['maps_to_module'])
            if not module or module.name not in module_names:
                return f"Sloth parameter '{s['param_name']}' has an invalid module - '{s['maps_to_module']}'.  This module has not been added to this run"

            sloth_param_objects.append(
                CalibrationSlothParam(
                    calibration_run=run, param_name=s['param_name'], param_count=s['param_count'],
                    param_type=s['param_type'], param_units=s['param_units'], param_location=s['param_location'],
                    param_value=s['param_value'], maps_to_module=module, maps_to_variable_name=s['maps_to_variable_name']
                )
            )

        CalibrationSlothParam.objects.bulk_create(sloth_param_objects)

    return None
