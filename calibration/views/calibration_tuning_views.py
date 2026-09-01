import io
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict
from urllib.parse import urlparse

import pandas as pd
from datetimerange import DateTimeRange
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet, Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ForcingSourceEnum, JobType
from calibration.models import CalibrationFormulation, CalibrationParameter, CalibrationRun
from calibration.util import cloud_util
from calibration.util.caching import get_cached_module_by_name, have_LSTM, get_cached_modules_by_id
from calibration.util.calibration_validators import CalibrationRunIdSerializer, \
    SaveTuningRequestSerializer, LoadTuningResponseSerializer, \
    ValidateTuningTimesRequestSerializer, ValidateTuningTimesResponseSerializer, \
    ErrorResponseSerializer, UploadUserParameterFile, UserParameterFileUploadResponse, \
    ValidateParametersResponseSerializer, SaveTuningResponseSerializer
from calibration.views import ngen_cal_input
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, CerfException, validate_request, \
    format_datetime, get_user_email, get_elapsed_str, readonly_transaction, ErrorReport
from calibration.views.data_services import get_observational_date_range_from_data_services

logger = logging.getLogger(__name__)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: LoadTuningResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    parameters=[
        OpenApiParameter(name='calibration_run_id', description='ID of the calibration run', required=True, type=int)
    ],
    description="Load tuning tab data for a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_tuning_tab(request: Request) -> Response:
    """
    API endpoint to load tuning tab data for a calibration run.

    Splits read-heavy operations into a read-only transaction,
    then persists time_range if it was newly computed. Calls
    ready_to_run() outside the read-only block so that updates
    to run.status are persisted and reflected in the response.

    :param request: Django HTTP request, containing parameters in the body for POST or query params for GET.
    :return: Response containing the tuning tab data, including time ranges, modules, and formulations.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    # Phase 1: Read-only section (heavy reads)
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return
        assert run is not None

        # Compute time range without persisting
        time_range = compute_time_range(run)

        if time_range:
            # Normalize start time to the next midnight within the available range.
            normalized_start_time = time_range['start_time'].replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )
            if normalized_start_time < time_range['start_time']:
                normalized_start_time += timedelta(days=1)
            time_range['start_time'] = normalized_start_time

            # Normalize end time to the most recent 23:00 within the available range.
            normalized_end_time = time_range['end_time'].replace(
                hour=23,
                minute=0,
                second=0,
                microsecond=0
            )
            if normalized_end_time > time_range['end_time']:
                normalized_end_time -= timedelta(days=1)
            time_range['end_time'] = normalized_end_time

        calibration_times, validation_times, time_controls = get_times(run)

        formulations = (
            CalibrationFormulation.objects
            .filter(calibration_run=run)
            .select_related('module')
            .prefetch_related(
                Prefetch(
                    'calibrationparameter_set',
                    queryset=CalibrationParameter.objects.only(
                        'calibration_formulation_id',
                        'name', 'minimum', 'maximum', 'initial_value',
                        'units', 'data_type', 'description', 'user_selected_for_tuning'
                    ),
                    to_attr='prefetched_params',
                )
            )
        )

        # For each module, get the Parameters and Output Variables
        module_list = get_parameters(formulations)

    # Phase 2: Write section (ready_to_run + optional persist_time_range)
    ngen_cal_input.ready_to_run(run)

    if time_range and (
            run.time_range_start is None
            or run.time_range_end is None
    ):
        with transaction.atomic():
            persist_time_range(run, time_range)

    # Phase 3: Build response with updated run.status
    response = {
        'calibration_run_id': run.id,
        'status': run.status.name,  # reflects updated status
        'modules': module_list,
        'time_range': time_range,
        'calibration_times': calibration_times,
        'validation_times': validation_times,
        'time_controls': time_controls
    }

    response_validator, error_response = validate_response(LoadTuningResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def has_user_selected_tuning_parameters(formulations: QuerySet[CalibrationFormulation]) -> bool:
    """
    Check whether any parameters tied to the provided calibration formulations
    are marked user_selected_for_tuning.

    :param formulations: QuerySet of CalibrationFormulation objects for a single run.
    :return: True if at least one parameter in these formulations is marked
             user_selected_for_tuning, otherwise False.
    """
    return CalibrationParameter.objects.filter(
        calibration_formulation__in=formulations,
        user_selected_for_tuning=True
    ).exists()


def get_parameters(modules: QuerySet[CalibrationFormulation]) -> list[dict[str, str | list[dict[str, str | float | int]]]]:
    """
    Retrieves the calibration parameters for each module in the specified calibration formulation.

    Uses `select_related('module')` and prefetched CalibrationParameter objects to avoid DB hits.

    :param modules: QuerySet of CalibrationFormulation objects, built with
                    select_related('module') and Prefetch for calibrationparameter_set.
    :return: List of dicts with the module name and its parameters.
    """
    module_list: list[dict] = []

    # 'modules' must be built with select_related('module') and the Prefetch above.
    for formulation in modules:
        module = formulation.module  # Already populated by select_related

        # Use the prefetched list (no DB hits here)
        params = [
            {
                'name': p.name,
                'minimum': p.minimum,
                'maximum': p.maximum,
                'initial_value': p.initial_value,
                'units': p.units,
                'data_type': p.data_type,
                'description': p.description,
                'user_selected_for_tuning': p.user_selected_for_tuning,
            }
            for p in getattr(formulation, 'prefetched_params', [])
        ]

        module_list.append({
            'name': module.name,
            'parameters': params,
        })

    return module_list


def get_parameters_for_export(run: CalibrationRun) -> list[dict]:
    """
    Export calibration parameters for all modules in the given calibration run that have been selected by the user
    Uses cached modules to resolve names instead of hitting DB for Module.

    :param run: The CalibrationRun to export parameters from.
    :return: List of parameter dicts for export.
    """
    modules_by_id = get_cached_modules_by_id()

    # Query parameters linked to formulations by module_id
    params = (
        CalibrationParameter.objects
        .filter(calibration_formulation__calibration_run=run, user_selected_for_tuning=True)
        .values(
            "name", "initial_value", "minimum", "maximum",
            "calibration_formulation__module_id"
        )
    )

    result = []
    for p in params:
        module_id = p["calibration_formulation__module_id"]
        module_name = modules_by_id[module_id].name
        result.append({
            "name": p["name"],
            "initial_value": p["initial_value"],
            "minimum": p["minimum"],
            "maximum": p["maximum"],
            "module": module_name,
        })
    return result


def compute_time_range(run: CalibrationRun) -> dict[str, datetime]:
    """
    Compute the intersection of observational and forcing data ranges for the given run,
    without persisting anything to the database.

    Behavior:
      - If the run already has a persisted time range (both start and end), that exact range is returned.
      - If required data is missing, returns an empty dict.
      - If both sources are available, computes the intersection and returns a dictionary with:
          * 'start_time': datetime (UTC, timezone-aware),
          * 'end_time': datetime (UTC, timezone-aware).
      - If there is no valid overlap between observational and forcing ranges, returns an empty dict.

    :param run: CalibrationRun instance.
    :return: A dictionary containing 'start_time' and 'end_time', or {} if unavailable.
    """
    if run.time_range_start and run.time_range_end:
        logger.info("Time range is already set")
        return {'start_time': run.time_range_start, 'end_time': run.time_range_end}

    if not run.gage:
        logger.info("Skipping time range computation because run has no gage")
        return {}

    # If both paths are available, calculate intersection and update run
    daterange_intersection_start = time.perf_counter()

    daterange = get_date_range_intersection(run)

    logger.info(
        f"Date range intersection completed in "
        f"{time.perf_counter() - daterange_intersection_start:.2f}s"
    )

    if daterange:
        start_time = daterange.start_datetime
        end_time = daterange.end_datetime

        assert start_time is not None and end_time is not None

        return {
            'start_time': start_time,
            'end_time': end_time
        }

    return {}


def persist_time_range(run: CalibrationRun, time_range: dict[str, datetime]) -> None:
    """
    Persist the computed time range to the database.

    :param run: CalibrationRun instance to update.
    :param time_range: Dictionary containing both 'start_time' and 'end_time'.
                       Assumes these keys are present and valid datetimes.
    """
    run.time_range_start = time_range['start_time']
    run.time_range_end = time_range['end_time']
    run.save(update_fields=['time_range_start', 'time_range_end'])


TimeDict = dict[str, datetime]
TimeControlsResponse = dict[str, datetime | int | bool | None]


def get_times(
        run: CalibrationRun,
        default_time_controls: bool = True
) -> tuple[TimeDict, TimeDict, TimeControlsResponse]:
    """
    Return the persisted calibration and validation periods and time controls
    for a calibration run.

    Calibration and validation period dictionaries are returned only when all
    fields required for the respective period are present.

    When ``default_time_controls`` is True, unsaved time controls are omitted
    from the returned dictionary. This allows the tuning-tab response serializer
    to apply its UI defaults.

    When ``default_time_controls`` is False, all time-control fields are returned
    using their persisted values, including None. This is used when loading or
    exporting a complete calibration job so unsaved controls are represented as
    unset rather than replaced with suggested defaults.

    No default values are assigned by this function.

    :param run: CalibrationRun containing the persisted time periods and controls.
    :param default_time_controls: If True, omit unsaved controls so the tuning-tab
                                  response serializer can apply defaults. If False,
                                  return all controls exactly as persisted.
    :return: A tuple containing:
             - calibration_times: Calibration simulation and evaluation start/end
               times, or an empty dictionary if they are incomplete.
             - validation_times: Validation simulation and evaluation start/end
               times, or an empty dictionary if they are incomplete.
             - time_controls: Persisted UI time-control values. Depending on
               default_time_controls, unsaved fields are either omitted or
               included with a value of None.
    """
    calibration_times: TimeDict = {}
    validation_times: TimeDict = {}

    calibration_start_period = run.calibration_start_period
    calibration_end_period = run.calibration_end_period
    calibration_eval_start_period = run.calibration_eval_start_period
    calibration_eval_end_period = run.calibration_eval_end_period

    if (
            calibration_start_period is not None
            and calibration_end_period is not None
            and calibration_eval_start_period is not None
            and calibration_eval_end_period is not None
    ):
        calibration_times = {
            'simulation_start_time': calibration_start_period,
            'simulation_end_time': calibration_end_period,
            'calibration_start_time': calibration_eval_start_period,
            'calibration_end_time': calibration_eval_end_period
        }

    validation_start_period = run.validation_start_period
    validation_end_period = run.validation_end_period
    validation_eval_start_period = run.validation_eval_start_period
    validation_eval_end_period = run.validation_eval_end_period

    if (
            validation_start_period is not None
            and validation_end_period is not None
            and validation_eval_start_period is not None
            and validation_eval_end_period is not None
    ):
        validation_times = {
            'simulation_start_time': validation_start_period,
            'simulation_end_time': validation_end_period,
            'validation_start_time': validation_eval_start_period,
            'validation_end_time': validation_eval_end_period
        }

    persisted_controls: TimeControlsResponse = {
        'simulation_start_time': calibration_start_period,
        'warmup_duration': run.warmup_duration,
        'calibration_duration': run.calibration_duration,
        'validation_window_gap': run.validation_window_gap,
        'validation_window_after_calibration':
            run.validation_window_after_calibration,
        'validation_duration': run.validation_duration,
    }

    if default_time_controls:
        # Always include the simulation start time, even when it is unset. It has no
        # serializer default, and existing responses represent an unset value as None.
        time_controls: TimeControlsResponse = {
            'simulation_start_time': calibration_start_period
        }

        # Omit the remaining unsaved controls so the tuning-tab response serializer
        # can apply its UI defaults. The explicit is-not-None check preserves valid
        # values such as 0 and False.
        time_controls.update({
            name: value
            for name, value in persisted_controls.items()
            if name != 'simulation_start_time' and value is not None
        })
    else:
        # Full job loading and export must represent the values exactly as persisted,
        # including None for controls that have never been saved.
        time_controls = persisted_controls

    return calibration_times, validation_times, time_controls


@extend_schema(
    request=SaveTuningRequestSerializer,
    responses={
        200: SaveTuningResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Save tuning tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_tuning_tab(request: Request) -> Response:
    """
    Saves tuning settings for a calibration run, including parameters, output variables, and time periods.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveTuningRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    time_controls = validator.get('time_controls')
    parameters = validator.get('parameters')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    # Require at least one module selected for this job (i.e., at least one formulation exists)
    has_any_modules = CalibrationFormulation.objects.filter(calibration_run=run).exists()
    if not has_any_modules:
        return ResponseError("You must select at least one module before selecting tuning parameters")

    # --- Validate parameter selection rules ---
    module_names_for_job = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list("module__name", flat=True)
    )

    selected_module_names = {p["module"] for p in (parameters or []) if p.get("module")}

    parameter_rule_report = ErrorReport()
    validate_parameter_rules(
        module_names_for_job=module_names_for_job,
        selected_module_names=selected_module_names,
        have_LSTM_flag=have_LSTM(run),
        has_any_params=bool(selected_module_names),
        error_object=parameter_rule_report,
    )

    if have_LSTM(run) and parameters:
        return ResponseError('You cannot specify parameters when using LSTM')

    # The available forcing/observational time range should have been computed
    # and persisted when the tuning tab was loaded.
    if run.time_range_start is None or run.time_range_end is None:
        return ResponseError(
            "The available forcing and observational data range has not been established. "
            "Reload the tuning tab before saving."
        )

    error_message, calibration_times, validation_times, time_control_limits = calculate_times_and_limits(run, time_controls)
    if error_message:
        return ResponseError(error_message)

    error_message = save_time_controls(run, time_controls)
    if error_message:
        return ResponseError(error_message)

    if parameters and not run.gage:
        return ResponseError('Parameters cannot be specified without a gage')

    # The UI already does the parameter validation, so we don't have to bother sending the warnings
    parameter_errors, _ = validate_parameter_values(run, parameters)
    if parameter_errors:
        return ResponseError(parameter_errors)

    with transaction.atomic():
        save_parameters(run, parameters)

    run.save()

    ngen_cal_input.ready_to_run(run)

    response = {'message': f'Calibration Job {run.id} updated', 'calibration_run_id': run.id, 'status': run.status.name}

    if parameter_rule_report.has_warnings():
        response["parameter_warnings"] = parameter_rule_report.warnings
    if parameter_rule_report.has_errors():
        response["parameter_errors"] = parameter_rule_report.errors

    response_validator, error_response = validate_response(SaveTuningResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=ValidateTuningTimesRequestSerializer,
    responses={
        200: ValidateTuningTimesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Calculate and validate times from tuning tab"
)
@api_view(['POST'])
@handle_exceptions
def validate_tuning_times(request: Request) -> Response:
    """
    Calculate and validate times set by the input values from the tuning tab
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ValidateTuningTimesRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    time_controls = validator.get('time_controls')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    # The available forcing/observational time range should have been computed
    # and persisted when the tuning tab was loaded.
    if run.time_range_start is None or run.time_range_end is None:
        return ResponseError(
            "The available forcing and observational data range has not been established. "
            "Reload the tuning tab before validating the tuning times."
        )

    error_message, calibration_times, validation_times, time_control_limits = calculate_times_and_limits(run, time_controls)
    if error_message:
        return ResponseError(error_message)

    response = {
        'message': f'Calibration Job {run.id} times validated',
        'calibration_run_id': run.id,
        'status': run.status.name,
        'calibration_times': calibration_times,
        'validation_times': validation_times,
        'time_control_limits': time_control_limits
    }

    response_validator, error_response = validate_response(ValidateTuningTimesResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=UploadUserParameterFile,
    responses={
        200: UserParameterFileUploadResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Allow user to upload a starting parameter file"
)
@api_view(['POST'])
@handle_exceptions
def upload_user_parameters(request: Request) -> Response:
    """
    Allows the user to upload a parameter file for tuning, validating its structure
    and content, and then attaching it to the specified calibration run.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(UploadUserParameterFile, data, context={'request': request})
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    files = request.FILES.getlist('user_parameter_files')
    if not files:
        return ResponseError('No files uploaded under field "user_parameter_files".')

    # Multiple parameter files are supported
    # Track parsed data in an array
    parsed_data = []
    for parameter_file in files:
        parsed_file_data = {
            "name": parameter_file.name,
            "message": None,
            "parameters": None
        }
        try:
            file_contents = parameter_file.read().decode('utf-8')
        except Exception as exc:
            logger.exception('Failed to read/decode uploaded file as UTF-8')
            parsed_file_data['message'] = f'Failed to read file as UTF-8: {exc}'
            parsed_data.append(parsed_file_data)
            continue

        if not file_contents.strip():
            parsed_file_data['message'] = 'Uploaded file is empty.'
            parsed_data.append(parsed_file_data)
            continue

        # Infer the delimiter from the header row. Comma and tab are handled as
        # explicit delimiters; otherwise fall back to whitespace so space-separated
        # files can still be accepted.
        first_line = file_contents.splitlines()[0]

        if ',' in first_line:
            delimiter = ','
            logger.debug("Detected comma delimiter.")
        elif '\t' in first_line:
            delimiter = '\t'
            logger.debug("Detected tab delimiter.")
        else:
            delimiter = r'\s+'
            logger.debug("Detected space delimiter.")

        # Expected columns
        required_columns = ['param', 'min', 'max', 'init', 'model']
        expected_cols = len(required_columns)

        # For CSV/TSV files, perform a strict pre-parse check before pandas reads
        # the file. This catches malformed rows with too many/few fields and gives
        # a clearer line-specific error than pandas usually provides.
        #
        # This is skipped for whitespace-delimited files because csv.reader cannot
        # use a regex delimiter like r'\s+'.
        if delimiter in (',', '\t'):
            import csv
            lines = file_contents.splitlines()

            # Require an exact header match after trimming whitespace. This avoids
            # accepting renamed, reordered, or extra columns accidentally.
            header_cols = [c.strip() for c in next(csv.reader([lines[0]], delimiter=delimiter))]
            if header_cols != required_columns:
                parsed_file_data['message'] = f"Header mismatch. Expected: {required_columns}, Found: {header_cols}"
                parsed_data.append(parsed_file_data)
                continue

            # Check each data row before pandas parsing so we can report the actual
            # offending line and avoid silent column shifting.
            for i, row in enumerate(lines[1:], start=2):  # human line numbers
                cols = next(csv.reader([row], delimiter=delimiter))
                if len(cols) != expected_cols:
                    message = f"Row {i} has {len(cols)} fields; expected {expected_cols}. Offending row: {row}\n"
                    if parsed_file_data['message']:
                        parsed_file_data['message'] += message
                    else:
                        parsed_file_data['message'] = message
            if parsed_file_data['message']:
                parsed_data.append(parsed_file_data)
                continue

        # Parse with pandas after the manual structural checks. The dtype mapping
        # forces numeric columns to be converted immediately, so invalid min/max/init
        # values fail early instead of being carried forward as strings.
        try:
            # Handle file parsing based on detected delimiter
            df = pd.read_csv(
                io.StringIO(file_contents),
                sep=delimiter,
                engine='python',
                skipinitialspace=True,
                dtype={'param': str, 'min': float, 'max': float, 'init': float, 'model': str},
            )
        except pd.errors.ParserError as exc:
            logger.debug(f'Pandas parser error: {exc}')
            parsed_file_data['message'] = f"Could not parse file with detected delimiter: {exc}"
            parsed_data.append(parsed_file_data)
            continue
        except ValueError as exc:
            # Typically raised when dtype conversion fails with informative message
            logger.debug(f'Pandas dtype error: {exc}')
            parsed_file_data['message'] = f"Invalid data types in file: {exc}"
            parsed_data.append(parsed_file_data)
            continue

        # Normalize column names after parsing so headers like " param " are treated
        # as "param".
        df.columns = df.columns.str.strip()

        # Log detected columns for debugging
        logger.debug(f"Detected columns: {df.columns.tolist()}")

        # Confirm that all required columns are present after parsing.
        missing_cols = [col for col in required_columns if col not in df.columns]
        if missing_cols:
            # Log the actual DataFrame to inspect it
            logger.debug("DataFrame content:\n%s", df.head())
            parsed_file_data['message'] = f"Missing required columns: {missing_cols}"
            parsed_data.append(parsed_file_data)
            continue

        # Reject extra columns. Extra columns often indicate a bad delimiter or a row
        # with too many fields, both of which can corrupt the parameter mapping.
        unexpected = [c for c in df.columns if c not in required_columns]
        if unexpected:
            parsed_file_data['message'] = f"Unexpected columns present: {unexpected}. Expected only {required_columns}."
            parsed_data.append(parsed_file_data)
            continue

        # Require at least one parameter row; a header-only file is structurally valid
        # but not useful.
        if df.empty:
            parsed_file_data['message'] = "No data rows found. Provide at least one parameter row."
            parsed_data.append(parsed_file_data)
            continue

        # Re-check numeric fields and return exact line/value details. This protects
        # against edge cases where pandas parsing succeeds but values still become NaN.
        invalid_details: dict[str, list[dict[str, object]]] = {}
        for col in ['min', 'max', 'init']:
            # Re-coerce to catch NaN in case dtype enforcement was bypassed by space sep quirks
            coerced = pd.to_numeric(df[col], errors='coerce')
            bad_mask = pd.isna(coerced)
            if bad_mask.any():
                bad_rows = df[bad_mask]

                # Add 2 because line 1 is the header and DataFrame index 0
                # corresponds to source file line 2.
                invalid_details[col] = [
                    {
                        'line': offset + 2,
                        'param': str(row['param']),
                        'value': row.get(col)
                    }
                    for offset, (_, row) in enumerate(bad_rows.iterrows())
                ]

        if invalid_details:
            logger.debug(f"Invalid numeric values: {invalid_details}")
            parsed_file_data['message'] = f"Invalid numeric values. {invalid_details}"
            parsed_data.append(parsed_file_data)
            continue

        # Validate parameter bounds before returning the parsed data to the UI.
        # Each row must satisfy:
        #   min <= max
        #   min <= init <= max
        range_errors = {}

        bad_minmax_mask = df['min'] > df['max']
        if bad_minmax_mask.any():
            rows = df[bad_minmax_mask]
            range_errors['min_gt_max'] = [
                {
                    'line': offset + 2,
                    'param': str(row['param']),
                    'min': row['min'],
                    'max': row['max']
                }
                for offset, (_, row) in enumerate(rows.iterrows())
            ]

        bad_init_low = df['init'] < df['min']
        if bad_init_low.any():
            rows = df[bad_init_low]
            range_errors.setdefault('init_lt_min', [])
            range_errors['init_lt_min'].extend(
                {
                    'line': offset + 2,
                    'param': str(row['param']),
                    'init': row['init'],
                    'min': row['min']
                }
                for offset, (_, row) in enumerate(rows.iterrows())
            )

        bad_init_high = df['init'] > df['max']
        if bad_init_high.any():
            rows = df[bad_init_high]
            range_errors.setdefault('init_gt_max', [])
            range_errors['init_gt_max'].extend(
                {
                    'line': offset + 2,
                    'param': str(row['param']),
                    'init': row['init'],
                    'max': row['max']
                }
                for offset, (_, row) in enumerate(rows.iterrows())
            )

        if range_errors:
            logger.debug(f"Range validation errors: {range_errors}")
            parsed_file_data['message'] = f"Range validation failed. {range_errors}"
            parsed_data.append(parsed_file_data)
            continue

        logger.debug(f"Parsed DataFrame after stripping and numeric conversion: \n%s", df)

        # Return the parsed parameter rows to the caller. This endpoint validates and
        # echoes the uploaded file contents; it only persists the filename on the run.
        parsed_file_data['message'] = f"Parameter file {parameter_file.name} processed successfully."
        parsed_file_data['parameters'] = df.to_dict(orient='records')
        parsed_data.append(parsed_file_data)

    response = {
        'message': f"{len(parsed_data)} Parameter file{'s' if len(parsed_data) != 1 else ''} processed for Calibration Job {run.id}",
        'calibration_run_id': run.id,
        'parsed_data': parsed_data
    }

    response_validator, error_response = validate_response(UserParameterFileUploadResponse, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: ValidateParametersResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Validate the tuning parameter selection rules"
)
@api_view(['POST'])
@handle_exceptions
def validate_parameters(request: Request) -> Response:
    """
    Validate the selected tuning parameters for a calibration run.

    Applies parameter selection rules (e.g., LSTM/Topoflow requirements) and returns any
    warnings/errors in the same style as validate_formulation_tab.

    :param request: Django REST Framework request with calibration_run_id.
    :return: Response containing optional parameter_warnings and parameter_errors lists.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    # --- Gather what validate_parameter_selection_rules needs ---
    module_names_for_job = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list("module__name", flat=True)
    )

    selected_module_names = set(
        CalibrationParameter.objects
        .filter(calibration_formulation__calibration_run=run, user_selected_for_tuning=True)
        .values_list("calibration_formulation__module__name", flat=True)
        .distinct()
    )

    error_object = ErrorReport()

    validate_parameter_rules(
        module_names_for_job=module_names_for_job,
        selected_module_names=selected_module_names,
        have_LSTM_flag=have_LSTM(run),
        has_any_params=bool(selected_module_names),
        error_object=error_object,
    )

    response: dict[str, object] = {}
    if error_object.has_warnings():
        response["parameter_warnings"] = error_object.warnings
    if error_object.has_errors():
        response["parameter_errors"] = error_object.errors

    response_validator, error_response = validate_response(ValidateParametersResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def validate_simulation_within_range(
        data_start: datetime,
        data_end: datetime,
        simulation_start: datetime,
        simulation_end: datetime,
        job_type: Literal[JobType.CALIBRATION, JobType.VALIDATION]
) -> str | None:
    """
    Validates that the simulation period falls within the available data range.

    :param data_start: The start date of the available data range.
    :param data_end: The end date of the available data range.
    :param simulation_start: The start date of the simulation period.
    :param simulation_end: The end date of the simulation period.
    :param job_type: Specifies whether this is a calibration or validation job (used in the error message).
    :return: An error message if the simulation period is out of bounds; otherwise, None.
    """
    if simulation_start < data_start or simulation_end > data_end:
        return (
            f"{job_type.value.capitalize()} simulation times must be within the intersection of forcing data and "
            f"observational data - {format_datetime(data_start)} to {format_datetime(data_end)}"
        )
    return None


def validate_time_range_against_data(
        run: CalibrationRun,
        calibration_times: dict[str, datetime] | None = None,
        validation_times: dict[str, datetime] | None = None
) -> str | None:
    """
    Ensures that calibration and validation times fall within the observational and forcing data range of the run.

    :param run: CalibrationRun instance.
    :param calibration_times: Dictionary containing calibration start and end times.
    :param validation_times: Dictionary containing validation start and end times.
    :return: Error message if validation fails; otherwise, None.
    """
    if not (run.time_range_start and run.time_range_end):
        return None

    data_start, data_end = run.time_range_start, run.time_range_end

    # Retrieve calibration period from either provided dictionary or `run`
    calibration_start = calibration_times.get('simulation_start_time') if calibration_times else run.calibration_start_period
    calibration_end = calibration_times.get('simulation_end_time') if calibration_times else run.calibration_end_period

    if calibration_start and calibration_end:
        error_message = validate_simulation_within_range(data_start, data_end, calibration_start, calibration_end, JobType.CALIBRATION)
        if error_message:
            return error_message

    # Retrieve validation period from either provided dictionary or `run`
    validation_start = validation_times.get('simulation_start_time') if validation_times else run.validation_start_period
    validation_end = validation_times.get('simulation_end_time') if validation_times else run.validation_end_period

    if validation_start and validation_end:
        return validate_simulation_within_range(data_start, data_end, validation_start, validation_end, JobType.VALIDATION)

    return None


class TimeControls(TypedDict, total=False):
    simulation_start_time: datetime
    warmup_duration: int
    calibration_duration: int
    validation_window_gap: int
    validation_window_after_calibration: bool
    validation_duration: int


def calculate_times_and_limits(
        run: CalibrationRun,
        time_controls: TimeControls
) -> tuple[
    str,
    dict[str, datetime],
    dict[str, datetime],
    dict[str, datetime | int]
]:
    """
    Calculate derived calibration/validation periods from the tuning time controls and
    return the valid UI limits for those controls.

    The controls persisted on the run are:
      - simulation_start_time: the calibration simulation start time, saved as
        calibration_start_period and normalized to midnight.
      - warmup_duration: months between simulation_start_time and the calibration
        evaluation start.
      - calibration_duration: months in the calibration evaluation period.
      - validation_window_gap: months in the gap between calibration and validation periods.
      - validation_window_after_calibration: True when validation follows calibration;
        False when validation precedes calibration.
      - validation_duration: months in the validation evaluation period.

    The remaining calibration/validation start/end times are derived from these
    controls by CalibrationRun properties.

    :param run: CalibrationRun being validated. Requires time_range_start and time_range_end.
    :param time_controls: UI time controls to validate.
    :return: Tuple containing error messages, calibration times, validation times,
             and UI control limits.
    """
    data_start = run.time_range_start
    data_end = run.time_range_end

    if data_start is None or data_end is None:
        return (
            "The available forcing and observational data range has not been established.",
            {},
            {},
            {}
        )

    simulation_start_time = time_controls.get('simulation_start_time', data_start)
    warmup_duration = time_controls.get('warmup_duration')
    calibration_duration = time_controls.get('calibration_duration')
    validation_window_gap = time_controls.get('validation_window_gap')
    validation_window_after_calibration = time_controls.get('validation_window_after_calibration', True)
    validation_duration = time_controls.get('validation_duration')

    assert isinstance(simulation_start_time, datetime)
    assert isinstance(warmup_duration, int)
    assert isinstance(calibration_duration, int)
    assert isinstance(validation_window_gap, int)
    assert isinstance(validation_window_after_calibration, bool)
    assert isinstance(validation_duration, int)

    # Normalize UI-selected dates to midnight because durations are whole-month windows.
    simulation_start_time = simulation_start_time.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    # If normalization moved the start before the first available timestamp,
    # advance to midnight on the following day.
    if simulation_start_time < data_start:
        simulation_start_time += relativedelta(days=1)

    # Keep the validated controls synchronized with the normalized value so that
    # save_time_controls() does not restore the original non-midnight timestamp.
    time_controls['simulation_start_time'] = simulation_start_time

    # Set time-control values. CalibrationRun properties derive all other periods.
    run.calibration_start_period = simulation_start_time
    run.warmup_duration = warmup_duration
    run.calibration_duration = calibration_duration
    run.validation_window_gap = validation_window_gap
    run.validation_window_after_calibration = validation_window_after_calibration
    run.validation_duration = validation_duration

    # Read the derived properties once. The model properties are typed as nullable,
    # so validate them before constructing dictionaries that require datetime values.
    calibration_start_period = run.calibration_start_period
    calibration_end_period = run.calibration_end_period
    calibration_eval_start_period = run.calibration_eval_start_period
    calibration_eval_end_period = run.calibration_eval_end_period

    validation_start_period = run.validation_start_period
    validation_end_period = run.validation_end_period
    validation_eval_start_period = run.validation_eval_start_period
    validation_eval_end_period = run.validation_eval_end_period

    if (
            calibration_start_period is None
            or calibration_end_period is None
            or calibration_eval_start_period is None
            or calibration_eval_end_period is None
            or validation_start_period is None
            or validation_end_period is None
            or validation_eval_start_period is None
            or validation_eval_end_period is None
    ):
        return (
            "Unable to calculate all calibration and validation times.",
            {},
            {},
            {}
        )

    calibration_times: dict[str, datetime] = {
        'calibration_start_time': calibration_eval_start_period,
        'calibration_end_time': calibration_eval_end_period,
        'simulation_start_time': calibration_start_period,
        'simulation_end_time': calibration_end_period
    }

    validation_times: dict[str, datetime] = {
        'validation_start_time': validation_eval_start_period,
        'validation_end_time': validation_eval_end_period,
        'simulation_start_time': validation_start_period,
        'simulation_end_time': validation_end_period
    }

    error_messages: list[str] = []

    allowed_range = (
        f"{format_datetime(data_start)} to {format_datetime(data_end)}"
    )

    def add_out_of_range_error(field_label: str, value: datetime) -> None:
        if value < data_start or value > data_end:
            error_messages.append(
                f"{field_label} {format_datetime(value)} falls outside the allowed range "
                f"({allowed_range})."
            )

    add_out_of_range_error(
        "Calibration Simulation Start",
        calibration_start_period
    )
    add_out_of_range_error(
        "Calibration Simulation End",
        calibration_end_period
    )
    add_out_of_range_error(
        "Calibration Start",
        calibration_eval_start_period
    )
    add_out_of_range_error(
        "Calibration End",
        calibration_eval_end_period
    )
    add_out_of_range_error(
        "Validation Simulation Start",
        validation_start_period
    )
    add_out_of_range_error(
        "Validation Simulation End",
        validation_end_period
    )
    add_out_of_range_error(
        "Validation Start",
        validation_eval_start_period
    )
    add_out_of_range_error(
        "Validation End",
        validation_eval_end_period
    )

    # Minimum values are constant.
    warmup_duration_min: int = 0
    calibration_duration_min: int = 1
    validation_window_gap_min: int = 0
    validation_duration_min: int = 1

    # Always allow the user to select any start date within the available data
    # range. The calculated-period checks above report incompatible combinations.
    simulation_start_time_min: datetime = data_start
    simulation_start_time_max: datetime = data_end

    if validation_window_after_calibration:
        # Timeline:
        #
        # simulation start
        #   + warmup
        #   + calibration duration
        #   + validation gap
        #   + validation duration
        #   <= available data end
        #
        # For each maximum, reserve room for all other configured durations.
        warmup_duration_max: int = delta_months(
            simulation_start_time,
            data_end - relativedelta(
                months=calibration_duration
                       + validation_window_gap
                       + validation_duration
            )
        )

        calibration_duration_max: int = delta_months(
            simulation_start_time,
            data_end - relativedelta(
                months=warmup_duration
                       + validation_window_gap
                       + validation_duration
            )
        )

        validation_window_gap_max: int = delta_months(
            simulation_start_time,
            data_end - relativedelta(
                months=warmup_duration
                       + calibration_duration
                       + validation_duration
            )
        )

        validation_duration_max: int = delta_months(
            simulation_start_time,
            data_end - relativedelta(
                months=warmup_duration
                       + calibration_duration
                       + validation_window_gap
            )
        )
    else:
        # Validation precedes calibration.
        #
        # Validation simulation start is:
        #
        #   simulation_start_time
        #       - validation_window_gap
        #       - validation_duration
        #
        # Warmup cancels from this calculation because both simulations use the
        # same warmup duration before their respective evaluation periods.
        warmup_duration_max = delta_months(
            simulation_start_time,
            data_end - relativedelta(months=calibration_duration)
        )

        calibration_duration_max = delta_months(
            simulation_start_time + relativedelta(months=warmup_duration),
            data_end
        )

        validation_window_gap_max = delta_months(
            data_start,
            simulation_start_time - relativedelta(
                months=validation_duration
            )
        )

        validation_duration_max = delta_months(
            data_start,
            simulation_start_time - relativedelta(
                months=validation_window_gap
            )
        )

    time_control_limits: dict[str, datetime | int] = {
        'simulation_start_time_min': simulation_start_time_min,
        'simulation_start_time_max': simulation_start_time_max,
        'warmup_duration_min': warmup_duration_min,
        'warmup_duration_max': warmup_duration_max,
        'calibration_duration_min': calibration_duration_min,
        'calibration_duration_max': calibration_duration_max,
        'validation_window_gap_min': validation_window_gap_min,
        'validation_window_gap_max': validation_window_gap_max,
        'validation_duration_min': validation_duration_min,
        'validation_duration_max': validation_duration_max
    }

    error_message: str = '\n'.join(error_messages)
    return error_message, calibration_times, validation_times, time_control_limits


def save_time_controls(run: CalibrationRun, time_controls: TimeControls) -> str | None:
    """
    Copy validated UI time controls onto the run.

    Only the control values are persisted. The simulation/evaluation end times are
    derived by CalibrationRun properties from calibration_start_period, durations,
    and validation_window_after_calibration.

    :param run: CalibrationRun to update.
    :param time_controls: Validated UI time controls.
    :return: None, or an error message if saving is not possible.
    """
    if not time_controls:
        return None

    run.calibration_start_period = time_controls.get('simulation_start_time')
    run.warmup_duration = time_controls.get('warmup_duration')
    run.calibration_duration = time_controls.get('calibration_duration')
    run.validation_window_gap = time_controls.get('validation_window_gap')
    run.validation_window_after_calibration = time_controls.get('validation_window_after_calibration', True)
    run.validation_duration = time_controls.get('validation_duration')

    return None


def get_full_evaluation_date_range_from_ranges(
        calibration_evaluation_range: tuple[datetime, datetime],
        validation_evaluation_range: tuple[datetime, datetime]
) -> tuple[datetime, datetime]:
    """
    Determines the full evaluation date range by identifying the earliest start time and latest end time
    across both calibration and validation evaluation ranges.

    :param calibration_evaluation_range: Tuple containing calibration evaluation start and end times.
    :param validation_evaluation_range: Tuple containing validation evaluation start and end times.
    :return: A tuple containing the start and end times of the full evaluation range.
    """
    start_date = min(calibration_evaluation_range[0], validation_evaluation_range[0])
    end_date = max(calibration_evaluation_range[1], validation_evaluation_range[1])
    return start_date, end_date


def get_full_evaluation_date_range(
        calibration_eval_start_time: datetime,
        calibration_eval_end_time: datetime,
        validation_eval_start_time: datetime,
        validation_eval_end_time: datetime
) -> tuple[datetime, datetime]:
    """
    Calculates the overall evaluation date range by taking the earliest start time and latest end time
    from both calibration and validation evaluation periods.

    :param calibration_eval_start_time: Start time of the calibration evaluation period.
    :param calibration_eval_end_time: End time of the calibration evaluation period.
    :param validation_eval_start_time: Start time of the validation evaluation period.
    :param validation_eval_end_time: End time of the validation evaluation period.
    :return: A tuple containing the start and end times of the combined evaluation period.
    """
    start_date = min(calibration_eval_start_time, validation_eval_start_time)
    end_date = max(calibration_eval_end_time, validation_eval_end_time)
    return start_date, end_date


def validate_time_range(
        start_time: datetime | None,
        end_time: datetime | None,
        field_name: str
) -> tuple[str | None, tuple[datetime, datetime] | None]:
    """
    Validates a given time range, ensuring that both start and end times are provided and that the start time
    is not later than the end time.

    :param start_time: The start time of the range.
    :param end_time: The end time of the range.
    :param field_name: The name of the field being validated, used in error messages.
    :return: A tuple where the first element is an error message (or None if valid),
             and the second element is a tuple of valid start and end times (or None if invalid).
    """
    if start_time is None or end_time is None:
        return f'{field_name.capitalize()} requires both start and end times', None

    # Check if start time is earlier than or equal to end time
    if start_time > end_time:
        return (
            f'{field_name.capitalize()} must have a start time earlier than or equal to the end time - '
            f'{format_datetime(start_time)} > {format_datetime(end_time)}'
        ), None

    # If all validations pass, return the valid range
    return None, (start_time, end_time)


def validate_parameter_values(run: CalibrationRun, parameters: list[dict[str, str | float]]) -> tuple[list[str], list[str]]:
    """
    Validate user-specified parameter values against the parameters available for this run.

    Returns (errors, warnings):
      - errors: invalid module names or invalid parameter names for a valid module
      - warnings: initial_value outside [minimum, maximum] when all three values are provided

    :param run: CalibrationRun being validated.
    :param parameters: List of parameter dicts (expects keys: module, name, minimum, maximum, initial_value).
    :return: (error_messages, warning_messages)
    """
    if not parameters:
        return [], []

    # Retrieve all parameters for this run with only the fields we need
    existing_parameters = list(
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run
        ).values('name', 'calibration_formulation__module_id')
    )

    # Get cached modules keyed by ID
    modules_by_id = get_cached_modules_by_id()

    # Build lookup dict: (module_name, parameter_name) → CalibrationParameter (as dict)
    parameter_lookup = {
        (modules_by_id[p['calibration_formulation__module_id']].name, p['name']): p
        for p in existing_parameters
    }

    # Validate provided parameters
    invalid_parameters = []
    invalid_modules = []
    value_out_of_bounds = []

    # Validate each provided parameter
    for p in parameters:
        module_name = p['module']
        param_name = p['name']

        if not isinstance(module_name, str) or not isinstance(param_name, str):
            continue

        key = (module_name, param_name)

        if key not in parameter_lookup:
            # Check if module is valid
            if get_cached_module_by_name(module_name):
                invalid_parameters.append(key)
            else:
                invalid_modules.append(key)
        else:
            min_val_raw = p.get('minimum')
            max_val_raw = p.get('maximum')
            initial_raw = p.get('initial_value')

            if min_val_raw is not None and max_val_raw is not None and initial_raw is not None:
                min_val = float(min_val_raw)
                max_val = float(max_val_raw)
                initial = float(initial_raw)

                # Only check range if all values are provided
                if not (min_val <= initial <= max_val):
                    msg = (
                        f"Initial value {initial} for parameter '{param_name}' in module '{module_name}' "
                        f"is outside the range [{min_val}, {max_val}]"
                    )
                    logger.warning(msg)
                    value_out_of_bounds.append(msg)

    # Construct messages
    error_messages = []
    warning_messages = []

    if invalid_parameters:
        error_messages.extend(
            f"Invalid parameter '{name}' for module '{module}'"
            for module, name in invalid_parameters
        )
    if invalid_modules:
        error_messages.extend(
            f"Invalid module '{module}' for parameter '{name}'"
            for module, name in invalid_modules
        )
    if value_out_of_bounds:
        for m in value_out_of_bounds:
            logger.warning(m)
        warning_messages.extend(value_out_of_bounds)

    return error_messages, warning_messages


def save_parameters(run: CalibrationRun, parameters: list[dict[str, str | float]], allow_nulls: bool = False) -> None:
    """
    Saves or updates calibration parameters for a given calibration run.
    Turns off user_selected_for_tuning for parameters not in the new list.

    :param run: The calibration run being updated.
    :param parameters: A list of dictionaries containing parameter details.
    :param allow_nulls: Determines how missing values are handled:
        - If `allow_nulls` is False (default), user-provided values override the Data Services defaults,
          even if some values are missing.
        - If `allow_nulls` is True, user-provided values override the defaults only if they are not None,
          allowing missing values to retain their defaults.
    """
    # Handle the case where the user clears all parameters
    if not parameters:
        # If no parameters are provided, turn off all user_selected_for_tuning flags
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run,
            user_selected_for_tuning=True
        ).update(user_selected_for_tuning=False)
        return

    # Fetch all parameters for this run efficiently
    existing_parameters = list(
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run
        ).values(
            'id', 'name', 'minimum', 'maximum', 'initial_value',
            'user_selected_for_tuning', 'calibration_formulation__module_id'
        )
    )

    modules_by_id = get_cached_modules_by_id()

    # Build lookup keyed by (module_name, parameter_name)
    parameter_lookup = {
        (modules_by_id[p['calibration_formulation__module_id']].name, p['name']): p
        for p in existing_parameters
    }

    selected_for_tuning = {
        (p['module'], p['name'])
        for p in parameters
    }
    parameters_to_update = []

    # Update existing parameters
    for p in parameters:
        module_name = p['module']
        param_name = p['name']

        if not isinstance(module_name, str) or not isinstance(param_name, str):
            continue

        key = (module_name, param_name)
        existing = parameter_lookup.get(key)
        if not existing:
            continue  # Ignore unknown parameters

        updates = {}
        if allow_nulls:
            if p.get('minimum') is not None:
                updates['minimum'] = p['minimum']
            if p.get('maximum') is not None:
                updates['maximum'] = p['maximum']
            if p.get('initial_value') is not None:
                updates['initial_value'] = p['initial_value']
        else:
            updates['minimum'] = p.get('minimum')
            updates['maximum'] = p.get('maximum')
            updates['initial_value'] = p.get('initial_value')

        if updates:
            updates['user_selected_for_tuning'] = True
            updates['id'] = existing['id']
            parameters_to_update.append(updates)

    # Bulk update selected parameters
    if parameters_to_update:
        CalibrationParameter.objects.bulk_update(
            [
                CalibrationParameter(
                    id=p['id'],
                    minimum=p.get('minimum'),
                    maximum=p.get('maximum'),
                    initial_value=p.get('initial_value'),
                    user_selected_for_tuning=True,
                )
                for p in parameters_to_update
            ],
            ['minimum', 'maximum', 'initial_value', 'user_selected_for_tuning']
        )

    # Turn off tuning flag for unselected parameters
    unselected_ids = [
        p['id']
        for p in existing_parameters
        if (modules_by_id[p['calibration_formulation__module_id']].name, p['name']) not in selected_for_tuning
           and p['user_selected_for_tuning']
    ]
    if unselected_ids:
        CalibrationParameter.objects.filter(id__in=unselected_ids).update(user_selected_for_tuning=False)


def _as_local_path(path: str) -> str:
    """
    Convert a file:// URL into a local filesystem path.
    For example: file:///ngencerf/data/file.csv -> /ngencerf/data/file.csv
    Leaves non-file URLs unchanged.
    """
    if path.startswith("file://"):
        return urlparse(path).path
    return path


# TODO This is only used to read Forcing files from S3.  We can get rid of this once we use BMI forcing.  We can also get rid of localize_to_path
def get_csv_daterange(path: str) -> DateTimeRange:
    """
    Reads a CSV file (local or cloud) that is assumed to be sorted by date/time and efficiently determines
    the min and max date values from the first column. Uses caching for remote files so that later operations
    (e.g., copying/subsetting) can reuse the same local file without re-downloading.

    :param path: The file path or cloud URL to the CSV file.
    :return: DateTimeRange representing the min and max datetime values from the file.
    :raises CerfException: If the file does not exist, contains invalid datetime values, or encounters a read error.
    """
    try:
        # Always cache remote files, so subsequent uses don't re-download
        with cloud_util.localize_to_path(path, enable_cache=True, suffix=".csv") as (orig, local_path):
            local_path = _as_local_path(local_path)  # ✅ ensure usable by os.path and open()

            if not os.path.exists(local_path):
                raise CerfException(f"File {path} does not exist")

            # Read first data row (skip header)
            with open(local_path, "r", encoding="utf-8") as f:
                _ = f.readline()  # skip header
                first_line = f.readline()
            if not first_line:
                raise CerfException(f"File {path} does not contain data rows")
            first_time = pd.to_datetime(first_line.strip().split(',', 1)[0], errors="coerce")

            # Read last line efficiently
            try:
                with open(local_path, "rb") as f:
                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b"\n":
                        f.seek(-2, os.SEEK_CUR)
                    last_line = f.readline().decode("utf-8").strip()
            except OSError:
                # For very small files, fall back to reading all lines
                with open(local_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                    if len(lines) < 2:
                        raise CerfException(f"File {path} does not contain data rows")
                    last_line = lines[-1].strip()

            last_time = pd.to_datetime(last_line.split(",", 1)[0], errors="coerce")

            if pd.isna(first_time) or pd.isna(last_time):
                raise CerfException(f"Invalid datetime values found in {path}")

            # Ensure timestamps are UTC
            return DateTimeRange(
                first_time.replace(tzinfo=timezone.utc),
                last_time.replace(tzinfo=timezone.utc),
            )

    except Exception as e:
        logger.error(f"Error while processing file {path}: {e}")
        raise CerfException(f"Error reading file {path}: {e}")


def get_date_range_intersection(run: CalibrationRun) -> DateTimeRange | None:
    """
    Calculates the intersection of date ranges between observational and forcing data.

    :param run: Calibration Run.
    :return: DateTimeRange representing the overlapping period, or None if no overlap.
    """
    # Calculate the date range for the observational data
    obs_range = get_observational_date_range_from_data_services(run)
    logger.debug(f"obs_range: {obs_range}")

    forcing_range = get_forcing_date_range(run)
    logger.debug(f"forcing_range: {forcing_range}")

    # Compute the intersection of the two ranges
    if obs_range and forcing_range:
        obs_start = obs_range.start_datetime
        obs_end = obs_range.end_datetime
        forcing_start = forcing_range.start_datetime
        forcing_end = forcing_range.end_datetime

        assert obs_start is not None and obs_end is not None
        assert forcing_start is not None and forcing_end is not None

        start_time = max(obs_start, forcing_start)
        end_time = min(obs_end, forcing_end)

        if start_time <= end_time:
            return DateTimeRange(start_time, end_time)

    return None


def get_forcing_date_range(run: CalibrationRun) -> DateTimeRange | None:
    """
    Return the configured forcing date range for the run's forcing source and domain.

    AORC currently only supports CONUS and uses the dynamically resolved AORC
    CONUS range.

    NWM Retrospective supports multiple domains, each with its own configured
    date range.
    """
    if run.forcing_source is None or run.gage is None or run.gage.domain is None:
        return None

    forcing_source_name = run.forcing_source.name
    domain_name = run.gage.domain.name

    if forcing_source_name == ForcingSourceEnum.AORC.value:
        if domain_name == "CONUS":
            return settings.FORCING_AORC_CONUS_BMI_DATE_RANGE
        return None

    if forcing_source_name == ForcingSourceEnum.NWM_RETROSPECTIVE.value:
        nwm_ranges = {
            "CONUS": settings.FORCING_NWM_RETROSPECTIVE_CONUS_BMI_DATE_RANGE,
            "Hawaii": settings.FORCING_NWM_RETROSPECTIVE_HAWAII_BMI_DATE_RANGE,
            "Alaska": settings.FORCING_NWM_RETROSPECTIVE_ALASKA_BMI_DATE_RANGE,
            "Puerto_Rico": settings.FORCING_NWM_RETROSPECTIVE_PUERTO_RICO_BMI_DATE_RANGE,
        }

        forcing_range = nwm_ranges.get(domain_name)

        logger.info(
            "Using NWM Retrospective forcing date range for domain %s: %s",
            domain_name,
            forcing_range,
        )

        return forcing_range

    return None


def validate_parameter_rules(
        *,
        module_names_for_job: set[str],
        selected_module_names: set[str],
        have_LSTM_flag: bool,
        has_any_params: bool,
        error_object: ErrorReport
) -> None:
    """
    Enforce parameter selection rules based on the modules included in the job.

    Rules:
      - If LSTM is present: no parameters may be selected.
      - If Topoflow-Glacier is present: at least one Topoflow-Glacier parameter must be selected.
        If any other non-Topoflow modules are present: at least one non-Topoflow parameter must also be selected.
      - Otherwise: at least one parameter must be selected.

    :param module_names_for_job: Module names included in the job.
    :param selected_module_names: Module names with >= 1 selected parameter.
    :param have_LSTM_flag: True if the job includes LSTM.
    :param has_any_params: True if any parameters are selected.
    :param error_object: ErrorReport to receive warnings/errors.
    :return: None.
    """
    TOPOFLOW = "Topoflow-Glacier"

    has_topoflow = TOPOFLOW in module_names_for_job
    has_non_topoflow_modules = any(name != TOPOFLOW for name in module_names_for_job)

    # Parameter selection rules:
    # - If LSTM is present: MUST have zero selected parameters.
    # - If Topoflow-Glacier is in the job: must select >=1 Topoflow-Glacier parameter.
    #   If any other modules are also in the job: must also select >=1 non-Topoflow-Glacier parameter.
    # - Otherwise (no LSTM, no Topoflow-Glacier): must select >=1 parameter overall.
    if have_LSTM_flag:
        if has_any_params:
            error_object.add_warning("LSTM jobs must not specify any calibration parameters")
        return

    # No params selected at all.
    if not has_any_params:
        if has_topoflow:
            if has_non_topoflow_modules:
                error_object.add_warning(
                    "At least one Topoflow-Glacier parameter and at least one non-Topoflow-Glacier parameter must be specified"
                )
            else:
                error_object.add_warning("At least one Topoflow-Glacier parameter must be specified")
        else:
            error_object.add_warning("At least one parameter must be specified")
        return

    # Parameters selected — ensure they cover required module categories
    has_topoflow_param = TOPOFLOW in selected_module_names
    has_non_topoflow_param = any(name != TOPOFLOW for name in selected_module_names)

    if has_topoflow and not has_topoflow_param:
        error_object.add_warning("At least one Topoflow-Glacier parameter must be specified")

    if has_topoflow and has_non_topoflow_modules and not has_non_topoflow_param:
        error_object.add_warning("At least one non-Topoflow-Glacier parameter must be specified")


def delta_months(date1, date2):
    delta = relativedelta(date2, date1)
    return (delta.years * 12) + delta.months
