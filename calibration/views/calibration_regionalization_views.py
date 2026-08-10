import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from django.http import FileResponse
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view

from calibration.enums import ValidationType, StatusEnum
from calibration.models import Iteration, ValidationRun, ValidationMetrics, CalibrationFormulation, CalibrationRun
from calibration.util.calibration_validators import ErrorResponseSerializer, CalibrationRunIdList
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, get_user_email, validate_request, get_calibration_run, ResponseError

logger = logging.getLogger(__name__)


@extend_schema(
    request=CalibrationRunIdList,
    responses={
        200: OpenApiResponse(description="ZIP file containing regionalization_metrics.csv and regionalization_parameters.csv"),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Generate regionalization CSVs (metrics + parameters) in-memory and return as a ZIP download."
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_regionalization_files_zip(request) -> FileResponse:
    """
    Generate regionalization CSVs for the given calibration_run_ids and return as a ZIP attachment.

    - Accepts POST JSON body or GET query params validated by CalibrationRunIdList.
    - Always returns a ZIP with two CSVs (metrics + parameters) and a JSON report of warnings/errors.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdList, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')

    job_results = []

    # make sure they all exist
    for calibration_run_id in calibration_run_ids:
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
        if error_return:
            job_results.append({
                "message": error_return.data.get('message'),
            })

    if job_results:
        return ResponseError(job_results)

    # Collect diagnostics to include in the ZIP's report.json and response headers
    warnings_list: list[dict] = []
    errors_list: list[dict] = []

    # Build both CSVs fully in-memory; append any warnings/errors along the way
    metrics_name, metrics_bytes = _build_metrics_csv_bytes(calibration_run_ids, warnings_list, errors_list)
    params_name, params_bytes = _build_params_csv_bytes(calibration_run_ids, warnings_list, errors_list)

    # Zip both CSVs + a machine-readable report.json (no temp files on disk)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(metrics_name, metrics_bytes)
        zf.writestr(params_name, params_bytes)
        # Add a diagnostic report inside the zip (client can inspect this if something looks off)
        report = {
            "requested_calibration_run_ids": calibration_run_ids,
            "warnings": warnings_list,
            "errors": errors_list,
        }
        zf.writestr("report.json", json.dumps(report, indent=2).encode("utf-8"))
    zip_buf.seek(0)

    # Stream ZIP back to the client; surface counts in headers for quick inspection
    filename = f"regionalization_files_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    resp = FileResponse(
        zip_buf,
        as_attachment=True,
        filename=filename,
        content_type='application/zip'
    )
    resp['Content-Length'] = str(zip_buf.getbuffer().nbytes)
    resp['Cache-Control'] = 'no-store'
    resp['X-Export-Warnings'] = str(len(warnings_list))
    resp['X-Export-Errors'] = str(len(errors_list))

    logger.info(
        "Regionalization export complete: calibration_run_ids=%s warnings=%d errors=%d",
        calibration_run_ids, len(warnings_list), len(errors_list)
    )
    return resp


def _build_metrics_csv_bytes(
        calibration_run_ids: list[int],
        warnings_list: list[dict],
        errors_list: list[dict]
) -> tuple[str, bytes]:
    """
    Build regionalization_metrics.csv in-memory.

    Flow:
      1) Load VALID_BEST ValidationRuns for the requested calibration_run_ids.
      2) Load all ValidationMetrics for those runs.
      3) Pivot to a wide format per (calibration_run_id, evalPeriod).
      4) Write a header + rows to a CSV buffer and return (filename, bytes).

    Logging/diagnostics:
      - Warn if a run has no VALID_BEST.
      - Error if no runs have VALID_BEST at all (still returns header-only CSV).
      - Warn if a run has VALID_BEST but no metrics.
    """
    if not calibration_run_ids:
        raise ValueError("calibration_run_ids must not be empty")

    logger.info(f"Building metrics CSV for {len(calibration_run_ids)} calibration_run_ids")

    # 1) VALID_BEST runs
    validation_runs_qs = (
        ValidationRun.objects
        .select_related('calibration_run', 'calibration_run__gage', 'calibration_run__optimization')
        .filter(calibration_run_id__in=calibration_run_ids, validation_type=ValidationType.VALID_BEST.value)
        .order_by('calibration_run_id', 'id')
    )

    runs_by_calibration_run_id: dict[int, ValidationRun] = {}
    base_info_by_calibration_run_id: dict[int, tuple[int, str, str]] = {}  # {calibration_run_id: (validation_run_id, gage_id, formulation)}
    requested = set(calibration_run_ids)

    for validation_run in validation_runs_qs:
        calibration_run = validation_run.calibration_run
        # Detect multiple VALID_BEST rows; keep the first, log & record an error for the rest
        if calibration_run.id in runs_by_calibration_run_id:
            logger.error(
                f"Multiple VALID_BEST ValidationRuns for calibration_run_id={calibration_run.id}; "
                f"keeping id={runs_by_calibration_run_id[calibration_run.id].id}, ignoring id={validation_run.id}"
            )
            errors_list.append({
                "code": "MULTIPLE_VALID_BEST",
                "calibration_run_id": calibration_run.id,
                "kept_validation_run_id": runs_by_calibration_run_id[calibration_run.id].id,
                "ignored_validation_run_id": validation_run.id,
            })
            continue

        runs_by_calibration_run_id[calibration_run.id] = validation_run
        gage_id = calibration_run.gage.gage_id if calibration_run.gage else ""
        formulation = get_formulations(calibration_run)
        base_info_by_calibration_run_id[calibration_run.id] = (validation_run.id, gage_id, formulation)

    # Warn for requested runs that lacked a VALID_BEST
    missing = sorted(requested - set(runs_by_calibration_run_id.keys()))
    for calibration_run_id in missing:
        logger.warning(f"No VALID_BEST ValidationRun found for calibration_run_id={calibration_run_id}")
        warnings_list.append({
            "code": "NO_VALID_BEST",
            "calibration_run_id": calibration_run_id,
            "detail": "No VALID_BEST ValidationRun found"
        })

    # If none of the requested runs had VALID_BEST, emit an error and return header-only CSV
    if not runs_by_calibration_run_id:
        logger.error("No VALID_BEST ValidationRuns found for any requested calibration_run_id")
        errors_list.append({
            "code": "NO_VALID_BEST_FOR_ANY",
            "detail": "No VALID_BEST ValidationRuns found for any requested calibration_run_id"
        })
        # Nothing to export; return an empty-but-valid CSV
        sio = io.StringIO(newline='')
        writer = csv.DictWriter(sio, fieldnames=['calibration_run_id', 'validation_run_id', 'gage_id', 'formulation', 'evalPeriod'])
        writer.writeheader()
        return "regionalization_metrics.csv", sio.getvalue().encode('utf-8')

    # 2) Load all metrics for those runs
    validation_metrics_qs = (
        ValidationMetrics.objects
        .filter(validation_run__in=runs_by_calibration_run_id.values())
        .select_related('metric', 'validation_run')
        .only('period', 'metric_value', 'metric__name', 'validation_run')
        .order_by('validation_run__calibration_run_id', 'period', 'metric__name')
    )

    # 3) Pivot (calibration_run_id, period) -> {metric_name: value}; build union of metric names
    metric_names: set[str] = set()
    rows_map: dict[tuple[int, str], dict[str, float]] = {}

    for validation_metric in validation_metrics_qs:
        calibration_run_id = validation_metric.validation_run.calibration_run_id
        key = (calibration_run_id, validation_metric.period)
        bucket = rows_map.setdefault(key, {})
        metric_name = validation_metric.metric.name
        metric_names.add(metric_name)
        bucket[metric_name] = validation_metric.metric_value

    # Warn if a VALID_BEST exists but produced no metrics
    have_metrics_for = {crid for (crid, _) in rows_map.keys()}
    for crid in runs_by_calibration_run_id.keys() - have_metrics_for:
        logger.warning(f"VALID_BEST has no metrics for calibration_run_id={crid}")
        warnings_list.append({
            "code": "NO_METRICS_FOR_VALID_BEST",
            "calibration_run_id": crid,
            "detail": "VALID_BEST exists but no ValidationMetrics were found"
        })

    # 4) Prepare the CSV header (union of metric columns sorted for consistency)
    metric_cols = sorted(metric_names)
    fieldnames = ['calibration_run_id', 'validation_run_id', 'gage_id', 'formulation', 'evalPeriod'] + metric_cols

    # 4) Write to an in-memory text buffer
    sio = io.StringIO(newline='')
    writer = csv.DictWriter(sio, fieldnames=fieldnames)
    writer.writeheader()

    # Sort by calibration_run_id then period for stable output
    for (calibration_run_id, period) in sorted(rows_map.keys(), key=lambda x: (x[0], x[1])):
        validation_run_id, gage_id, formulation = base_info_by_calibration_run_id.get(calibration_run_id, ("", "", ""))
        metrics_map = rows_map[(calibration_run_id, period)]
        row = {
            'calibration_run_id': calibration_run_id,
            'validation_run_id': validation_run_id,
            'gage_id': gage_id,
            'formulation': formulation,
            'evalPeriod': period,
        }
        for name in metric_cols:
            row[name] = metrics_map.get(name, "")
        writer.writerow(row)

    logger.info(f"Metrics CSV built: rows={len(rows_map)}, metric_columns={len(metric_cols)}")

    return "regionalization_metrics.csv", sio.getvalue().encode('utf-8')


def _build_params_csv_bytes(
        calibration_run_ids: list[int],
        warnings_list: list[dict],
        errors_list: list[dict]
) -> tuple[str, bytes]:
    """
    Build regionalization_parameters.csv in-memory.

    Flow:
      1) Map each calibration_run_id to its VALID_BEST ValidationRun (for validation_run_id column).
      2) Load the single best iteration (best_params=True) per calibration run.
      3) Union all parameter names across runs and emit one row per run.
      4) Write a header + rows to a CSV buffer and return (filename, bytes).

    Logging/diagnostics:
      - Warn if a run has no best iteration.
      - Warn if a best iteration has no parameters.
      - Error if multiple VALID_BEST rows exist for a run (we keep the first).
      - Error if no parameters were found for any run (still returns header-only CSV).
    """
    if not calibration_run_ids:
        raise ValueError("calibration_run_ids must not be empty")

    logger.info(f"Building parameters CSV for {len(calibration_run_ids)} calibration_run_ids")

    # 1) Map calibration_run_id -> VALID_BEST validation_run_id (for the validation_run_id column)
    validation_runs_qs = (
        ValidationRun.objects
        .filter(calibration_run_id__in=calibration_run_ids, validation_type=ValidationType.VALID_BEST.value)
        .order_by('calibration_run_id', 'id')
        .only('id', 'calibration_run_id')
    )
    validation_run_id_by_calibration_run_id: dict[int, int] = {}
    for vr in validation_runs_qs:
        # Detect multiple VALID_BEST rows (keep the first, flag an error for the rest)
        if vr.calibration_run_id in validation_run_id_by_calibration_run_id:
            logger.error(
                f"Multiple VALID_BEST ValidationRuns for calibration_run_id={vr.calibration_run_id}; "
                f"keeping id={validation_run_id_by_calibration_run_id[vr.calibration_run_id]}, ignoring id={vr.id}"
            )
            errors_list.append({
                "code": "MULTIPLE_VALID_BEST",
                "calibration_run_id": vr.calibration_run_id,
                "kept_validation_run_id": validation_run_id_by_calibration_run_id[vr.calibration_run_id],
                "ignored_validation_run_id": vr.id,
            })
            continue
        validation_run_id_by_calibration_run_id[vr.calibration_run_id] = vr.id

    # Pull best iterations (best_params=True) and prefetch parameters for names/values
    best_iterations_qs = (
        Iteration.objects
        .select_related('calibration_run', 'calibration_run__gage', 'calibration_run__optimization')
        .filter(calibration_run_id__in=calibration_run_ids, best_params=True)
        .prefetch_related('iterationparameter_set__calibration_parameter')
        .order_by('calibration_run_id', 'iteration_num')
    )

    # Build a map: calibration_run_id -> list of best iterations (should be exactly 1)
    best_by_calibration_run_id: dict[int, list[Iteration]] = {}
    for iteration in best_iterations_qs:
        best_by_calibration_run_id.setdefault(iteration.calibration_run_id, []).append(iteration)

    # Warn on missing or multiple bests; choose the first deterministically if multiple
    rows: list[dict[str, object]] = []
    parameter_names: set[str] = set()

    requested = set(calibration_run_ids)
    found = set(best_by_calibration_run_id.keys())
    for missing_id in sorted(requested - found):
        logger.warning(f"No best iteration (best_params=True) for calibration_run_id={missing_id}")
        warnings_list.append({
            "code": "NO_BEST_ITERATION",
            "calibration_run_id": missing_id,
            "detail": "No best iteration (best_params=True) found"
        })

    for calibration_run_id in sorted(found):
        best_list = best_by_calibration_run_id[calibration_run_id]
        if len(best_list) > 1:
            logger.warning(
                f"Multiple best iterations for calibration_run_id={calibration_run_id}; "
                f"using the first (iteration_num={best_list[0].iteration_num})"
            )
            # Also reflect this duplicate in warnings_list so it shows in report.json
            warnings_list.append({
                "code": "MULTIPLE_BEST_ITERATIONS",
                "calibration_run_id": calibration_run_id,
                "kept_iteration_num": best_list[0].iteration_num,
                "ignored_count": len(best_list) - 1,
            })

        best_iteration = best_list[0]
        calibration_run = best_iteration.calibration_run
        gage_id = calibration_run.gage.gage_id if calibration_run.gage else ""
        formulation = get_formulations(calibration_run)
        validation_run_id = validation_run_id_by_calibration_run_id.get(calibration_run_id, "")

        # Collect parameter name -> tuned_value for this best iteration
        param_map: dict[str, float | None] = {}
        for ip in best_iteration.iterationparameter_set.all():
            name = ip.calibration_parameter.name
            parameter_names.add(name)
            param_map[name] = ip.tuned_value

        # Warn if best iteration carries no parameters (data gap)
        if not param_map:
            logger.warning(
                f"Best iteration has no parameters for calibration_run_id={calibration_run_id} "
                f"(iteration={best_iteration.iteration_num})"
            )
            warnings_list.append({
                "code": "BEST_HAS_NO_PARAMS",
                "calibration_run_id": calibration_run_id,
                "iteration_num": best_iteration.iteration_num,
            })

        rows.append({
            "calibration_run_id": calibration_run_id,
            "validation_run_id": validation_run_id,
            "gage_id": gage_id,
            "formulation": formulation,
            "best_iteration": best_iteration.iteration_num,
            "_params": param_map,
        })

    # Write CSV
    param_cols = sorted(parameter_names)

    # If no parameters across all runs, log an error (header-only CSV will still be returned)
    if not param_cols:
        logger.error(f"No parameters found across best iterations for calibration_run_ids={calibration_run_ids}")
        errors_list.append({
            "code": "NO_PARAMS_FOR_ANY",
            "detail": "No IterationParameter records found for any best iteration"
        })

    fieldnames = ["calibration_run_id", "validation_run_id", "gage_id", "formulation", "best_iteration"] + param_cols

    sio = io.StringIO(newline='')
    writer = csv.DictWriter(sio, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        out_row = {
            "calibration_run_id": row["calibration_run_id"],
            "validation_run_id": row["validation_run_id"],
            "gage_id": row["gage_id"],
            "formulation": row["formulation"],
            "best_iteration": row["best_iteration"],
        }
        params = row["_params"]  # type: ignore[assignment]
        for name in param_cols:
            # noinspection PyUnresolvedReferences
            out_row[name] = params.get(name, "")
        writer.writerow(out_row)

    logger.info(f"Parameters CSV built: rows={len(rows)}, param_columns={len(param_cols)}")

    return "regionalization_parameters.csv", sio.getvalue().encode('utf-8')


def get_formulations(run: CalibrationRun) -> str:
    formulations = CalibrationFormulation.objects.filter(calibration_run=run).select_related('module')
    # Sort to ensure deterministic order in CSV
    module_names = sorted({f.module.name for f in formulations})
    return ' '.join(module_names)
