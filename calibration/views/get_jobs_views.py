import json
import logging
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q, Exists, OuterRef
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import GetValidationJobsScope, StatusEnum, ValidationType
from calibration.models import CalibrationFormulation, CalibrationRun, CalibrationStopCriteria, \
    ValidationRun, IterationParameter, ForecastRun, VerificationRun
from calibration.util.calibration_validators import EmptySerializer, GetCalibrationJobsForEvaluationResponseSerializer, ErrorResponseSerializer, \
    GetCalibrationJobsResponseSerializer, GetCalibrationJobsRequestSerializer, CalibrationRunSerializer, GetValidationJobsResponseSerializer, \
    GetForecastJobsResponseSerializer, GetVerificationJobsResponseSerializer, PaginationSerializer
from calibration.views.calibration_evaluation_views import downloadable_statuses
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, truncate_large_fields, get_calibration_run, \
    get_user_email, get_elapsed_str, readonly_transaction

logger = logging.getLogger(__name__)

User = get_user_model()

"""
Instructions for UI developer.  Will be deleted once we have this implemented

UI Behavior Requirements for Job List (Calibration + Forecast Screens)

All filter and sorting changes trigger a fresh API call:

- When the user changes any filter (gage_id, status, modules, include_archived, etc.),
  immediately request data with offset = 0.
- When the user changes sorting (either field or direction),
  immediately request data with offset = 0.
- No “Apply” button is required; auto-submit on change is acceptable.

Pagination behavior:

- Next/Previous page or page number click updates offset
  to the appropriate value (e.g., offset = pageIndex * limit)
- Changing limit resets offset to 0
- Changing filters or sort always resets offset to 0

Request payload shape:

Use this shape for every request (omit keys you’re not using):

    limit: integer page size (e.g., 25)
    offset: integer row offset (0-based)
    filters: object with any of:
        gage_id: string
        status: array of validated status names (e.g. ["Done","Failed"])
        modules: array of module names
        include_archived: boolean (false by default on backend)
    sort: object with:
        field: one of the server-allowed fields
        direction: "asc" or "desc"

Do NOT send empty/defaults.
If there are no filters, omit "filters".
If there is no sort, omit "sort".

Example request (as plain text):

    { 
    "limit": 25, "offset": 0,
      "filters": { "gage_id": "01544887", "status": ["Done","Failed"],
                   "modules": ["NWMv3","ParFlow"], "include_archived": false },
      "sort": { "field": "submit_date", "direction": "asc" }
    }

Minimal example:

    { "limit": 25, "offset": 0 }

Allowed sort fields (must match what backend supports):

- Calibration: gage_id, user_formulation_name, submit_date, created_at,
  job_genesis, status, calibration_start_period, calibration_end_period, stop_criteria
- Forecast: gage_id, submit_date, cycle_date, configuration, domain_name,
  created_at, status

Default sort (when not provided): by -id on the server.

Client UI Interaction:

- Single “Sort by” select for field, plus a toggle for asc/desc
  (default asc when field is first selected).
- gage_id: free-text input with debounce (250–400 ms). Pressing Enter or blur
  immediately triggers request (offset = 0). Include a clear/reset button.
- status: multi-select with backend-approved label values.
- modules: multi-select from server-provided list.
- include_archived: checkbox (unchecked by default).
- All filter changes immediately fetch data with offset = 0.
- Debounce text filters, but not dropdowns or checkboxes.

UX expectations:

- Show loading indicator while fetching. Disable pagination controls during load.
- Always display total_count from server.
- Show “Showing 26–50 of 137” style summary.
- Keep filters + sort visibly summarized.
- URL query string SHOULD reflect current limit/offset/filters/sort
  (optional but recommended).

Error / Empty States:

- If total_count = 0, show “No jobs match your filters. Clear filters?”.
- If API error, show toast/banner, allow retry, keep last good data visible.
- Ensure ARIA + keyboard accessibility.

Performance guidance:

- Don’t send the request if nothing actually changed.
- Optimistically flip sort indicators during user interaction.
- Optionally cache results by a hash of {limit, offset, filters, sort}.

This ensures consistent behavior: any filter or sort change resets offset = 0
and immediately fetches new server data. Pagination only manipulates offset.
"""


@extend_schema(
    request=PaginationSerializer,
    responses={
        200: GetCalibrationJobsForEvaluationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get all Calibration jobs for Evaluation"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs_for_evaluation(request: Request) -> Response:
    """
    Retrieves calibration jobs that are DONE, FAILED, CANCELLED, or SERVER_ERROR for evaluation purposes.

    :param request: The HTTP request object.
    :return: JSON response with a list of calibration jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(PaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")

    jobs, total_count = get_jobs(
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR],
        include_validation_data=GetValidationJobsScope.STATUS,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(GetCalibrationJobsForEvaluationResponseSerializer, response, fields_to_truncate=['jobs'])
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=PaginationSerializer,
    responses={
        200: GetCalibrationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get all calibration jobs for Forecast"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs_for_forecast(request: Request) -> Response:
    """
    Returns only DONE calibration jobs for forecasting purposes.

    :param request: The HTTP request object.
    :return: JSON response with a list of calibration jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(PaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")

    jobs, total_count = get_jobs(
        request.user,
        run_status=[StatusEnum.DONE],
        include_validation_data=GetValidationJobsScope.DONE,
        include_archived=include_archived,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(GetCalibrationJobsResponseSerializer, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=PaginationSerializer,
    responses={
        200: GetCalibrationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },

    description="Get all calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs(request):
    """
    Return all calibration jobs, including archived ones.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(PaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")

    jobs, total_count = get_jobs(
        request.user,
        run_status=list(StatusEnum),
        include_validation_data=GetValidationJobsScope.STATUS,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(GetCalibrationJobsResponseSerializer, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def apply_calibration_filters(query: Q, filters: dict) -> Q:
    """Apply optional calibration-related filters to a base Q expression."""
    if not filters:
        return query

    if "gage_id" in filters and filters["gage_id"]:
        query &= Q(gage__gage_id=filters["gage_id"])

    if "status" in filters and filters["status"]:
        query &= Q(status__in=[StatusEnum.from_name(s).db_instance for s in filters["status"]])

    if "modules" in filters and filters["modules"]:
        modules_by_name = {m.name: m.id for m in get_cached_modules_by_id().values()}
        module_ids = [modules_by_name[name] for name in filters["modules"] if name in modules_by_name]
        if module_ids:
            query &= Q(calibrationformulation__module_id__in=module_ids)

    if "include_archived" in filters and not filters["include_archived"]:
        query &= Q(is_archived=False)

    return query


# Map client sort field → actual ORM values(field)
CALIBRATION_SORT_FIELD_MAP = {
    "gage_id": "gage__gage_id",
    "user_formulation_name": "user_formulation_name",
    "submit_date": "submit_date",
    "created_at": "created_at",
    "job_genesis": "job_genesis",
    "status": "status__name",
    "calibration_start_period": "calibration_start_period",
    "calibration_end_period": "calibration_end_period",
    "stop_criteria": "calibrationstopcriteria__value",
}

# Map client sort field → actual ORM values(field)
FORECAST_SORT_FIELD_MAP = {
    "gage_id": "calibration_run__gage__gage_id",
    "submit_date": "submit_date",
    "cycle_date": "cycle_date",
    "configuration": "configuration__name",
    "domain_name": "configuration__domain__name",
    "created_at": "created_at",
    "status": "status__name",
}


def get_jobs(
        user: User,
        run_status: list[StatusEnum] = None,
        include_validation_data: GetValidationJobsScope = None,
        include_stop_criteria: bool = False,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None  # ← NEW
) -> tuple[list[dict[str, Any]], int]:
    """
    Retrieves calibration jobs for the given user with optional status filtering,
    validation data inclusion, server-side filters, sorting, and optional pagination.

    Runs in READ ONLY mode to reduce contention.

    :param user: The user for whom the jobs are being fetched.
    :param run_status: Optional list of StatusEnum values to filter jobs (e.g., DONE, FAILED).
    :param include_validation_data: Determines the level of validation data to include:
        - 'ids': Includes validation_run_ids and their count in validation_runs.
        - 'status': Includes validation status details.
        - 'done': Filters to only include jobs where both valid_control and valid_best are DONE.
    :param include_archived: Whether to include archived jobs in the queryset.
    :param include_stop_criteria: Whether to include stop_criteria in the queryset.
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": "created_at", "direction": "asc" or "desc" }.
    :return: Tuple (results, total_count). total_count reflects total rows BEFORE pagination.
    """
    filters = filters or {}

    # Default ordering
    order_by = "-id"

    # Apply requested sort if valid
    if sort and "field" in sort and sort["field"] in CALIBRATION_SORT_FIELD_MAP:
        orm_field = CALIBRATION_SORT_FIELD_MAP[sort["field"]]
        direction = sort.get("direction", "asc").lower()
        order_by = orm_field if direction == "asc" else f"-{orm_field}"

    with readonly_transaction():
        # Base query: filter jobs for the user
        query = Q(owner=user)

        # If a specific status list is provided, filter by those statuses
        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        base_qs = CalibrationRun.objects.filter(query)

        # Only include jobs where both Valid_control and Valid_best jobs are DONE
        if include_validation_data == GetValidationJobsScope.DONE:
            base_qs = base_qs.annotate(
                has_valid_control_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef('pk'),
                        validation_type=ValidationType.VALID_CONTROL.value,
                        status=StatusEnum.DONE.db_instance
                    )
                ),
                has_valid_best_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef('pk'),
                        validation_type=ValidationType.VALID_BEST.value,
                        status=StatusEnum.DONE.db_instance
                    )
                )
            ).filter(
                has_valid_control_done=True,
                has_valid_best_done=True
            )

        total_count = base_qs.count()

        # Base query for CalibrationRun (dict results, lighter than ORM instances)
        calibration_runs_qs = (
            base_qs
            .order_by('-id')
            .values(
                "id", "gage__gage_id", "gage__domain__name", "submit_date", "updated_at", "user_formulation_name",
                "calibration_start_period", "calibration_end_period",
                "status__name", "job_genesis", "created_at",
                "objective_function__name", "optimization__name",
                "is_archived", "is_locked"
            )
        )

        # ───────────────────────────────────────
        # Apply pagination ONLY if limit provided
        # ───────────────────────────────────────
        if limit:
            calibration_runs_qs = calibration_runs_qs[offset: offset + limit]
        # ───────────────────────────────────────

        calibration_runs = list(calibration_runs_qs)
        run_ids = [r["id"] for r in calibration_runs]

        # Fetch formulations separately and map them to run IDs
        formulations_qs = (
            CalibrationFormulation.objects
            .filter(calibration_run_id__in=run_ids)
            .select_related("module")
            .order_by('-id')
            .values_list("calibration_run_id", "module__name")
        )

        formulations_map: dict[int, list[str]] = {}
        for run_id, module_name in formulations_qs:
            formulations_map.setdefault(run_id, []).append(module_name)

        # Preload validation runs if requested
        validations_map: dict[int, list] = {}
        if include_validation_data in [GetValidationJobsScope.IDS, GetValidationJobsScope.STATUS]:
            validation_filter = Q()  # default to "no filter"
            if include_validation_data == GetValidationJobsScope.IDS:
                # Exclude VALID_CONTROL for IDS
                validation_filter = ~Q(validation_type=ValidationType.VALID_CONTROL.value)

            validations_qs = (
                ValidationRun.objects
                .filter(calibration_run_id__in=run_ids)
                .filter(validation_filter)
                .select_related("status")
                .order_by('-id')
                .values("id", "calibration_run_id", "validation_type", "status__name")
            )

            for v in validations_qs:
                validations_map.setdefault(v["calibration_run_id"], []).append(v)

        # Preload stop criteria if requested
        stop_criteria_map: dict[int, str] = {}
        if include_stop_criteria:
            stop_qs = (
                CalibrationStopCriteria.objects
                .filter(calibration_run_id__in=run_ids)
                .values("calibration_run_id", "value")
            )
            stop_criteria_map = {sc["calibration_run_id"]: sc["value"] for sc in stop_qs}

        results = []
        for run in calibration_runs:
            run_id = run["id"]
            result = {
                'calibration_run_id': run_id,
                'gage_id': run['gage__gage_id'],
                'domain_name': run['gage__domain__name'],
                'status': run['status__name'],
                'objective_function': run.get('objective_function__name'),  # may be None
                'optimization_algorithm': run.get('optimization__name'),  # may be None
                'is_archived': run['is_archived'],
                'is_locked': run['is_locked'],
                'submit_date': run['submit_date'],
                'formulation_name': run['user_formulation_name'],
                'calibration_start_period': run['calibration_start_period'],
                'calibration_end_period': run['calibration_end_period'],
                'job_genesis': run['job_genesis'],
                'created_at': run['created_at'],
                'last_updated_on': run['updated_at'],
                'modules': formulations_map.get(run_id, []),
                'is_downloadable': StatusEnum.from_name(run['status__name']) in downloadable_statuses,
            }

            # Include validation IDs and count if requested
            if include_validation_data == GetValidationJobsScope.IDS:
                ids = [v["id"] for v in validations_map.get(run_id, [])]
                result['validation_run_ids'] = ids
                result['validation_runs'] = len(ids)

            # Include detailed validation status if requested
            if include_validation_data == GetValidationJobsScope.STATUS:
                result['validations'] = [
                    {
                        "validation_run_id": v["id"],
                        "validation_type": v["validation_type"],
                        "status": v["status__name"],
                    }
                    for v in validations_map.get(run_id, [])
                ]
                result['validation_run_ids'] = [v["id"] for v in validations_map.get(run_id, [])]
                result['validation_runs'] = len(validations_map.get(run_id, []))

            # Include stop criteria if requested
            if include_stop_criteria:
                result['stop_criteria'] = stop_criteria_map.get(run_id)

            results.append(result)

        return results, total_count


def get_validation_jobs_internal(
        calibration_run_id: int,
        detail_level: GetValidationJobsScope = GetValidationJobsScope.IDS,
) -> list[dict[str, Any]] | list[int]:
    """
    Retrieves validation jobs for a specific calibration job.

    :param calibration_run_id: ID of the calibration run to fetch validation jobs for.
    :param detail_level: Determines the level of detail in the response:
        - IDS: handled by get_jobs (this function returns []).
        - STATUS: handled by get_jobs (this function returns []).
        - DETAILS: returns full validation job details including parameters.
    :return: [] unless detail_level == DETAILS, in which case a list of detailed dicts.
    """
    # Keep batch logic only for DETAILS; IDS/STATUS are already handled in get_jobs
    if detail_level != GetValidationJobsScope.DETAILS:
        return []

    with readonly_transaction():

        # 1) Fetch all validation runs for this calibration run
        validation_runs = list(
            ValidationRun.objects
            .filter(calibration_run_id=calibration_run_id)
            .exclude(validation_type=ValidationType.VALID_CONTROL.value)
            .select_related("status", "iteration")
        )

        if not validation_runs:
            return []

        # Collect iteration IDs
        iteration_ids = [v.iteration_id for v in validation_runs if v.iteration_id]

        # 2) Preload iteration parameters in one query
        iteration_params_qs = IterationParameter.objects.filter(iteration_id__in=iteration_ids).values(
            "iteration_id", "calibration_parameter__name", "tuned_value"
        )

        params_map: dict[int, list[dict[str, Any]]] = {}
        for p in iteration_params_qs:
            params_map.setdefault(p["iteration_id"], []).append({
                "name": p["calibration_parameter__name"],
                "value": p["tuned_value"]
            })

        # 3) Preload all "best params" for the calibration run in one query
        best_params_qs = IterationParameter.objects.filter(
            iteration__calibration_run_id=calibration_run_id,
            iteration__best_params=True
        ).values("calibration_parameter__name", "tuned_value")

        best_params = [
            {"name": bp["calibration_parameter__name"], "value": bp["tuned_value"]}
            for bp in best_params_qs
        ]

        # Build result
        results: list[dict[str, Any]] = []
        for job in validation_runs:
            if job.validation_type == ValidationType.VALID_BEST.value:
                parameters = best_params
            else:
                parameters = params_map.get(job.iteration_id, [])

            results.append({
                "validation_run_id": job.id,
                "submit_date": job.submit_date,
                "status": job.status.name,
                "validation_type": job.validation_type,
                "iteration_num": job.iteration_num if job.iteration else None,
                "parameters": parameters,
                "best": job.validation_type == ValidationType.VALID_BEST.value,
            })

        return results


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetValidationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve validation jobs along with their starting parameter values"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_validation_jobs(request: Request) -> Response:
    """
    Retrieves validation jobs for a specific calibration run along with initial parameter values.
    This endpoint itself doesn’t need a read-only wrapper, because
    `get_validation_jobs_internal` already enforces READ ONLY.

    - Handles user authentication and request validation.
    - Fetches validation jobs linked to a calibration run.
    - Constructs and validates the response with serialized data.

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response containing validation jobs or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    # Retrieve validation jobs using internal helper - already runs in READ ONLY mode
    validation_jobs = get_validation_jobs_internal(calibration_run_id, detail_level=GetValidationJobsScope.DETAILS)

    response = {'validation_jobs': validation_jobs}
    response_validator, error_response = validate_response(GetValidationJobsResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def get_forecast_jobs_internal(
        user: User,
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None  # NEW
) -> tuple[list[dict[str, Any]], int]:
    """
    Internal helper to retrieve forecast jobs for a user (READ ONLY), with optional filtering,
    sorting, and pagination.

    :param user: Owner of the jobs to fetch.
    :param run_status: Optional list of StatusEnum values to filter on (e.g., DONE).
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (reusing calibration filters, e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": one of FORECAST_SORT_FIELD_MAP keys, "direction": "asc" or "desc" }.
    :return: Tuple (results, total_count). total_count reflects the total number of matching rows
             BEFORE pagination is applied.
    """
    filters = filters or {}

    # Default ordering
    order_by = "-id"

    # Apply forecast-specific sorting
    if sort:
        field = sort.get("field")
        direction = sort.get("direction", "").lower()
        if field not in FORECAST_SORT_FIELD_MAP or direction not in ("asc", "desc"):
            raise ValueError(f"Invalid sort field or direction: {sort}")
        orm_field = FORECAST_SORT_FIELD_MAP[field]
        order_by = orm_field if direction == "asc" else f"-{orm_field}"

    query = Q(calibration_run__owner=user)
    query = apply_calibration_filters(query, filters)

    if run_status:
        query &= Q(status_id__in=[s.db_instance.id for s in run_status])

    total_count = ForecastRun.objects.filter(query).count()

    with readonly_transaction():
        rows = list(
            ForecastRun.objects
            .filter(query)
            .order_by(order_by)
            .values(
                'id',
                'calibration_run_id',
                'configuration__name',
                'configuration__domain__name',
                'cycle_date',
                'submit_date',
                'calibration_run__gage__gage_id',
                'status__name',
                'cold_start_run__cold_start_date',
                'cold_start_run__status__name',
                'cold_start_run__submit_date',
            )
        )

        # ──────────────────────────────────────────
        # Apply pagination ONLY if limit provided
        # ──────────────────────────────────────────
        if limit:
            rows = rows[offset: offset + limit]
        # ──────────────────────────────────────────

    # Normalize keys expected by the API response/serializer
    for f in rows:
        f['forecast_run_id'] = f.pop('id')
        f['configuration'] = f.pop('configuration__name')
        f['domain_name'] = f.pop('configuration__domain__name')
        f['gage_id'] = f.pop('calibration_run__gage__gage_id')
        f['forecast_status'] = f.pop('status__name')
        f['cycle_date'] = f.pop('cycle_date')
        f['submit_date'] = f.pop('submit_date')

        cold_date = f.pop('cold_start_run__cold_start_date')
        cold_status = f.pop('cold_start_run__status__name')
        cold_submit = f.pop('cold_start_run__submit_date')

        # Only include nested cold_start object if data exists
        if cold_date or cold_status:
            f['cold_start'] = {
                'cold_start_date': cold_date,
                'cold_start_status': cold_status,
                'cold_start_submit_date': cold_submit
            }
        # else: omit cold_start entirely

    return rows, total_count


@extend_schema(
    request=PaginationSerializer,
    responses={
        200: GetForecastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get forecast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_forecast_jobs(request: Request) -> Response:
    """
    Retrieves all forecast jobs for a user
    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response with validation jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(PaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")

    forecast_jobs, total_count = get_forecast_jobs_internal(
        request.user,
        run_status=None,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "forecast_jobs": forecast_jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(
        GetForecastJobsResponseSerializer, response,
        fields_to_truncate=['forecast_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=PaginationSerializer,
    responses={
        200: GetForecastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get DONE forecast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_forecast_jobs_for_verification(request: Request) -> Response:
    """
    Retrieves only DONE forecast jobs for the authenticated user (READ ONLY).

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response with validation jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(PaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")

    forecast_jobs, total_count = get_forecast_jobs_internal(
        request.user, run_status=[StatusEnum.DONE],
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "forecast_jobs": forecast_jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(
        GetForecastJobsResponseSerializer, response,
        fields_to_truncate=['forecast_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetVerificationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get verification jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_verification_jobs(request: Request) -> Response:
    """
    Retrieves all verification jobs for a user

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response with validation jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    verification_objects = VerificationRun.objects.filter(owner=request.user)

    verification_jobs = list(
        verification_objects.values('id', 'created_at', 'submit_date', 'status__name', 'forecast_run_id'))

    for v in verification_jobs:
        v['verification_job_id'] = v.pop('id')
        v['status'] = v.pop('status__name')

    response = {'verification_jobs': verification_jobs}
    response_validator, error_response = validate_response(GetVerificationJobsResponseSerializer, response,
                                                           fields_to_truncate=['verification_jobs'], max_length=10)

    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["verification_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)
