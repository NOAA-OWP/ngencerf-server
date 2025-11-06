import json
import logging
from typing import Any, Type

from django.contrib.auth import get_user_model
from django.db.models import Q, Exists, OuterRef, Count
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import GetValidationJobsScope, StatusEnum, ValidationType, CalibrationSortField, ForecastSortField, VerificationSortField
from calibration.models import CalibrationFormulation, CalibrationRun, CalibrationStopCriteria, \
    ValidationRun, IterationParameter, ForecastRun, VerificationRun
from calibration.util.caching import get_cached_modules_by_id
from calibration.util.calibration_validators import GetCalibrationJobsForEvaluationResponseSerializer, ErrorResponseSerializer, \
    GetCalibrationJobsResponseSerializer, CalibrationRunSerializer, GetValidationJobsResponseSerializer, \
    GetForecastJobsResponseSerializer, GetVerificationJobsResponseSerializer, CalibrationPaginationSerializer, \
    ForecastPaginationSerializer, VerificationPaginationSerializer, GetCalibrationJobIDsResponseSerializer
from calibration.views.calibration_evaluation_views import downloadable_statuses
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, truncate_large_fields, get_calibration_run, \
    get_user_email, get_elapsed_str, readonly_transaction

logger = logging.getLogger(__name__)

User = get_user_model()

"""
Instructions for UI developer.  Will be deleted once we have this implemented

UI Behavior Requirements for Job List (Calibration + Forecast Screens)
=====================================================================

All filter and sorting changes trigger a fresh API call:

- When the user changes any filter (gage_id, status, modules, include_archived, etc.),
  immediately request data with offset = 0.
- When the user changes sorting (either field or direction),
  immediately request data with offset = 0.
- No “Apply” button is required; auto-submit on change is acceptable.
  The server is optimized for this pattern — each response should return
  a single page of results almost instantly (well under one second in typical use).


Pagination behavior
-------------------
- Next/Previous page or page number click updates offset
  to the appropriate value (e.g., offset = pageIndex * limit)
- Changing limit resets offset to 0
- Changing filters or sort always resets offset to 0
- Implement anticipatory loading (prefetching):
  When fetching a page (e.g., limit = 25), request and locally cache
  both the previous and next pages relative to the current one.
  This ensures that when the user scrolls forward (Next) or backward (Previous),
  data for those adjacent pages is already available.
  Once the user navigates to a new page, prefetch the next one in that direction.
  This rolling prefetch avoids lag while keeping memory usage predictable.

Request payload shape
---------------------
Use this shape for every request (omit keys you’re not using):

    limit: integer page size (e.g., 25)
    offset: integer row offset (0-based)
    filters: object with any of:
        gage_id: string
        status: array of validated status names (e.g. ["Done", "Failed"])
        module_filter: object with:
            operator: "and" | "or"       (default = "and")
            modules: array of module names
        date_filter: object with:
            operator: "before" | "after" | "between"
            create_date: "YYYY-MM-DD"    # used for 'before' or 'after'
            start_date: "YYYY-MM-DD"     # used for 'between'
            end_date: "YYYY-MM-DD"       # used for 'between'
        id_filter: object with:
            operator: "before" | "after" | "between"
            id: integer                  # used for 'before' or 'after'
            start_id: integer            # used for 'between'
            end_id: integer              # used for 'between'
        include_archived: boolean (false by default on backend)
    sort: object with:
        field: one of the server-allowed fields
        direction: "asc" or "desc"
    ids_only: boolean (false by default; when true, only job IDs are returned)

Do NOT send empty/defaults.
If there are no filters, omit "filters".
If there is no sort, omit "sort".

Example request (as plain text):

    {
        "limit": 25,
        "offset": 0,
        "filters": {
            "gage_id": "01544887",
            "status": ["Done", "Failed"],
            "module_filter": {
                "operator": "and",
                "modules": ["CFE-X", "Noah-OWP-Modular"]
            },
            "date_filter": {
                "operator": "after",
                "create_date": "2025-01-01"
            },
            "id_filter": {
                "operator": "before",
                "id": 500
            },
            "include_archived": false
        },
        "sort": { "field": "submit_date", "direction": "asc" },
        "ids_only": false
    }

Example with date range filter:

    {
      "limit": 25,
      "offset": 0,
      "filters": {
          "date_filter": {
              "operator": "between",
              "start_date": "2025-01-01",
              "end_date": "2025-02-01"
          }
      }
    }

Minimal example:

    { "limit": 25, "offset": 0 }

Allowed sort fields (must match what backend supports):

- Calibration: id, gage_id, user_formulation_name, submit_date, create_date,
  job_genesis, status, period, stop_criteria, validation_runs
- Forecast: id, gage_id, submit_date, create_date, cycle_date, configuration, domain_name, status
- Verification: id, forecast_run_id, submit_date, create_date, status

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
- Implement rolling prefetch for pagination (anticipatory loading):
  Always keep both the previous and next pages of the current page preloaded.
  Replace older cached pages as the user scrolls forward or backward
  to keep memory footprint predictable.

This ensures consistent behavior: any filter or sort change resets offset = 0
and immediately fetches new server data. Pagination manipulates offset only,
while prefetching makes transitions instantaneous.
"""


@extend_schema(
    request=CalibrationPaginationSerializer,
    responses={
        200: OpenApiResponse(
            response={
                "oneOf": [
                    GetCalibrationJobsForEvaluationResponseSerializer,
                    GetCalibrationJobIDsResponseSerializer,
                ]
            },
            description="Full job list or ID-only list depending on ids_only flag"
        ),
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

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    jobs, total_count = get_jobs(
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR],
        include_validation_data=GetValidationJobsScope.STATUS,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    if ids_only:
        serializer_class = GetCalibrationJobIDsResponseSerializer
    else:
        serializer_class = GetCalibrationJobsForEvaluationResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'])
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationPaginationSerializer,
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

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    jobs, total_count = get_jobs(
        request.user,
        run_status=[StatusEnum.DONE],
        include_validation_data=GetValidationJobsScope.DONE,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    if ids_only:
        serializer_class = GetCalibrationJobIDsResponseSerializer
    else:
        serializer_class = GetCalibrationJobsResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationPaginationSerializer,
    responses={
        200: OpenApiResponse(
            response={
                "oneOf": [
                    GetCalibrationJobsResponseSerializer,
                    GetCalibrationJobIDsResponseSerializer,
                ]
            },
            description="Full job list or ID-only list depending on ids_only flag"
        ),
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

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    jobs, total_count = get_jobs(
        request.user,
        run_status=list(StatusEnum),
        include_validation_data=GetValidationJobsScope.STATUS,
        include_stop_criteria=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response = {
        "jobs": jobs,
        "total_count": total_count
    }

    if ids_only:
        serializer_class = GetCalibrationJobIDsResponseSerializer
    else:
        serializer_class = GetCalibrationJobsResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def _normalize_filters_and_sort(filters: dict | None, sort: dict | None) -> tuple[dict | None, dict | None]:
    """
    Normalize and sanitize incoming filter and sort payloads.

    This function removes blank, empty, or null fields from the validated request data
    to ensure consistent query behavior. It also strips out nested filter objects
    (e.g., module_filter, date_filter) if they contain no usable values, and disables
    sorting if the sort field is missing or blank.

    :param filters: Optional dictionary of filter parameters (may include nested objects).
    :param sort: Optional dictionary specifying sorting field and direction.
    :return: Tuple of (normalized_filters, normalized_sort) with blanks stripped out.
             Returns ({}, None) when inputs are invalid or contain only empty values.
    """
    if filters:
        filters = {
            k: v for k, v in filters.items()
            if v not in ("", [], {}, None)
        }

        # handle nested filters
        if "module_filter" in filters and filters["module_filter"]:
            mf = filters["module_filter"]
            if not mf.get("modules"):
                filters.pop("module_filter")

        if "date_filter" in filters and filters["date_filter"]:
            date_filter = filters["date_filter"]
            op = (date_filter.get("operator") or "").lower()

            if op == "between":
                # Require both start and end
                if not date_filter.get("start_date") or not date_filter.get("end_date"):
                    filters.pop("date_filter")
            elif not date_filter.get("operator") or not date_filter.get("create_date"):
                # for 'before' / 'after', require a single value
                filters.pop("date_filter")

        if "id_filter" in filters and filters["id_filter"]:
            id_filter = filters["id_filter"]
            op = (id_filter.get("operator") or "").lower()

            if op == "between":
                # Require both start and end
                if not id_filter.get("start_id") or not id_filter.get("end_id"):
                    filters.pop("id_filter")
            elif not id_filter.get("operator") or id_filter.get("id") is None:
                # for 'before' / 'after', require a single id value
                filters.pop("id_filter")

    if sort and (not sort.get("field") or str(sort.get("field")).strip() == ""):
        sort = None

    return filters, sort


def _apply_shared_filters(
        query: Q, filters: dict, *,
        gage_prefix: str,
        module_prefix: str,
        status_field: str,
        created_field: str,
        archived_field: str = "is_archived"
) -> Q:
    """
    Apply shared filter logic for Calibration, Forecast, and Verification jobs.

    :param query: Base Q object to filter (e.g., ownership constraint).
    :param filters: Dictionary of filters passed by the client.
    :param gage_prefix: ORM prefix path to gage_id (e.g., 'gage__' or 'calibration_run__gage__').
    :param module_prefix: ORM prefix path to module relationship (e.g., 'calibrationformulation__').
    :param status_field: ORM field path for status filtering (e.g., 'status__in').
    :param created_field: ORM field path to the created_at date field.
    :param archived_field: ORM field path to the archive flag field (default 'is_archived').
    :return: Updated Q object with all applicable filters applied.
    """
    if not filters:
        return query

    # ───── Gage filter ─────
    if "gage_id" in filters and filters["gage_id"]:
        query &= Q(**{f"{gage_prefix}gage_id": filters["gage_id"]})

    # ───── Status filter ─────
    if "status" in filters and filters["status"]:
        query &= Q(**{status_field: [StatusEnum.from_name(s).db_instance for s in filters["status"]]})

    # ───── Module filter ─────
    if "module_filter" in filters:
        mf = filters["module_filter"]
        modules = mf.get("modules") or []
        operator = (mf.get("operator") or "and").lower()

        if modules:
            modules_by_name = {m.name: m.id for m in get_cached_modules_by_id().values()}
            module_ids = [modules_by_name[name] for name in modules if name in modules_by_name]

            if module_ids:
                if operator == "and":
                    subquery = (
                        CalibrationFormulation.objects
                        .filter(calibration_run_id=OuterRef(f"{module_prefix.rstrip('__')}id"),
                                module_id__in=module_ids)
                        .values("calibration_run_id")
                        .annotate(match_count=Count("module_id", distinct=True))
                        .filter(match_count=len(module_ids))
                    )
                    query &= Q(Exists(subquery))
                else:
                    query &= Q(**{f"{module_prefix}module_id__in": module_ids})

    # ───── Date filter ─────
    if "date_filter" in filters:
        date_info = filters["date_filter"]
        operator = (date_info.get("operator") or "").lower()

        if operator == "before":
            date_value = date_info.get("create_date")
            if date_value:
                query &= Q(**{f"{created_field}__lt": date_value})

        elif operator == "after":
            date_value = date_info.get("create_date")
            if date_value:
                query &= Q(**{f"{created_field}__gt": date_value})

        elif operator == "between":
            start_date = date_info.get("start_date")
            end_date = date_info.get("end_date")
            if start_date and end_date:
                query &= Q(**{f"{created_field}__gte": start_date, f"{created_field}__lte": end_date})

    # ───── ID filter ─────
    if "id_filter" in filters:
        id_info = filters["id_filter"]
        operator = (id_info.get("operator") or "").lower()

        if operator == "before":
            id_value = id_info.get("id")
            if id_value is not None:
                query &= Q(id__lt=id_value)

        elif operator == "after":
            id_value = id_info.get("id")
            if id_value is not None:
                query &= Q(id__gt=id_value)

        elif operator == "between":
            start_id = id_info.get("start_id")
            end_id = id_info.get("end_id")
            if start_id is not None and end_id is not None:
                query &= Q(id__gte=start_id, id__lte=end_id)

    # ───── Archived toggle ─────
    if "include_archived" in filters and not filters["include_archived"]:
        query &= Q(**{archived_field: False})

    return query


def apply_calibration_filters(query: Q, filters: dict) -> Q:
    """
    Apply standard calibration filters to a CalibrationRun queryset.

    :param query: Base Q object (typically includes ownership constraint).
    :param filters: Dictionary of filter parameters (gage_id, modules, date_filter, etc.).
    :return: Q object with calibration-specific filters applied.
    """
    return _apply_shared_filters(
        query, filters,
        gage_prefix="gage__",
        module_prefix="calibrationformulation__",
        status_field="status__in",
        created_field="created_at",
        archived_field="is_archived"
    )


def apply_forecast_filters(query: Q, filters: dict) -> Q:
    """
    Apply standard forecast filters to a ForecastRun queryset.

    :param query: Base Q object (e.g., Q(calibration_run__owner=user)).
    :param filters: Dictionary of filter parameters (gage_id, modules, date_filter, etc.).
    :return: Q object with forecast-specific filters applied.
    """
    return _apply_shared_filters(
        query, filters,
        gage_prefix="calibration_run__gage__",
        module_prefix="calibration_run__calibrationformulation__",
        status_field="status__in",
        created_field="created_at",
        archived_field="calibration_run__is_archived"
    )


def apply_verification_filters(query: Q, filters: dict) -> Q:
    """
    Apply standard verification filters to a VerificationRun queryset.

    :param query: Base Q object (e.g., Q(forecast_run__calibration_run__owner=user)).
    :param filters: Dictionary of filter parameters (gage_id, modules, date_filter, etc.).
    :return: Q object with verification-specific filters applied.
    """
    return _apply_shared_filters(
        query, filters,
        gage_prefix="forecast_run__calibration_run__gage__",
        module_prefix="forecast_run__calibration_run__calibrationformulation__",
        status_field="status__in",
        created_field="created_at",
        archived_field="forecast_run__calibration_run__is_archived"
    )


def resolve_sort(sort: dict | None, enum_class: Type[CalibrationSortField | ForecastSortField | VerificationSortField]) -> list[str]:
    """
    Convert the validated client-provided sort object into a Django `order_by` argument list.

    - Supports single- and multi-field sorting (e.g., 'period' maps to two ORM fields).
    - If 'direction' is 'desc', all fields are prefixed with '-'.
    - Defaults to ['-id'] if no sort provided.

    This function translates the UI-provided sort configuration into the corresponding ORM field
    name(s) used for ordering querysets. The mapping between user-facing fields and database columns
    is defined by the respective Enum (e.g., CalibrationSortField, ForecastSortField, etc.).

    Assumptions (enforced by upstream serializers and enum validators):
      - `sort["field"]` is a valid string representation of an existing enum member.
      - It can be safely converted via `enum_class.from_name()`.
      - `enum_class` must be a subclass defining `.orm_field` mappings.
      - If `sort` is missing or empty, the function defaults to ['-id'] (descending by primary key).
      - If direction is `"desc"`, a leading "-" is applied to each field.

    Examples:
      >>> resolve_sort({"field": "gage_id", "direction": "asc"}, CalibrationSortField)
      ['gage__gage_id']
      >>> resolve_sort({"field": "period", "direction": "desc"}, CalibrationSortField)
      ['-calibration_start_period', '-calibration_end_period']

    :param sort: Dictionary with 'field' and optional 'direction' ('asc' or 'desc').
    :param enum_class: Enum class defining valid sort fields and their ORM column names.
    :return: List of Django-compatible order_by fields (e.g., ['-submit_date'] or ['gage__gage_id']).
    """
    if not sort or "field" not in sort:
        return ["-id"]

    # Convert validated string → enum member
    member = enum_class.from_name(sort["field"])

    direction = sort.get("direction", "asc").lower()
    orm_field = member.orm_field

    # Normalize single vs multi-field sorts
    if isinstance(orm_field, str):
        orm_fields = [orm_field]
    else:
        orm_fields = orm_field  # already a list

    # Apply direction prefix to all fields
    if direction == "desc":
        orm_fields = [f"-{f}" for f in orm_fields]

    return orm_fields


def get_jobs(
        user: User,
        run_status: list[StatusEnum] = None,
        include_validation_data: GetValidationJobsScope = None,
        include_stop_criteria: bool = False,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
        ids_only: bool = False,
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
    :param include_stop_criteria: Whether to include stop_criteria in the queryset.
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": "created_at", "direction": "asc" or "desc" }.
    :param ids_only: Only return the ids of the calibration jobs
    :return: Tuple (results, total_count). total_count reflects total rows BEFORE pagination.
    """
    filters = filters or {}

    # ───── Validate module names (if provided) ─────
    if "module_filter" in filters:
        mf = filters["module_filter"] or {}
        modules = mf.get("modules") or []
        if modules:
            valid_modules = {m.name for m in get_cached_modules_by_id().values()}
            invalid = [m for m in modules if m not in valid_modules]
            if invalid:
                raise ValueError(
                    f"Invalid module names: {invalid}. "
                    f"Valid options are: {sorted(valid_modules)}"
                )

    order_by = resolve_sort(sort, CalibrationSortField)

    with readonly_transaction():
        # Base query: filter jobs for the user
        query = Q(owner=user)

        # If a specific status list is provided, filter by those statuses
        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        query = apply_calibration_filters(query, filters)


        # ───── annotate validation_run_count for sorting ─────
        base_qs = CalibrationRun.objects.filter(query).annotate(
            validation_run_count=Count(
                "validationrun",
                filter=~Q(validationrun__validation_type=ValidationType.VALID_CONTROL.value),
                distinct=True
            )
        )
        # ──────────────────────────────────────────────────────────

        if ids_only:
            total_count = base_qs.count()

            # Apply sorting and pagination if specified
            ids_qs = base_qs.order_by(*order_by).values_list("id", flat=True)
            if limit:
                ids_qs = ids_qs[offset: offset + limit]

            return list(ids_qs), total_count

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
            .order_by(*order_by)
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

    order_by = resolve_sort(sort, ForecastSortField)

    query = apply_forecast_filters(Q(calibration_run__owner=user), filters)

    if run_status:
        query &= Q(status_id__in=[s.db_instance.id for s in run_status])

    total_count = ForecastRun.objects.filter(query).count()

    with readonly_transaction():
        rows = list(
            ForecastRun.objects
            .filter(query)
            .order_by(*order_by)
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
    request=ForecastPaginationSerializer,
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

    validator, error_return = validate_request(ForecastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

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
    request=ForecastPaginationSerializer,
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

    validator, error_return = validate_request(ForecastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

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


def get_verification_jobs_internal(
        user: User,
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """
    Internal helper to retrieve verification jobs (READ ONLY) with optional
    filtering, sorting, and pagination.

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

    order_by = resolve_sort(sort, VerificationSortField)

    query = apply_verification_filters(Q(forecast_run__calibration_run__owner=user), filters)

    if run_status:
        query &= Q(status_id__in=[s.db_instance.id for s in run_status])

    total_count = VerificationRun.objects.filter(query).count()

    with readonly_transaction():
        rows = list(
            VerificationRun.objects
            .filter(query)
            .order_by(*order_by)
            .values(
                "id",
                "forecast_run_id",
                "status__name",
                "created_at",
                "submit_date",
            )
        )

        # ──────────────────────────────────────────
        # Apply pagination ONLY if limit provided
        # ──────────────────────────────────────────
        if limit:
            rows = rows[offset: offset + limit]

    # Normalize keys expected by the API response/serializer
    for r in rows:
        r["verification_job_id"] = r.pop("id")
        r["status"] = r.pop("status__name")

    return rows, total_count


@extend_schema(
    request=VerificationPaginationSerializer,
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

    validator, error_return = validate_request(VerificationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    verification_jobs, total_count = get_verification_jobs_internal(
        request.user,
        run_status=None,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
    )

    response = {
        'verification_jobs': verification_jobs,
        "total_count": total_count
    }

    response_validator, error_response = validate_response(
        GetVerificationJobsResponseSerializer, response,
        fields_to_truncate=['verification_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["verification_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)
