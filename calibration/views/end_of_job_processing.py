import csv
import logging
import math
import os
import traceback
from collections import deque
from datetime import timedelta
from itertools import groupby
from operator import attrgetter
from typing import Dict

import pandas as pd
from django.db import transaction
from django.utils.timezone import now

from calibration.enums import OptimizationEnum, ValidationMetricPeriod, ValidationType, MetricEnum
from calibration.models import Iteration, CalibrationRun, IterationMetric, IterationParameter, CalibrationParameter, ValidationRun, \
    PerformanceMetrics, ValidationMetrics, NWMRetrospectiveMetrics, IterationResult, ForecastRun, ColdStartRun
from calibration.models.base_run import BaseRun
from calibration.util.caching import have_LSTM
from calibration.util.ngen_locations import get_realization_file_path, get_metrics_iteration_file, \
    get_objective_log_best_file, get_calibration_worker_path, get_global_best_params_file, get_validation_metrics_valid_control_file, \
    get_validation_metrics_valid_best_file, get_validation_metrics_valid_iteration_file, \
    get_validation_performance_file, get_calibration_performance_file, get_validation_metrics_nwm_retrospective_file, get_output_iteration_csv, \
    get_validation_special_performance_file, get_forecast_forcing_download_performance_file, \
    get_forecast_performance_file, get_params_iteration_file
from calibration.views.calibration_secondary_data_views import generate_swe_ts_data, generate_soil_moisture_ts_data
from calibration.views.common import CerfException, get_job_description, find_validation_worker_with_matching_id

logger = logging.getLogger(__name__)

BULK_CREATE_BATCH_SIZE = 1000  # Define a reasonable batch size


def read_validation_output(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Processes the output of a validation run by identifying the correct worker, retrieving performance metrics,
    and updating the validation run's attributes.

    :param validation_run: The ValidationRun instance.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(validation_run)

    logger.info(f"Processing output for {job_description}, status: {validation_run.status}")

    # --- Moved this read-only query OUTSIDE the transaction to reduce lock contention ---
    validation_type = ValidationType(validation_run.validation_type)
    iteration = validation_run.iteration if validation_type == ValidationType.VALID_ITERATION else None

    with transaction.atomic():
        # Identify the matching worker based on validation type
        matching_worker = find_validation_worker_with_matching_id(
            validation_run,
            worker_name=iteration.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
            iteration_num=iteration.iteration_num if validation_type == ValidationType.VALID_ITERATION else None,
        )
        validation_run.validation_worker_name = matching_worker
        validation_run.save(update_fields=['validation_worker_name'])

        performance_metrics_file = (
            get_validation_performance_file(validation_run.calibration_run, iteration.worker_name, iteration.iteration_num)
            if validation_type == ValidationType.VALID_ITERATION
            else get_validation_special_performance_file(validation_run.calibration_run, validation_type)
        )

        create_performance_metrics(validation_run, performance_metrics_file)

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

    with transaction.atomic():
        performance_metrics_file = get_calibration_performance_file(calibration_run)
        create_performance_metrics(calibration_run, performance_metrics_file)
        calibration_run.save(update_fields=['performance_metrics', 'run_start'])

        if IterationMetric.objects.filter(iteration__calibration_run=calibration_run).exists():
            raise CerfException(f"End of job processing has already been completed for {job_description}")

        if not failed_so_far:
            # Set the realization file path for the run
            calibration_run.realization_file_path = get_realization_file_path(calibration_run)
            calibration_run.save(update_fields=['realization_file_path'])

            process_iterations_for_all_workers(calibration_run)

    logger.info(f"End of processing output for {job_description}")


def read_cold_start_output(run: ColdStartRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a forecast run by parsing performance metrics.

    :param run: The ColdStartRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """

    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")
    with transaction.atomic():
        create_performance_metrics(run, get_cold_start_performance_file(run))

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_forecast_output(run: ForecastRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a forecast run by parsing performance metrics.

    :param run: The ForecastRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """

    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")
    with transaction.atomic():
        create_performance_metrics(run, get_forecast_performance_file(run))

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def create_performance_metrics(run: BaseRun, performance_metrics_file: str) -> None:
    """
    Parses performance metrics from a file and updates the run with the metrics.

    :param run: The run instance (CalibrationRun, ValidationRun, or similar).
    :param performance_metrics_file: Path to the performance metrics file.
    :return: None
    """
    performance_metrics = parse_performance_metrics(performance_metrics_file)

    if not performance_metrics:
        # Fallback to calculate elapsed_time manually
        elapsed_time = now() - run.run_start
        performance_metrics = PerformanceMetrics.objects.create(elapsed_time=elapsed_time)

    run.performance_metrics = performance_metrics
    run.save(update_fields=['performance_metrics', 'run_start'])


def process_validation_metrics(run: ValidationRun | CalibrationRun, metrics_file: str, expected_run_type: str) -> None:
    """
    Generic function to process validation or calibration metrics from a CSV file and create corresponding Metric objects.

    :param run: The ValidationRun or CalibrationRun instance.
    :param metrics_file: The file path of the metrics CSV file.
    :param expected_run_type: The expected run type to validate.
    :return: None
    """

    job_description = get_job_description(run)
    logger.info(f"Processing '{metrics_file} for {job_description}")

    # Check if the file exists
    if not os.path.isfile(metrics_file):
        logger.error(f'{metrics_file} does not exist')
        return

    # Read the metrics file using pandas
    metrics_df = pd.read_csv(metrics_file)

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
            # Perform case-insensitive lookup for the metric
            metric = MetricEnum.get_instance(str(metric_name))
            if not metric:
                raise CerfException(f"Could not find metric '{metric_name}' in MetricEnum")

            metric_value = float(value) if value is not None else float('nan')

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
        MetricModel.objects.bulk_create(metrics_to_create, batch_size=BULK_CREATE_BATCH_SIZE)


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

    if ValidationMetrics.objects.filter(validation_run=validation_run, run_type=expected_run_type).exists():
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

    logger.info('Generating SWE timeseries data')
    try:
        generate_swe_ts_data(validation_run)
    except Exception as e:
        logger.error(f'Failed to generate SWE timeseries data: {e}')
        traceback.print_exc()

    logger.info('Generating Soil Moisture timeseries data')
    try:
        generate_soil_moisture_ts_data(validation_run)
    except Exception as e:
        logger.error(f'Failed to generate Soil Moisture timeseries data: {e}')
        traceback.print_exc()


def process_iterations_for_all_workers(calibration_run: CalibrationRun) -> None:
    """
    Process all Iteration objects for the workers of a given CalibrationRun.
    It uses prefetching to optimize database queries and processes the iterations
    for each worker based on their metrics and parameters.

    :param calibration_run: The CalibrationRun instance.
    """
    # Compute best_params_dict once, outside the loop
    best_params_dict: Dict[str, float] = {}

    have_LSTM_flag = have_LSTM(calibration_run)
    if calibration_run.optimization != OptimizationEnum.DDS.db_instance:
        if not have_LSTM_flag:
            global_best_params_file = get_global_best_params_file(calibration_run)
            if not os.path.isfile(global_best_params_file):
                raise CerfException(f"{global_best_params_file} does not exist")

            # Read the global best parameters into a dictionary
            df = pd.read_csv(global_best_params_file, names=['value', 'name', 'model'], skiprows=1)
            best_params_dict = pd.Series(df['value'].astype(float).values, index=df['name']).to_dict()

    # Query all Iteration objects for the calibration run and prefetch related metrics and parameters
    iterations = Iteration.objects.filter(calibration_run=calibration_run).order_by(
        'worker_name', 'iteration_num'
    ).prefetch_related('iterationmetric_set', 'iterationparameter_set')

    # Group the iterations by worker and process them
    for worker_name, worker_iterations in groupby(iterations, key=attrgetter('worker_name')):
        process_iterations_for_a_worker(calibration_run, worker_name, list(worker_iterations), best_params_dict, have_LSTM_flag)

    # Raise an error if no best iteration was found
    if not have_LSTM_flag and not Iteration.objects.filter(calibration_run=calibration_run, best_params=True).exists():
        raise CerfException(f"No best iteration was found for CalibrationRun {calibration_run.id}")


# Function to process iterations for a specific worker
def process_iterations_for_a_worker(calibration_run: CalibrationRun, worker_name: str, iterations: list[Iteration],
                                    best_params_dict: Dict[str, float], have_LSTM_flag: bool) -> None:
    """
    Process all iterations for a specific worker in a CalibrationRun.
    It reads the metrics and parameters files for the worker and processes each
    iteration for metrics and parameters creation.

    :param calibration_run: The CalibrationRun instance.
    :param worker_name: The name of the worker. This is the middle part of the worker name.
                        Need to prefix with ngen_ and suffix with _worker.
    :param iterations: A list of Iteration objects for the worker.
    :param best_params_dict: Precomputed dictionary of best parameters for comparison.
    :param have_LSTM_flag: Flag to indicate whether this job has LSTM
    """
    logger.info(f"Processing iterations for {worker_name} for Calibration Job {calibration_run.id}")

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

    metrics_to_create = []  # List to accumulate metrics to be created
    params_to_create = []  # List to accumulate parameters to be created

    # Track whether a best iteration was set
    best_iteration_found = False

    # Process metrics file
    metrics_df = pd.read_csv(metrics_iteration_file)
    if not have_LSTM_flag:
        update_objective_function_values(metrics_iteration_file, calibration_run, worker_name)

    for _, row in metrics_df.iterrows():
        row_dict = row.to_dict()
        iteration_num = row_dict['iteration']
        iteration = iteration_dict.get(iteration_num)
        if not iteration:
            raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")
        process_metrics_row_for_calibration(calibration_run, iteration, row_dict, metrics_to_create)

        if have_LSTM_flag:
            # For LSTM, there is only 1 iteration so we will mark it as having the best
            iteration.best_params = True
            iteration.save(update_fields=['best_params'])

    # Process parameters file
    if not have_LSTM_flag:
        params_df = pd.read_csv(params_iteration_file)

        for _, row in params_df.iterrows():
            row_dict = row.to_dict()
            iteration_num = row_dict['iteration']
            iteration = iteration_dict.get(iteration_num)
            if not iteration:
                raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")
            process_params_row(calibration_run, iteration, row_dict, params_to_create, best_iteration_for_worker, best_params_dict)

            # Check if this iteration was set as the best
            if iteration.best_params:
                best_iteration_found = True

    # Bulk create IterationMetric and IterationParameter objects in chunks
    if metrics_to_create:
        for i in range(0, len(metrics_to_create), BULK_CREATE_BATCH_SIZE):
            batch = metrics_to_create[i:i + BULK_CREATE_BATCH_SIZE]
            try:
                IterationMetric.objects.bulk_create(batch)
            except Exception as e:
                logger.error(f"Error inserting IterationMetric batch {i // BULK_CREATE_BATCH_SIZE + 1}: {e}")
                for metric in batch:
                    logger.error(
                        f"Failed IterationMetric: Iteration {metric.iteration.iteration_num}, Metric {metric.metric}, Value {metric.metric_value}")
                raise  # Re-raise exception after logging details

    if params_to_create:
        for i in range(0, len(params_to_create), BULK_CREATE_BATCH_SIZE):
            batch = params_to_create[i:i + BULK_CREATE_BATCH_SIZE]
            try:
                IterationParameter.objects.bulk_create(batch)
            except Exception as e:
                logger.error(f"Error inserting IterationParameter batch {i // BULK_CREATE_BATCH_SIZE + 1}: {e}")
                for param in batch:
                    logger.error(
                        f"Failed IterationParameter: Iteration {param.iteration.iteration_num}, Parameter {param.calibration_parameter.name}, Value {param.tuned_value}")
                raise  # Re-raise exception after logging details

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
def process_metrics_row_for_calibration(calibration_run: CalibrationRun,
                                        iteration: Iteration,
                                        metrics_row: dict[str, float | None],
                                        metrics_to_create: list[IterationMetric]) -> None:
    """
    Process a single row from the metrics file and create IterationMetric objects.

    :param calibration_run: The CalibrationRun instance.
    :param iteration: The Iteration object for the current iteration.
    :param metrics_row: The row of metrics data from the file.
    :param metrics_to_create: The list to accumulate created IterationMetric objects.
    """
    job_description = get_job_description(calibration_run)

    # Get rid of 'iteration' and 'objFunVal' columns
    metrics_row = {k: v for k, v in metrics_row.items() if k not in ['iteration', 'objFunVal']}

    for metric_name, value in metrics_row.items():
        # Perform case-insensitive lookup for the metric
        metric = MetricEnum.get_instance(metric_name.lower())

        if not metric:
            raise CerfException(f"Could not find metric '{metric_name}'")

        # Set metric_value to NaN if missing
        metric_value = float(value) if value is not None else float('nan')

        metric_obj = IterationMetric(
            iteration=iteration,
            metric=metric,
            metric_value=metric_value
        )
        logger.debug(f'{job_description}: Creating Iteration metric for {metric_obj}')
        metrics_to_create.append(metric_obj)


# Function to process a single parameters row
def process_params_row(calibration_run: CalibrationRun,
                       iteration: Iteration,
                       params_row: dict[str, float | None],
                       params_to_create: list[IterationParameter],
                       best_iteration_for_worker: int,
                       best_params_dict: Dict[str, float]) -> None:
    """
    Process a single row from the parameters file and create IterationParameter objects.
    Determine if the iteration represents the best set of parameters and set the `best_params` flag on the Iteration.

    :param calibration_run: The CalibrationRun instance.
    :param iteration: The Iteration object for the current iteration.
    :param params_row: The row of parameter data from the file.
    :param params_to_create: The list to accumulate created IterationParameter objects.
    :param best_iteration_for_worker: The best iteration number for the worker, used to mark the best parameters.
    :param best_params_dict: Precomputed dictionary of best parameters for comparison.
    """
    job_description = get_job_description(calibration_run)

    # Filter out the 'iteration' column
    params_row = {k: v for k, v in params_row.items() if k != 'iteration'}

    # Check if the current params_row matches the global best parameters
    # The is_best_match logic is done for PSO and GWO.  We actually compare the values of the parameters
    is_best_match = (
            len(params_row) == len(best_params_dict) and
            all(param_name in best_params_dict and math.isclose(float(value), best_params_dict[param_name], rel_tol=1e-9, abs_tol=0.0)
                for param_name, value in params_row.items())
    )

    # If the iteration is the best (based on matching parameters or best iteration number (from objective_log file for DDS))
    if is_best_match or iteration.iteration_num == best_iteration_for_worker:
        logger.debug(f'{calibration_run.id}_{calibration_run.owner.username} Found best iteration: {iteration.iteration_num}, for {job_description}')
        iteration.best_params = True
    else:
        iteration.best_params = False

    # Save the iteration after setting the best_params flag
    iteration.save(update_fields=['best_params'])

    # Prefetch CalibrationParameter objects for quick lookup
    params_lookup = {p.name.lower(): p for p in CalibrationParameter.objects.all()}

    for param_name, value in params_row.items():
        # Perform case-insensitive lookup for the parameter
        parameter = params_lookup.get(param_name.lower())
        if not parameter:
            raise CerfException(f"Could not find parameter '{param_name}' referenced in params_iteration_file")

        tuned_value = float(value) if value is not None else None
        param_obj = IterationParameter(
            iteration=iteration,
            calibration_parameter=parameter,
            tuned_value=tuned_value
        )
        logger.debug(f'{job_description}: Creating Iteration parameter for {param_obj}')
        params_to_create.append(param_obj)


def update_objective_function_values(metrics_iteration_file: str, calibration_run: CalibrationRun, worker_name: str) -> None:
    """
    Updates the objective function values for each iteration of a given worker in a calibration run.

    :param metrics_iteration_file: The file path to the CSV metrics file containing iteration numbers and objective function values.
    :param calibration_run: The CalibrationRun instance to which the iterations belong.
    :param worker_name: The name of the worker whose iterations are being updated.
    """
    # Fetch only the fields needed using .values_list()
    iterations_dict = {
        iteration_num: Iteration(id=iteration_id, objective_function_value=None)  # Initialize without value
        for iteration_id, iteration_num in Iteration.objects.filter(
            calibration_run=calibration_run, worker_name=worker_name
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
                f"Cannot find Iteration object for calibration run {calibration_run.id}, worker {worker_name}, iteration {iteration_num}. Ngen-cal did not report this iteration")

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


def parse_duration(duration_str: str | None) -> timedelta | None:
    """
    Converts a duration string (D-HH:MM:SS or HH:MM:SS) into a timedelta object.

    :param duration_str: The duration string in D-HH:MM:SS or HH:MM:SS format.
    :return: A timedelta object representing the duration.
    """
    if not duration_str:
        return None
    try:
        if '-' in duration_str:
            days_part, time_part = duration_str.split('-')
            days = int(days_part)
        else:
            time_part = duration_str
            days = 0
        hours, minutes, seconds = map(int, time_part.split(':'))
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except (ValueError, TypeError):
        return None


def parse_size_to_kb(size_str: str | None) -> float | None:
    """
    Converts a size string (e.g., '123K', '1.5M', '2G') to a float in kilobytes.

    :param size_str: The size string with an optional unit suffix (K, M, G).
    :return: The size converted to kilobytes or None if invalid.
    """

    if not size_str:
        return None

    size_str = size_str.strip().upper()
    try:
        if size_str.endswith('K'):
            return float(size_str[:-1])
        elif size_str.endswith('M'):
            return float(size_str[:-1]) * 1024
        elif size_str.endswith('G'):
            return float(size_str[:-1]) * 1024 ** 2
        else:
            # Assume no unit means it's already in KB
            return float(size_str)
    except ValueError:
        return None


def parse_performance_metrics(file_path: str) -> PerformanceMetrics | None:
    """
    Opens the pipe-delimited file, parses the content, and extracts performance metrics to save to the database.

    This function assumes:
    - `MaxRSS`, `MaxDiskRead`, and `MaxDiskWrite` fields are only present in the `.batch` job line.
    - `Planned` (previously called `Reserved`) is only present in the non-batch job line.

    The function:
    - Logs a warning if any expected field is missing.
    - Extracts job performance details from batch and non-batch job records.
    - Returns a `PerformanceMetrics` instance populated with parsed values.

    :param file_path: The path to the performance metrics file.
    :return: A `PerformanceMetrics` object with extracted metrics, or `None` if the file is missing or invalid.
    """
    if not os.path.exists(file_path):
        logger.error(f'Performance metrics file {file_path} not found')
        return None
    else:
        logger.info(f'Reading performance metrics from {file_path}')

    reserved_time = None
    batch_metrics = None

    with open(file_path, 'r') as file:
        reader = csv.DictReader(file, delimiter='|')

        for row in reader:
            job_id = row['JobID']

            if job_id.endswith('.batch'):
                # Expected fields only for the .batch line
                expected_batch_fields = ['Elapsed', 'NCPUS', 'CPUTime', 'MaxRSS', 'MaxDiskRead', 'MaxDiskWrite']
                missing_fields = [field for field in expected_batch_fields if not row.get(field)]
                if missing_fields:
                    logger.warning(f'Missing fields for batch JobID {job_id}: {", ".join(missing_fields)}')

                # Collect data from the .batch line with fallback to None for missing fields
                batch_metrics = {
                    'slurm_job_id': job_id,
                    'elapsed_time': parse_duration(row.get('Elapsed')),
                    'num_cpus': int(row.get('NCPUS')) if row.get('NCPUS') else None,
                    'cpu_time': parse_duration(row.get('CPUTime')),
                    'max_rss': parse_size_to_kb(row.get('MaxRSS')),
                    'max_disk_read': parse_size_to_kb(row.get('MaxDiskRead')),
                    'max_disk_write': parse_size_to_kb(row.get('MaxDiskWrite')),
                    'reserved_time': reserved_time  # This will be updated later if available
                }
            else:
                # Expected fields only for the non-batch line
                expected_non_batch_fields = ['Elapsed', 'NCPUS', 'CPUTime', 'Planned']
                missing_fields = [field for field in expected_non_batch_fields if not row.get(field)]
                if missing_fields:
                    logger.warning(f'Missing fields for non-batch JobID {job_id}: {", ".join(missing_fields)}')

                # Save the reserved time from the non-.batch line
                reserved_time = parse_duration(row.get('Planned'))

    if batch_metrics:
        # Update the reserved_time for the batch metrics
        batch_metrics['reserved_time'] = reserved_time

        # Create or update the PerformanceMetrics record
        metrics = PerformanceMetrics.objects.create(**batch_metrics)
        return metrics

    return None
