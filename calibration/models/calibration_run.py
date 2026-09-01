from datetime import datetime

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import models

from calibration.models.base_run import BaseRun


class CalibrationRun(BaseRun):  # Inherit from BaseRun
    is_active = models.BooleanField(null=False, default=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=False, on_delete=models.RESTRICT, db_index=True)
    gage = models.ForeignKey('Gage', null=True, on_delete=models.RESTRICT)
    job_genesis = models.CharField(max_length=20, null=False)
    job_data_dir = models.CharField(max_length=255, null=False)
    optimization = models.ForeignKey('Optimization', null=True, on_delete=models.RESTRICT)
    objective_function = models.ForeignKey('Metric', null=True, on_delete=models.RESTRICT)
    time_range_start = models.DateTimeField(null=True)
    time_range_end = models.DateTimeField(null=True)
    calibration_start_period = models.DateTimeField(null=True)
    warmup_duration = models.IntegerField(null=True)
    calibration_duration = models.IntegerField(null=True)
    validation_window_gap = models.IntegerField(null=True)
    validation_window_after_calibration = models.BooleanField(null=True, default=True)
    validation_duration = models.IntegerField(null=True)
    use_sloth = models.BooleanField(null=False, default=False)
    threshold_categorical = models.FloatField(null=True)
    threshold_event = models.FloatField(null=True)
    geopackage_source = models.ForeignKey('GeopackageSource', null=True, on_delete=models.RESTRICT)
    forcing_source = models.ForeignKey('ForcingSource', null=True, on_delete=models.RESTRICT, related_name='+', related_query_name='+')
    observational_source = models.ForeignKey('ObservationalSource', null=True, on_delete=models.RESTRICT)
    realization_file_path = models.TextField(null=True)
    status = models.ForeignKey('Status', null=False, on_delete=models.RESTRICT, db_index=True)
    job_name = models.CharField(max_length=100, null=True)
    save_plot_iteration_frequency = models.PositiveIntegerField(null=True)
    save_output_iteration = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    archive_status_updated_at = models.DateTimeField(null=True)
    is_locked = models.BooleanField(default=False)
    mpi_nprocs = models.IntegerField(null=True)
    next_worker_number = models.PositiveIntegerField(default=1)
    num_catchments = models.IntegerField(null=True)
    node_type = models.CharField(max_length=20, null=True)

    class Meta:
        db_table = 'calibration_run'
        indexes = [
            models.Index(fields=['owner', 'status', 'is_archived'], name='idx_run_owner_status_archived'),
            models.Index(fields=['owner', 'is_archived'], name='idx_run_owner_archived'),
            models.Index(fields=['owner', 'is_archived', 'status', '-id'], name='idx_run_owner_arch_stat_id'),
            models.Index(fields=['owner', 'is_archived', 'created_at'], name='idx_run_owner_arch_created'),
        ]

    def __str__(self):
        gage_info = f"Gage: {self.gage.gage_id}" if self.gage else "No Gage"
        return (
            f"CalibrationRun {self.id}, {gage_info}, "
            f"owner: {self.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"job_data_directory: {self.job_data_dir},"
            f"objective_function.name: {self.objective_function.name if self.objective_function else 'None'}, "
            f"optimization.name: {self.optimization.name if self.optimization else 'None'}, "
            f"status.name: {self.status.name}, "
            f"is_archived: {self.is_archived}"
        )

    @property
    def calibration_end_period(self) -> datetime | None:
        # Calibration simulation ends when calibration evaluation ends, at 23:00.
        return self.calibration_eval_end_period

    @property
    def calibration_eval_start_period(self) -> datetime | None:
        # Calibration evaluation starts at 00:00 after the warmup period.
        if self.calibration_start_period is None or self.warmup_duration is None:
            return None

        return relativedelta(
            months=self.warmup_duration
        ) + self.calibration_start_period

    @property
    def calibration_eval_end_period(self) -> datetime | None:
        # Calibration evaluation ends at 23:00 after calibration_duration months.
        if (
                self.calibration_start_period is None
                or self.warmup_duration is None
                or self.calibration_duration is None
        ):
            return None

        start = relativedelta(
            months=self.warmup_duration
        ) + self.calibration_start_period

        return relativedelta(
            months=self.calibration_duration,
            hours=-1,
        ) + start

    @property
    def validation_start_period(self) -> datetime | None:
        if self.calibration_start_period is None:
            return None

        if self.validation_window_after_calibration:
            # Calibration and validation simulations both start at 00:00
            # on the calibration start date.
            return self.calibration_start_period

        # When validation precedes calibration, the validation simulation starts
        # at 00:00, warmup_duration months before validation evaluation begins.
        validation_eval_start = self.validation_eval_start_period
        if validation_eval_start is None or self.warmup_duration is None:
            return None

        return relativedelta(
            months=-self.warmup_duration
        ) + validation_eval_start

    @property
    def validation_end_period(self) -> datetime | None:
        if self.validation_window_after_calibration:
            # When validation period follows calibration, the simulation ends when
            # validation evaluation ends, at 23:00.
            return self.validation_eval_end_period

        # When validation period precedes calibration, the simulation continues through
        # calibration evaluation and ends at 23:00.
        return self.calibration_eval_end_period

    @property
    def validation_eval_start_period(self) -> datetime | None:
        if self.calibration_start_period is None:
            return None

        if self.validation_window_after_calibration:
            # Validation evaluation starts at 00:00 after the configured gap
            # following calibration evaluation.
            if self.validation_window_gap is None:
                return None

            calibration_eval_end = self.calibration_eval_end_period
            if calibration_eval_end is None:
                return None

            return relativedelta(
                hours=1,
                months=self.validation_window_gap,
            ) + calibration_eval_end

        # When validation precedes calibration, validation evaluation starts at
        # 00:00 validation_duration months before its end, with the configured
        # gap between validation and calibration evaluation.
        if (
                self.validation_duration is None
                or self.validation_window_gap is None
        ):
            return None

        calibration_eval_start = self.calibration_eval_start_period
        if calibration_eval_start is None:
            return None

        return relativedelta(
            months=-(self.validation_window_gap + self.validation_duration)
        ) + calibration_eval_start

    @property
    def validation_eval_end_period(self) -> datetime | None:
        if self.calibration_start_period is None:
            return None

        if self.validation_window_after_calibration:
            # Validation evaluation ends at 23:00 after validation_duration months.
            if self.validation_duration is None:
                return None

            validation_eval_start = self.validation_eval_start_period
            if validation_eval_start is None:
                return None

            return relativedelta(
                months=self.validation_duration,
                hours=-1,
            ) + validation_eval_start

        # When validation precedes calibration, validation evaluation ends at
        # 23:00 immediately before the configured gap begins.
        if self.validation_window_gap is None:
            return None

        calibration_eval_start = self.calibration_eval_start_period
        if calibration_eval_start is None:
            return None

        return relativedelta(
            months=-self.validation_window_gap,
            hours=-1,
        ) + calibration_eval_start
