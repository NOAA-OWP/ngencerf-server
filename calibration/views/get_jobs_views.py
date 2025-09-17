import json
import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import GetValidationJobsScope, StatusEnum, ValidationType
from calibration.models import CalibrationFormulation, CalibrationRun, CalibrationStopCriteria, ValidationRun, IterationParameter, ForecastRun
from calibration.util.calibration_validators import EmptySerializer, GetCalibrationJobsForEvaluationResponseSerializer, ErrorResponseSerializer, \
    GetCalibrationJobsResponseSerializer, GetCalibrationJobsRequestSerializer, CalibrationRunSerializer, GetValidationJobsResponseSerializer, \
    GetForecastJobsResponseSerializer
from calibration.views.calibration_evaluation_views import downloadable_statuses
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, truncate_large_fields, get_calibration_run, \
    get_user_email, get_elapsed_str, readonly_transaction

logger = logging.getLogger(__name__)

User = get_user_model()


@extend_schema(
    request=GetCalibrationJobsRequestSerializer,
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
    Retrieves calibration jobs that are either DONE or FAILED for evaluation purposes.

    :param request: The HTTP request object.
    :return: JSON response with a list of calibration jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetCalibrationJobsRequestSerializer, data)
    if error_return:
        return error_return

    include_archived = validator.get('include_archived')

    jobs = get_jobs(request.user,
                    include_validation_data=GetValidationJobsScope.STATUS,
                    run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR],
                    include_archived=include_archived,
                    include_stop_criteria=True
                    )

    response = {'jobs': jobs}

    response_validator, error_response = validate_response(GetCalibrationJobsForEvaluationResponseSerializer, response, fields_to_truncate=['jobs'])
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetCalibrationJobsRequestSerializer,
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

    validator, error_return = validate_request(GetCalibrationJobsRequestSerializer, data)
    if error_return:
        return error_return

    include_archived = validator.get('include_archived')

    jobs = get_jobs(request.user,
                    run_status=[StatusEnum.DONE],
                    include_archived=include_archived,
                    include_stop_criteria=True
                    )

    response = {'jobs': jobs}

    response_validator, error_response = validate_response(GetCalibrationJobsResponseSerializer, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetCalibrationJobsRequestSerializer,
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

    validator, error_return = validate_request(GetCalibrationJobsRequestSerializer, data)
    if error_return:
        return error_return

    include_archived = validator.get('include_archived')

    jobs = get_jobs(
        request.user,
        run_status=list(StatusEnum),
        include_validation_data=GetValidationJobsScope.STATUS,
        include_archived=include_archived,
        include_stop_criteria=True
    )

    response = {'jobs': jobs}

    response_validator, error_response = validate_response(GetCalibrationJobsResponseSerializer, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def get_jobs(
        user: User,
        run_status: list[StatusEnum] = None,
        include_validation_data: GetValidationJobsScope = None,
        include_archived: bool = False,
        include_stop_criteria: bool = False
) -> list[dict[str, Any]]:
    """
    Retrieves calibration jobs for the given user with optional status filtering and validation data inclusion.
    Runs in READ ONLY mode to reduce contention. Uses `savepoint=False` to avoid unnecessary overhead since
    there are no writes, only reads.

    :param user: The user for whom the jobs are being fetched.
    :param run_status: List of statuses to filter jobs (e.g., DONE, FAILED).
    :param include_validation_data: Determines the level of validation data to include:
        - 'ids': Includes validation_run_ids and their count in validation_runs.
        - 'status': Includes validation status details.
    :param include_archived: Whether to include archived jobs in the queryset.
    :param include_stop_criteria: Whether to include stop_criteria in the queryset.
    :return: List of calibration jobs with selected fields.
    """
    with readonly_transaction():
        # Base query: filter jobs for the user
        query = Q(owner=user)

        # If include_archived=False, exclude archived jobs
        if not include_archived:
            query &= Q(is_archived=False)

        # If a specific status list is provided, filter by those statuses
        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        # Base query for CalibrationRun (dict results, lighter than ORM instances)
        calibration_runs_qs = (
            CalibrationRun.objects
            .filter(query)
            .select_related("gage", "status", "objective_function", "optimization")
            .values(
                "id", "gage__gage_id", "submit_date", "user_formulation_name",
                "calibration_start_period", "calibration_end_period",
                "status__name", "job_genesis", "created_at",
                "objective_function__name", "optimization__name",
                "is_archived", "is_locked"
            )
        )

        calibration_runs = list(calibration_runs_qs)
        run_ids = [r["id"] for r in calibration_runs]

        # Fetch formulations separately and map them to run IDs
        formulations_qs = (
            CalibrationFormulation.objects
            .filter(calibration_run_id__in=run_ids)
            .select_related("module")
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

        return results


def get_validation_jobs_internal(
        calibration_run_id: int,
        detail_level: GetValidationJobsScope = GetValidationJobsScope.IDS,
) -> list[dict[str, Any]] | list[int]:
    """
    Retrieves validation jobs for a specific calibration job.

    :param calibration_run_id: ID of the calibration run to fetch validation jobs for.
    :param detail_level: Determines the level of detail in the response:
        - 'ids': Returns only validation job IDs excluding VALID_CONTROL.
        - 'status': Returns validation_run_id, validation_type, and status, including VALID_CONTROL.
        - 'detailed': Returns full validation job details including parameters.
    :return: A list of validation job IDs, status summaries, or detailed dicts.
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


@extend_schema(
    request=EmptySerializer,
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

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    with readonly_transaction():

        forecast_jobs = list(
            ForecastRun.objects
            .filter(calibration_run__owner=request.user)
            .values(
                'id', 'calibration_run_id', 'cycle__name', 'submit_date',
                'calibration_run__gage__gage_id', 'status__name',
                'forcing_download_run__status__name'
            )
        )

    for f in forecast_jobs:
        f['forecast_run_id'] = f.pop('id')
        f['cycle'] = f.pop('cycle__name')
        f['gage_id'] = f.pop('calibration_run__gage__gage_id')
        f['forecast_status'] = f.pop('status__name')
        f['forcing_download_status'] = f.pop('forcing_download_run__status__name')

    response = {'forecast_jobs': forecast_jobs}
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
