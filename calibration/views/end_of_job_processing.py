import logging
import math
import os
import time
import traceback
from collections import deque
from datetime import timedelta
from itertools import groupby
from operator import attrgetter
from typing import cast, Any

import pandas as pd
from datetimerange import DateTimeRange
from django.db import transaction
from django.utils.timezone import now

from calibration.enums import OptimizationEnum, ValidationMetricPeriod, ValidationType, MetricEnum
from calibration.enums_vanilla import SecondaryDataEnum, JobExecutionMode
from calibration.models import Iteration, CalibrationRun, IterationMetric, IterationParameter, CalibrationParameter, ValidationRun, \
    PerformanceMetrics, ValidationMetrics, NWMRetrospectiveMetrics, IterationResult, ColdStartRun, ForecastRun, \
    VerificationRun, CalibrationFormulation, HindcastRun
from calibration.models.base_run import BaseRun
from calibration.util.caching import have_LSTM, get_cached_modules_by_id
from calibration.util.ngen_locations import get_realization_file_path, get_metrics_iteration_file, \
    get_objective_log_best_file, get_calibration_worker_path, get_global_best_params_file, get_validation_metrics_valid_control_file, \
    get_validation_metrics_valid_best_file, get_validation_metrics_valid_iteration_file, \
    get_validation_metrics_nwm_retrospective_file, get_output_iteration_csv, \
    get_params_iteration_file, get_observational_file_for_hindcast
from calibration.views.calibration_secondary_data_views import generate_secondary_ts_data, should_generate_swe, should_generate_soil_moisture
from calibration.views.common import CerfException, get_job_description, find_validation_worker_with_matching_id
from calibration.views.data_services import get_observational_date_range_from_data_services, get_observational_data_from_data_services
from cerfServer.settings import JOB_EXECUTION_MODE

logger = logging.getLogger(__name__)

BULK_CREATE_BATCH_SIZE = 1000  # Define a reasonable batch size


def to_float_or_nan(value: object) -> float:
    """
    Convert arbitrary CSV/pandas values to a float.

    Missing, blank, or NA-like values are converted to NaN (not None) so
    FloatField(null=False) constraints are satisfied.
    """
    # Fast-path for Python None
    if value is None:
        return float("nan")

    # Handle pandas/numpy scalar NA safely (NaN, NA, NaT, etc.).
    # cast(Any, ...) is for the type checker only; pd.isna accepts arbitrary objects at runtime.
    try:
        if pd.isna(cast(Any, value)):
            return float("nan")
    except Exception:
        # Non-scalar / unexpected object; fall through to float(), which will raise if invalid
        pass

    # Treat blank or whitespace-only strings as missing
    if isinstance(value, str) and not value.strip():
        return float("nan")

    # Normal numeric conversion (raises if invalid)
    return float(cast(Any, value))


def read_validation_output(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Processes the output of a validation run by identifying the correct worker,
    retrieving performance metrics, and updating the validation run's attributes.

    :param validation_run: The ValidationRun instance.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(validation_run)

    logger.info(f"Processing output for {job_description}, status: {validation_run.status}")

    # --- Moved this read-only query OUTSIDE the transaction to reduce lock contention ---
    validation_type = ValidationType(validation_run.validation_type)
    iteration = validation_run.iteration if validation_type == ValidationType.VALID_ITERATION else None

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(validation_run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        # Identify the matching worker based on validation type
        matching_worker = find_validation_worker_with_matching_id(
            validation_run,
            worker_name=iteration.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
            iteration_num=iteration.iteration_num if validation_type == ValidationType.VALID_ITERATION else None,
        )
        validation_run.validation_worker_name = matching_worker
        validation_run.save(update_fields=['validation_worker_name'])

        create_performance_metrics(
            validation_run,
            slurm_accounting
        )

        if not failed_so_far:
            process_validation_for_validation_run(validation_run)

    logger.info(f"End of processing output for {job_description}")


def read_calibration_output(calibration_run: CalibrationRun, failed_so_far: bool) -> None:
    """
    Process the output of a CalibrationRun. This function handles reading and
    processing the worker directories and their iteration files.

    :param calibration_run: The CalibrationRun instance whose output is to be processed.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(calibration_run)

    logger.info(f"Processing output for {job_description}, status={calibration_run.status}")

    already_processed = IterationMetric.objects.filter(
        iteration__calibration_run=calibration_run
    ).exists()

    if already_processed:
        raise CerfException(
            f"End of job processing has already been completed for {job_description}"
        )

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(calibration_run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        create_performance_metrics(
            calibration_run,
            slurm_accounting
        )

        if not failed_so_far:
            # Set the realization file path for the run
            calibration_run.realization_file_path = get_realization_file_path(calibration_run)
            calibration_run.save(update_fields=['realization_file_path'])

            process_iterations_for_all_workers(calibration_run)

    logger.info(f"End of processing output for {job_description}")


def read_cold_start_output(run: ColdStartRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a cold start run by creating performance metrics.

    :param run: The ColdStartRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        create_performance_metrics(
            run,
            slurm_accounting
        )

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_forecast_output(run: ForecastRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a forecast run by creating performance metrics.

    :param run: The ForecastRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        create_performance_metrics(
            run,
            slurm_accounting
        )

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_hindcast_output(run: HindcastRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a hindcast run by creating performance metrics.

    :param run: The HindcastRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")

    # Get observed data for the Hindcast
    _write_observed_hindcast_file(run)

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        create_performance_metrics(
            run,
            slurm_accounting
        )

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_verification_output(run: VerificationRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a verification run by creating performance metrics.

    :param run: The VerificationRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")

    # Wait for finalized Slurm accounting before opening the database transaction.
    slurm_accounting = (
        get_final_slurm_accounting(run)
        if JOB_EXECUTION_MODE == JobExecutionMode.SLURM
        else None
    )

    with transaction.atomic():
        create_performance_metrics(
            run,
            slurm_accounting
        )

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def create_performance_metrics(
        run: BaseRun,
        slurm_accounting: dict[str, Any] | None = None,
) -> None:
    """
    Create performance metrics and associate them with the run.

    In Slurm mode, use finalized accounting data retrieved before entering the
    surrounding database transaction.

    Docker and SLURM_MOCK do not provide detailed accounting data, so they
    create a runtime-only PerformanceMetrics record.

    :param run: The run instance.
    :param slurm_accounting: Finalized SlurmDB accounting response, when running
        in Slurm mode.
    :return: None
    """
    job_description = get_job_description(run)
    performance_metrics = None

    if JOB_EXECUTION_MODE == JobExecutionMode.SLURM:
        if slurm_accounting:
            performance_metrics = (
                create_performance_metrics_from_slurm_accounting(
                    slurm_accounting
                )
            )
        else:
            logger.warning(
                f"Final Slurm accounting data was unavailable for "
                f"{job_description}"
            )

    if not performance_metrics:
        # Fallback to calculate run_time manually.
        run_time = now() - run.run_start

        logger.info(
            f"Creating runtime-only performance metrics for "
            f"{job_description}: run_time={run_time}"
        )

        performance_metrics = PerformanceMetrics.objects.create(
            run_time=run_time
        )

    run.performance_metrics = performance_metrics
    run.save(update_fields=["performance_metrics"])


def get_slurm_tres_count(
        tres_values: list[dict[str, Any]],
        tres_type: str,
        tres_name: str = "",
) -> int | float | None:
    """
    Return the count for a matching Slurm TRES entry.

    :param tres_values: List of Slurm TRES dictionaries.
    :param tres_type: TRES type, such as "mem" or "fs".
    :param tres_name: Optional TRES name, such as "disk".
    :return: Matching count, or None if no matching entry exists.
    """
    for tres in tres_values:
        if tres.get("type") != tres_type:
            continue

        if tres_name and tres.get("name") != tres_name:
            continue

        count = tres.get("count")

        if isinstance(count, (int, float)):
            return count

    return None


def create_performance_metrics_from_slurm_accounting(
        accounting_data: dict[str, Any],
) -> PerformanceMetrics | None:
    """
    Create a PerformanceMetrics record from finalized SlurmDB accounting data.

    SlurmDB provides the main job record and its individual steps. General job
    timing and CPU allocation are taken from the main job record. Maximum
    memory, disk read, and disk write values are taken from the batch step.

    SlurmDB reports memory and filesystem counts in bytes. PerformanceMetrics
    stores those values in kilobytes, matching the previous sacct --units=K
    behavior.

    :param accounting_data: Finalized SlurmDB job accounting response.
    :return: Created PerformanceMetrics record, or None if required records
        cannot be found.
    """
    jobs = accounting_data.get("jobs") or []

    if not jobs:
        logger.warning("Slurm accounting response does not contain a job record")
        return None

    # The endpoint is queried for one Slurm JobID, so the first item is the job.
    job = jobs[0]
    steps = job.get("steps") or []

    # MaxRSS and disk I/O statistics are stored on the .batch step.
    batch_step = next(
        (
            step
            for step in steps
            if str(
            (step.get("step") or {}).get("name", "")
        ).lower() == "batch"
        ),
        None
    )

    if not batch_step:
        logger.warning(
            f"Slurm accounting response for JobID "
            f"{job.get('job_id')} does not contain a batch step"
        )
        return None

    job_time = job.get("time") or {}

    # Elapsed wall-clock runtime for the job, in seconds.
    elapsed_seconds = job_time.get("elapsed")

    required = job.get("required") or {}

    # Number of CPUs allocated to the job.
    num_cpus = required.get("CPUs")

    planned = job_time.get("planned") or {}

    # Time between the job becoming eligible and starting, in seconds.
    planned_seconds = planned.get("number") if planned.get("set") else None

    batch_tres = batch_step.get("tres") or {}
    requested_tres = batch_tres.get("requested") or {}
    consumed_tres = batch_tres.get("consumed") or {}

    requested_max = requested_tres.get("max") or []
    consumed_max = consumed_tres.get("max") or []

    # Maximum resident memory used by the batch step, reported in bytes.
    max_rss_bytes = get_slurm_tres_count(
        requested_max,
        "mem",
    )

    # Maximum bytes read from the filesystem by the batch step.
    max_disk_read_bytes = get_slurm_tres_count(
        requested_max,
        "fs",
        "disk",
    )

    # Maximum bytes written to the filesystem by the batch step.
    max_disk_write_bytes = get_slurm_tres_count(
        consumed_max,
        "fs",
        "disk",
    )

    # Preserve the same identifier previously obtained from the sacct .batch row.
    batch_step_id = (batch_step.get("step") or {}).get("id")

    # Wall-clock duration of the job.
    run_time = (
        timedelta(seconds=elapsed_seconds)
        if isinstance(elapsed_seconds, (int, float))
        else None
    )

    # Equivalent to sacct CPUTime: elapsed time multiplied by allocated CPUs.
    cpu_time = (
        timedelta(seconds=elapsed_seconds * num_cpus)
        if isinstance(elapsed_seconds, (int, float))
           and isinstance(num_cpus, (int, float))
        else None
    )

    # Queue wait time before execution began.
    reserved_time = (
        timedelta(seconds=planned_seconds)
        if isinstance(planned_seconds, (int, float))
        else None
    )

    # Convert SlurmDB byte counts to KiB, matching sacct --units=K.
    max_rss = (
        max_rss_bytes / 1024
        if isinstance(max_rss_bytes, (int, float))
        else None
    )
    max_disk_read = (
        max_disk_read_bytes / 1024
        if isinstance(max_disk_read_bytes, (int, float))
        else None
    )
    max_disk_write = (
        max_disk_write_bytes / 1024
        if isinstance(max_disk_write_bytes, (int, float))
        else None
    )

    missing_fields = []

    if run_time is None:
        missing_fields.append("Elapsed")
    if num_cpus is None:
        missing_fields.append("NCPUS")
    if cpu_time is None:
        missing_fields.append("CPUTime")
    if max_rss is None:
        missing_fields.append("MaxRSS")
    if max_disk_read is None:
        missing_fields.append("MaxDiskRead")
    if max_disk_write is None:
        missing_fields.append("MaxDiskWrite")
    if reserved_time is None:
        missing_fields.append("Planned")

    if missing_fields:
        logger.warning(
            f"Missing finalized Slurm accounting fields for JobID "
            f"{job.get('job_id')}: {', '.join(missing_fields)}"
        )

    metrics = PerformanceMetrics.objects.create(
        slurm_job_id=batch_step_id or str(job.get("job_id")),
        run_time=run_time,
        num_cpus=int(num_cpus) if isinstance(num_cpus, (int, float)) else None,
        cpu_time=cpu_time,
        max_rss=max_rss,
        max_disk_read=max_disk_read,
        max_disk_write=max_disk_write,
        reserved_time=reserved_time,
    )

    logger.info(
        f"Created performance metrics from SlurmDB for JobID "
        f"{job.get('job_id')}: run_time={run_time}, "
        f"num_cpus={num_cpus}, cpu_time={cpu_time}, "
        f"max_rss={max_rss} KB, "
        f"max_disk_read={max_disk_read} KB, "
        f"max_disk_write={max_disk_write} KB, "
        f"reserved_time={reserved_time}"
    )

    return metrics


def get_final_slurm_accounting(
        run: BaseRun,
        max_attempts: int = 10,
        retry_delay: float = 1.0,
) -> dict[str, Any] | None:
    """
    Wait for SlurmDB to publish finalized accounting data for a Slurm job.

    :param run: Run whose Slurm accounting data is required.
    :param max_attempts: Maximum number of accounting requests.
    :param retry_delay: Seconds between accounting requests.
    :return: Final SlurmDB accounting response, or None if unavailable.
    """
    if run.slurm_job_id is None or run.slurm_job_id < 0:
        return None

    from calibration.run_util.job_executor_slurm import (
        get_slurm_job_accounting,
    )

    job_description = get_job_description(run)

    terminal_states = {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "BOOT_FAIL",
        "DEADLINE",
        "PREEMPTED",
        "REVOKED",
        "SPECIAL_EXIT",
    }

    for attempt in range(1, max_attempts + 1):
        accounting_data = get_slurm_job_accounting(run.slurm_job_id)

        logger.debug(
            f"Full Slurm accounting response for {job_description}, "
            f"attempt {attempt}: {accounting_data}"
        )

        jobs = accounting_data.get("jobs") if accounting_data else None
        job = jobs[0] if jobs else None

        state = None

        if job:
            state_data = job.get("state")

            if isinstance(state_data, dict):
                state = state_data.get("current")
            else:
                state = state_data

            # SlurmDB represents the current state as a one-item list.
            if isinstance(state, list):
                state = state[0] if state else None

            if state:
                state = str(state).upper()

        steps = job.get("steps") if job else None

        # Resource high-water values are recorded on the batch step.
        batch_step = next(
            (
                step
                for step in steps or []
                if str(
                (step.get("step") or {}).get("name", "")).lower() == "batch"
            ),
            None,
        )

        # Require both the completed job record and its batch-step accounting.
        if state in terminal_states and batch_step:
            logger.info(
                f"Final Slurm accounting found for {job_description} "
                f"on attempt {attempt}, state={state}"
            )
            return accounting_data

        if attempt < max_attempts:
            logger.info(
                f"Final Slurm accounting not yet available for "
                f"{job_description}; state={state}, "
                f"batch_step_found={batch_step is not None}; "
                f"retrying in {retry_delay} seconds "
                f"({attempt}/{max_attempts})"
            )
            time.sleep(retry_delay)

    logger.warning(
        f"Final Slurm accounting was not available for {job_description} "
        f"after {max_attempts} attempts"
    )

    return None


def process_validation_metrics(run: ValidationRun | CalibrationRun, metrics_file: str, expected_run_type: str) -> None:
    """
    Generic function to process validation or calibration metrics from a CSV file and create corresponding Metric objects.

    :param run: The ValidationRun or CalibrationRun instance.
    :param metrics_file: The file path of the metrics CSV file.
    :param expected_run_type: The expected run type to validate.
    :return: None
    """

    job_description = get_job_description(run)
    logger.info(f"Processing '{metrics_file}' for {job_description}")

    # Check if the file exists
    if not os.path.isfile(metrics_file):
        logger.error(f'{metrics_file} does not exist')
        return

    # Read the metrics file using pandas
    # Treat common "blank"/string-missing tokens as NA so they become NaN in pandas
    metrics_df = pd.read_csv(metrics_file, na_values=['', ' ', 'null', 'None'])

    metrics_to_create = []  # List to accumulate metrics to be created

    # Determine the type of metric to create
    MetricModel = ValidationMetrics if isinstance(run, ValidationRun) else NWMRetrospectiveMetrics

    # Loop over each row in the metrics file
    for _, row in metrics_df.iterrows():
        # Extract the run type and period fields
        run_type = str(row['run']).strip()
        if run_type != expected_run_type:
            logger.info(f'Unexpected run_type in {metrics_file} - {run_type}')

        period = str(row['period']).strip()
        if period not in ValidationMetricPeriod.get_names():
            logger.info(f'Unexpected period in {metrics_file} - {period}')

        # Extract the metrics starting from the third column onwards
        metrics_row = row[2:]

        # For each metric in the row, create or update the relevant Metric model
        for metric_name, value in metrics_row.items():
            metric_name = cast(str, metric_name)
            # Perform case-insensitive lookup for the metric
            metric = MetricEnum.get_instance(metric_name)
            if not metric:
                raise CerfException(f"Could not find metric '{metric_name}' in MetricEnum")

            metric_value = to_float_or_nan(value)

            # Create the Metric object (ValidationMetrics or NWMRetrospectiveMetrics)
            metric_obj = MetricModel(
                metric=metric,
                run_type=run_type,
                period=period,
                metric_value=metric_value,
                **({'validation_run': run} if isinstance(run, ValidationRun) else {'calibration_run': run})
            )
            logger.debug(
                f'{job_description}, type: {expected_run_type}: Creating {MetricModel.__name__} metric for Period: {period}, {metric_name} with value {metric_value}'
            )

            metrics_to_create.append(metric_obj)

    # Bulk create the metrics in the database
    if metrics_to_create:
        bad = [m for m in metrics_to_create if m.metric_value is None]
        if bad:
            raise CerfException(f"BUG: metric_value None before bulk_create (count={len(bad)})")

        MetricModel.objects.bulk_create(
            metrics_to_create,
            batch_size=BULK_CREATE_BATCH_SIZE
        )


def process_validation_for_validation_run(validation_run: ValidationRun) -> None:
    """
    Read the file that is created by the Validation run for the specific iteration.
    Processes the metrics and updates the corresponding ValidationMetric entries.

    :param validation_run: The ValidationRun instance.
    :return: None
    """
    job_description = get_job_description(validation_run)

    metrics_file = None
    expected_run_type = None
    worker_name = validation_run.worker_name
    iteration_num = validation_run.iteration_num

    if validation_run.validation_type == ValidationType.VALID_ITERATION.value:
        metrics_file = get_validation_metrics_valid_iteration_file(validation_run.calibration_run, worker_name, iteration_num)
        expected_run_type = f'valid_{worker_name}_iter{iteration_num}'
    elif validation_run.validation_type == ValidationType.VALID_CONTROL.value:
        metrics_file = get_validation_metrics_valid_control_file(validation_run.calibration_run)
        expected_run_type = ValidationType.VALID_CONTROL.value
    elif validation_run.validation_type == ValidationType.VALID_BEST.value:
        metrics_file = get_validation_metrics_valid_best_file(validation_run.calibration_run)
        expected_run_type = ValidationType.VALID_BEST.value

    assert metrics_file is not None and expected_run_type is not None

    already_done = ValidationMetrics.objects.filter(
        validation_run=validation_run, run_type=expected_run_type
    ).exists()

    if already_done:
        raise CerfException(f"End of job processing has already been completed for {job_description}")

    process_validation_metrics(
        run=validation_run,
        metrics_file=metrics_file,
        expected_run_type=expected_run_type
    )

    if validation_run.validation_type == ValidationType.VALID_BEST.value:
        logger.info("Processing nwm retrospective data")

        # NWM Retrospective data is processed as part of Validation Best, but we save it in the Calibration Run
        metrics_file = get_validation_metrics_nwm_retrospective_file(validation_run.calibration_run)
        expected_run_type = 'nwm_retro'

        process_validation_metrics(
            run=validation_run.calibration_run,
            metrics_file=metrics_file,
            expected_run_type=expected_run_type
        )

    if validation_run.validation_type == ValidationType.VALID_BEST.value:
        # ------------------------------------------------------------------
        # Unconditionally get Precipitation data for Validation best
        # ------------------------------------------------------------------
        logger.info(f"Generating {SecondaryDataEnum.PRECIPITATION.value} timeseries data")
        try:
            generate_secondary_ts_data(validation_run, SecondaryDataEnum.PRECIPITATION)
        except Exception as e:
            logger.error(f"Failed to generate {SecondaryDataEnum.PRECIPITATION.value} timeseries data: {e}")
            traceback.print_exc()

    # Get SWE and Soil Moisture for all validations except control
    if validation_run.validation_type != ValidationType.VALID_CONTROL.value:
        # ------------------------------------------------------------------
        # Secondary timeseries (SWE / Soil Moisture) — conditional by modules
        # ------------------------------------------------------------------
        modules_by_id = get_cached_modules_by_id()
        modules_by_name = {m.name: m for m in modules_by_id.values()}

        formulations = (
            CalibrationFormulation.objects
            .filter(calibration_run=validation_run.calibration_run)
            .only("module_id")
        )

        module_names_for_job = {modules_by_id[f.module_id].name for f in formulations}
        modules_by_name_for_job = {name: modules_by_name[name] for name in module_names_for_job}

        if should_generate_swe(modules_by_name_for_job):
            logger.info(f"Generating {SecondaryDataEnum.SWE.value} timeseries data")
            try:
                generate_secondary_ts_data(validation_run, SecondaryDataEnum.SWE)
            except Exception as e:
                logger.error(f"Failed to generate {SecondaryDataEnum.SWE.value} timeseries data: {e}")
                traceback.print_exc()

        if should_generate_soil_moisture(modules_by_name_for_job):
            logger.info(f"Generating {SecondaryDataEnum.SOIL_MOISTURE.value} timeseries data")
            try:
                generate_secondary_ts_data(validation_run, SecondaryDataEnum.SOIL_MOISTURE)
            except Exception as e:
                logger.error(f"Failed to generate {SecondaryDataEnum.SOIL_MOISTURE.value} timeseries data: {e}")
                traceback.print_exc()


def process_iterations_for_all_workers(calibration_run: CalibrationRun) -> None:
    """
    Process all Iteration objects for the workers of a given CalibrationRun.
    It loads all Iteration rows once, groups them by worker, and processes the iterations
    for each worker based on their metrics and parameters.

    NOTE ON NUMERIC VALUES
    ----------------------
    We intentionally store raw DB values (including NaN/±Inf if they occur).
    Any normalization for JSON safety happens at API response time.

    :param calibration_run: The CalibrationRun instance.
    """

    # ------------------------------------------------------------
    # This function operates at the *run* level.
    #
    # - Worker-level processing determines which iteration is best
    # - This function enforces run-level invariants:
    #     * Best-params dictionary loading
    #     * Calling per-worker processing
    #     * Verifying that exactly one best iteration exists
    # ------------------------------------------------------------
    # Compute best_params_dict once, outside the loop
    best_params_dict: dict[str, float] = {}

    have_LSTM_flag = have_LSTM(calibration_run)

    logger.debug(
        f"[BESTPARAMS CHECK] Run {calibration_run.id} using optimization={calibration_run.optimization}; "
        f"DDS={calibration_run.optimization == OptimizationEnum.DDS.db_instance}, "
        f"LSTM={have_LSTM_flag}"
    )

    # ----------------------------------------------------------------------
    # NON-DDS branch (GWO / PSO)
    # Only load global best params if NOT DDS AND NOT LSTM
    # ----------------------------------------------------------------------
    if calibration_run.optimization != OptimizationEnum.DDS.db_instance:
        if not have_LSTM_flag:
            global_best_params_file = get_global_best_params_file(calibration_run)

            logger.debug(
                f"[BESTPARAMS CHECK] Non-DDS + Non-LSTM: expecting global_best_params_file={global_best_params_file}"
            )

            if not os.path.isfile(global_best_params_file):
                logger.error(
                    f"[BESTPARAMS ERROR] global_best_params_file does NOT exist for run {calibration_run.id}: "
                    f"{global_best_params_file}"
                )
                raise CerfException(f"{global_best_params_file} does not exist")

            # File exists — load it
            logger.debug(
                f"[BESTPARAMS CHECK] Loading global best params file for run {calibration_run.id}"
            )
            # Read the global best parameters into a dictionary

            df = pd.read_csv(global_best_params_file, names=['value', 'name', 'model'], skiprows=1)

            # IMPORTANT:
            # - params_match_best() expects numeric values (float/None) so it can use math.isclose.
            # - We do NOT "sanitize" (NaN/±Inf -> None) here; we just coerce to float.
            best_params_dict = {
                str(name): float(value)
                for name, value in zip(df['name'], df['value'])
            }

            logger.debug(
                f"[BESTPARAMS CHECK] Loaded {len(best_params_dict)} best params for run {calibration_run.id}: "
                f"{list(best_params_dict.keys())[:10]}..."
            )

    # ----------------------------------------------------------------------
    # DDS branch — SHOULD NOT USE global best params
    # But we add diagnostics if the file exists
    # ----------------------------------------------------------------------
    else:
        global_best_params_file = get_global_best_params_file(calibration_run)
        if os.path.isfile(global_best_params_file):
            logger.error(
                f"[DDS WARNING] global_best_params_file EXISTS for DDS run {calibration_run.id}: "
                f"{global_best_params_file}. DDS should not produce this file."
            )
        else:
            logger.debug(
                f"[DDS OK] No global_best_params_file present for DDS run {calibration_run.id}"
            )

        # Explicitly ensure no params are used
        best_params_dict = {}

    # Query all Iteration objects for the calibration run and prefetch related metrics and parameters
    iterations = list(
        Iteration.objects
        .filter(calibration_run_id=calibration_run.id)
        .only("id", "worker_name", "iteration_num", "best_params")
        .order_by("worker_name", "iteration_num")
    )

    # Load all CalibrationParameter rows ONCE for this run
    job_parameters = CalibrationParameter.objects.filter(
        calibration_formulation__calibration_run_id=calibration_run.id
    )
    params_lookup = {p.name.lower(): p for p in job_parameters}

    # ----------------------------------------------------------------------
    # Group the iterations by worker and process them
    # ----------------------------------------------------------------------
    for worker_name, worker_iterations in groupby(iterations, key=attrgetter('worker_name')):
        process_iterations_for_a_worker(
            calibration_run,
            worker_name,
            list(worker_iterations),
            best_params_dict,
            have_LSTM_flag,
            params_lookup
        )

    # ----------------------------------------------------------------------
    # FINAL DIAGNOSTIC — Make sure exactly one best iteration exists
    # Applies to DDS, GWO/PSO, LSTM
    # ----------------------------------------------------------------------
    best_list = list(
        Iteration.objects
        .filter(calibration_run=calibration_run, best_params=True)
        .values_list("id", "iteration_num", "worker_name")
    )

    if len(best_list) == 0:
        logger.error(
            f"[BESTPARAMS ERROR] No best iteration found after processing for run {calibration_run.id}. "
            f"(optimization={calibration_run.optimization}, LSTM={have_LSTM_flag})"
        )
    elif len(best_list) > 1:
        logger.error(
            f"[BESTPARAMS ERROR] Multiple ({len(best_list)}) best iterations found for run {calibration_run.id}. "
            f"Expected exactly one. Details: {best_list}"
        )
    else:
        logger.debug(
            f"[BESTPARAMS OK] Exactly one best iteration found for run {calibration_run.id}: {best_list[0]}"
        )

    # ----------------------------------------------------------------------
    # Final check — only runs without LSTM require best iteration detection
    # ----------------------------------------------------------------------
    if not have_LSTM_flag:
        # Raise an error if no best iteration was found
        has_best = Iteration.objects.filter(
            calibration_run=calibration_run, best_params=True
        ).exists()

        if not has_best:
            raise CerfException(f"No best iteration was found for CalibrationRun {calibration_run.id}")


# Function to process iterations for a specific worker
def process_iterations_for_a_worker(
        calibration_run: CalibrationRun,
        worker_name: str,
        iterations: list[Iteration],
        best_params_dict: dict[str, float],
        have_LSTM_flag: bool,
        params_lookup: dict[str, CalibrationParameter]
) -> None:
    """
    Process all iterations for a specific worker in a CalibrationRun.
    It reads the metrics and parameters files for the worker and processes each
    iteration for metrics and parameters creation.

    Best-iteration rules:
      - DDS: best iteration number is read from objective_log_best_file
      - GWO/PSO: best iteration is determined by matching parameters against global_best_params_file
      - LSTM: only one iteration exists and is always best

    NOTE ON NUMERIC VALUES
    ----------------------
    We store the numeric values as given (including NaN/±Inf if present).
    We still coerce parameter values to float so matching (math.isclose) works.
    This may raise if the parameters CSV contains non-numeric values.
    JSON-safety normalization happens at API response construction time.

    :param calibration_run: The CalibrationRun instance.
    :param worker_name: The name of the worker. This is the middle part of the worker name.
                        Need to prefix with ngen_ and suffix with _worker.
    :param iterations: A list of Iteration objects for the worker.
    :param best_params_dict: Precomputed dictionary of best parameters for comparison.
    :param have_LSTM_flag: Flag to indicate whether this job has LSTM
    :param params_lookup: Mapping of lowercased parameter names to CalibrationParameter objects
    """

    logger.info(f"Processing iterations for {worker_name} for Calibration Job {calibration_run.id}")

    # ------------------------------------------------------------
    # Worker-level best-iteration determination happens here.
    #
    # Rules:
    #
    # DDS:
    #   - Best iteration is read from objective_log_best_file
    #
    # GWO / PSO:
    #   - Best iteration is determined by matching parameters
    #     against global_best_params_file
    #
    # LSTM:
    #   - Only one iteration exists and is always best
    #
    # This is the ONLY function that sets iteration.best_params.
    # ------------------------------------------------------------

    job_description = f"Calibration Job {calibration_run.id}, user: {calibration_run.owner.username}"

    # Get the worker's path
    worker_path = get_calibration_worker_path(calibration_run, worker_name)
    if not os.path.isdir(worker_path):
        raise CerfException(f"{worker_path} does not exist or is not a directory for CalibrationRun {calibration_run.id}")

    # Get the necessary files for metrics, parameters, and best objective function log
    metrics_iteration_file = get_metrics_iteration_file(calibration_run, worker_name)
    params_iteration_file = get_params_iteration_file(calibration_run, worker_name)
    # Contains the best for DDS
    objective_log_best_file = get_objective_log_best_file(calibration_run, worker_name)

    # Check if the files exist
    if not os.path.isfile(metrics_iteration_file):
        raise CerfException(f'{metrics_iteration_file} does not exist for CalibrationRun {calibration_run.id}')
    if not os.path.isfile(params_iteration_file) and not have_LSTM_flag:
        raise CerfException(f'{params_iteration_file} does not exist for CalibrationRun {calibration_run.id}')

    # Check for the best iteration based on optimization type (DDS, GWO, PSO)
    best_iteration_for_worker = -1
    if calibration_run.optimization == OptimizationEnum.DDS.db_instance:
        if not os.path.isfile(objective_log_best_file):
            raise CerfException(f'{objective_log_best_file} does not exist for CalibrationRun {calibration_run.id}')
        # Read the best iteration from the log
        last_line = read_last_line(objective_log_best_file)
        best_iteration_for_worker = int(last_line.split(',')[2])
    else:
        # for GWO and PSO, we can't get the best iteration number.
        # We need to read the actual best parameters and then try to match them up when we read the parameter file later
        pass

    # Prefetch Iteration objects for efficiency
    iteration_dict = {it.iteration_num: it for it in iterations}

    metrics_to_create: list[IterationMetric] = []
    params_to_create: list[IterationParameter] = []

    # Collect best_params updates instead of saving per-iteration
    iterations_to_update_best_flag: list[Iteration] = []

    # Track whether a best iteration was set
    best_iteration_found = False

    # Process metrics file
    # Treat common "blank"/string-missing tokens as NA so they become NaN in pandas
    metrics_df = pd.read_csv(metrics_iteration_file, na_values=["", " ", "null", "None"])

    if not have_LSTM_flag:
        update_objective_function_values(metrics_iteration_file, calibration_run, worker_name)

    for _, row in metrics_df.iterrows():
        iteration_num = int(row['iteration'])

        # Raw values (no NaN/Inf cleanup). Keep as-is for DB write.
        row_dict = {
            str(k): row[k]
            for k in row.index
            if k != 'iteration'
        }

        iteration = iteration_dict.get(iteration_num)
        if not iteration:
            raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")

        process_metrics_row_for_calibration(iteration, row_dict, metrics_to_create, job_description)

        if have_LSTM_flag:
            # For LSTM, there is only 1 iteration so we will mark it as having the best
            iteration.best_params = True
            iterations_to_update_best_flag.append(iteration)  # buffer update

    # Process parameters file
    if not have_LSTM_flag:
        params_df = pd.read_csv(params_iteration_file)

        # # Prefetch CalibrationParameter objects once per calibration_run
        # job_parameters = CalibrationParameter.objects.filter(
        #     calibration_formulation__calibration_run_id=calibration_run.id
        # )
        # params_lookup = {p.name.lower(): p for p in job_parameters}

        for _, row in params_df.iterrows():
            iteration_num = int(row['iteration'])

            iteration = iteration_dict.get(iteration_num)
            if not iteration:
                raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")

            # IMPORTANT:
            # - params_match_best() uses math.isclose, so we must coerce to float.
            # - We do NOT sanitize (NaN/±Inf -> None); we just float() the value.
            params_row = {
                str(k): float(cast(Any, row[k]))
                for k in row.index
                if k != 'iteration'
            }

            is_best_match = params_match_best(params_row, best_params_dict)

            iteration.best_params = (
                    is_best_match or
                    iteration.iteration_num == best_iteration_for_worker
            )

            if iteration.best_params:
                best_iteration_found = True

            # Buffer update — saved once via bulk_update
            iterations_to_update_best_flag.append(iteration)

            # Create parameters
            process_params_row(
                iteration,
                params_row,
                params_to_create,
                params_lookup,
                job_description
            )

    # Bulk create IterationMetric and IterationParameter objects with Django batching
    if metrics_to_create:
        bad = [m for m in metrics_to_create if m.metric_value is None]
        if bad:
            raise CerfException(f"BUG: metric_value None before bulk_create (count={len(bad)})")

        IterationMetric.objects.bulk_create(
            metrics_to_create,
            batch_size=BULK_CREATE_BATCH_SIZE
        )

    # Bulk create parameters
    if params_to_create:
        IterationParameter.objects.bulk_create(
            params_to_create,
            batch_size=BULK_CREATE_BATCH_SIZE
        )

    # Single bulk update for best_params instead of thousands of saves
    if iterations_to_update_best_flag:
        Iteration.objects.bulk_update(
            iterations_to_update_best_flag,
            ['best_params'],
            batch_size=BULK_CREATE_BATCH_SIZE,
        )

    # Log a warning if no best iteration was found for the worker
    if not best_iteration_found:
        logger.warning(f"No best iteration found for worker {worker_name} in CalibrationRun {calibration_run.id}")

    # Check if this worker has a non-empty Output_Iteration directory
    output_iter = os.path.join(worker_path, 'Output_Iteration')
    if os.path.isdir(output_iter) and any(os.listdir(output_iter)):
        # Iterate over files and check if a file matches the current iteration number
        for filename in os.listdir(output_iter):
            # Check if the file matches the iteration number format
            for iteration_num in iteration_dict:
                expected_filename = get_output_iteration_csv(calibration_run, iteration_num)
                if filename == expected_filename:
                    # Save the filename to the IterationResult for this iteration
                    iteration = iteration_dict.get(iteration_num)
                    if iteration:
                        IterationResult.objects.create(iteration=iteration, filename=filename)
                        logger.info(f"Saved output_iteration filename {filename} for CalibrationRun {calibration_run.id}")
                    break


# Function to process a single metrics row
def process_metrics_row_for_calibration(
        iteration: Iteration,
        metrics_row: dict[str, object],
        metrics_to_create: list[IterationMetric],
        job_description: str
) -> None:
    """
    Process a single row from the metrics file and create IterationMetric objects.

     NOTE ON NUMERIC VALUES
    ----------------------
    This function does not sanitize numeric values; it stores what it is given.
    Any JSON-safety normalization (NaN/±Inf -> None) is done later at API response time.

    :param iteration: The Iteration object for the current iteration.
    :param metrics_row: The row of metrics data from the file.
    :param metrics_to_create: The list to accumulate created IterationMetric objects.
    :param job_description: Job identifier string used for logging.
    """
    # Get rid of 'iteration' and 'objFunVal' columns
    metrics_row = {k: v for k, v in metrics_row.items() if k not in ['iteration', 'objFunVal']}

    for metric_name, value in metrics_row.items():
        # Perform case-insensitive lookup for the metric
        metric = MetricEnum.get_instance(metric_name.lower())

        if not metric:
            raise CerfException(f"Could not find metric '{metric_name}'")

        metric_value = to_float_or_nan(value)

        metric_obj = IterationMetric(
            iteration=iteration,
            metric=metric,
            metric_value=metric_value
        )
        logger.debug(f'{job_description}: Creating Iteration metric for {metric_obj}')
        metrics_to_create.append(metric_obj)


# Function to process a single parameters row
def process_params_row(
        iteration: Iteration,
        params_row: dict[str, float],
        params_to_create: list[IterationParameter],
        params_lookup: dict[str, CalibrationParameter],
        job_description: str
) -> None:
    """
    Create IterationParameter objects for a single iteration.

    This function:
    - Converts one row from the parameters CSV into IterationParameter records
    - Performs case-insensitive parameter lookup
    - Buffers objects for bulk_create

    Best-iteration selection is handled in process_iterations_for_a_worker().

    NOTE ON NUMERIC VALUES
    ----------------------
    This function does not sanitize numeric values; it stores what it is given.
    Any JSON-safety normalization (NaN/±Inf -> None) is done later at API response time.

    :param iteration: The Iteration object for the current iteration.
    :param params_row: The row of parameter data from the file (excluding 'iteration').
    :param params_to_create: Accumulator list for IterationParameter objects.
    :param params_lookup: Mapping of lowercased parameter names to CalibrationParameter objects.
    :param job_description: Job identifier string used for logging.
    """

    # ----------------------------------------------------------------------
    # Create IterationParameter objects
    # ----------------------------------------------------------------------
    for param_name, tuned_value in params_row.items():
        # Perform case-insensitive lookup for the parameter
        parameter = params_lookup.get(param_name.lower())
        if not parameter:
            raise CerfException(f"Could not find parameter '{param_name}' referenced in params_iteration_file")

        param_obj = IterationParameter(
            iteration=iteration,
            calibration_parameter=parameter,
            tuned_value=tuned_value
        )
        logger.debug(
            f"{job_description}: Creating IterationParameter "
            f"for iteration={iteration.iteration_num}, param={parameter.name}, value={tuned_value}"
        )

        params_to_create.append(param_obj)


def update_objective_function_values(metrics_iteration_file: str, calibration_run: CalibrationRun, worker_name: str) -> None:
    """
    Updates the objective function values for each iteration of a given worker in a calibration run.

    :param metrics_iteration_file: The file path to the CSV metrics file containing iteration numbers and objective function values.
    :param calibration_run: The CalibrationRun instance to which the iterations belong.
    :param worker_name: The name of the worker whose iterations are being updated.
    """
    iterations_dict = {
        iteration_num: Iteration(id=iteration_id, objective_function_value=None)  # Initialize without value
        for iteration_id, iteration_num in Iteration.objects.filter(
            calibration_run_id=calibration_run.id,
            worker_name=worker_name
        ).values_list("id", "iteration_num")
    }

    # Read the metrics file using pandas
    metrics_df = pd.read_csv(metrics_iteration_file, usecols=["iteration", "objFunVal"])

    iterations_to_update = []  # List to accumulate iterations to update

    # Iterate over rows in the DataFrame
    for _, row in metrics_df.iterrows():
        iteration_num = int(row['iteration'])  # type: ignore[arg-type]
        obj_fun_val = row['objFunVal']

        # Retrieve the iteration object from the dictionary
        iteration = iterations_dict.get(iteration_num)
        if not iteration:
            raise CerfException(
                f"Cannot find Iteration object for calibration run {calibration_run.id}, "
                f"worker {worker_name}, iteration {iteration_num}. Ngen-cal did not report this iteration"
            )

        logger.debug(
            f'{calibration_run.id}_{calibration_run.owner.username} Updating iteration {iteration_num} '
            f'for worker {worker_name} with output variable value {obj_fun_val}'
        )
        iteration.objective_function_value = obj_fun_val

        # Add the modified object to the list
        iterations_to_update.append(iteration)

    # Perform a bulk update for all iterations in chunks if there are any updates
    if iterations_to_update:
        with transaction.atomic():  # Ensure atomicity of the bulk update
            for i in range(0, len(iterations_to_update), BULK_CREATE_BATCH_SIZE):
                Iteration.objects.bulk_update(
                    iterations_to_update[i:i + BULK_CREATE_BATCH_SIZE], ['objective_function_value']
                )


# Function to read the last line of a file
def read_last_line(filename: str) -> str:
    """
    Reads and returns the last line of a file.

    :param filename: The path to the file.
    :return: The last line of the file as a string.
    """
    with open(filename, 'r', encoding='utf-8') as file:
        return deque(file, maxlen=1).pop().strip()


# Function to count the number of rows in a CSV file
def count_rows_in_csv(file_path: str) -> int:
    """
    Counts the number of rows in a CSV file, excluding the header.

    :param file_path: The path to the CSV file.
    :return: The number of rows in the CSV file (excluding the header).
    """
    with open(file_path, 'r') as file:
        # Count the lines and subtract 1 for the header
        return sum(1 for _ in file) - 1


def params_match_best(params_row: dict[str, float], best_params_dict: dict[str, float]) -> bool:
    """
    Determine whether a row of tuned parameters exactly matches the known global-best parameters.

    Rules:
    - Used only for GWO/PSO jobs. DDS never uses parameter matching.
    - Parameter names must match exactly (case-insensitive).
    - Value comparison:
        * NaN matches NaN
        * +Inf matches +Inf
        * -Inf matches -Inf
        * otherwise values must match within math.isclose tolerance.
    """
    # If we have no global best params (DDS or missing file), never match.
    if not best_params_dict:
        return False

    # Normalize keys to avoid CSV header casing mismatches.
    row = {k.lower(): v for k, v in params_row.items()}
    best = {k.lower(): v for k, v in best_params_dict.items()}

    # Strict: same parameter set size
    if len(row) != len(best):
        return False

    # Strict: same parameter names
    if row.keys() != best.keys():
        return False

    # For each global-best parameter:
    # - Require it to exist in the row
    # - Require its value to match (None must match None; floats within tolerance)
    for name, b in best.items():
        v = row[name]

        # NaN matches NaN only
        if math.isnan(v) or math.isnan(b):
            if math.isnan(v) and math.isnan(b):
                continue
            return False

        # +Inf/-Inf must match exactly (including sign)
        if math.isinf(v) or math.isinf(b):
            if v == b:
                continue
            return False

        # Normal finite float compare
        if not math.isclose(v, b, rel_tol=1e-9, abs_tol=0.0):
            return False

    return True


def _write_observed_hindcast_file(run) -> None:
    """
    Fetch observed streamflow for the hindcast window and write it to disk.

    If Data Services does not have the full requested window, no file is written.
    """
    # Build the full hindcast output window in UTC.
    time_start = run.cycle_date
    time_end = run.cycle_date + timedelta(
        hours=run.interval_cycle * (run.num_iterations - 1) + run.configuration.fcst_win
    )

    # Fetch observed data for the same UTC window used by the hindcast output.
    date_time_range = DateTimeRange(time_start, time_end)

    # Verify that Data Services has observations for the requested hindcast window.
    available_date_range = get_observational_date_range_from_data_services(run.calibration_run)

    available_start = available_date_range.start_datetime
    available_end = available_date_range.end_datetime
    requested_start = date_time_range.start_datetime
    requested_end = date_time_range.end_datetime

    # These should always be set if Data Services behaved correctly
    assert available_start is not None and available_end is not None
    assert requested_start is not None and requested_end is not None

    observations_available = (
            available_start <= requested_start
            and requested_end <= available_end
    )

    if not observations_available:
        logger.warning(
            "Observed streamflow is not available for the full hindcast window. "
            f"Requested: {requested_start} to {requested_end}. "
            f"Available: {available_start} to {available_end}."
        )
        return None

    obs_path = get_observational_file_for_hindcast(run)

    # Fetch observed streamflow only when Data Services has the full requested range.
    obs_csv = get_observational_data_from_data_services(run.calibration_run, date_time_range)

    with open(obs_path, "w", encoding="utf-8", newline="") as f:
        f.write(obs_csv)

    logger.info(
        f"Wrote observed hindcast data to {obs_path} "
        f"for {requested_start} to {requested_end}"
    )

    return None
