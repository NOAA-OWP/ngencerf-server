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
    calibration_end_period = models.DateTimeField(null=True)
    calibration_eval_start_period = models.DateTimeField(null=True)
    calibration_eval_end_period = models.DateTimeField(null=True)
    validation_start_period = models.DateTimeField(null=True)
    validation_end_period = models.DateTimeField(null=True)
    validation_eval_start_period = models.DateTimeField(null=True)
    validation_eval_end_period = models.DateTimeField(null=True)
    use_sloth = models.BooleanField(null=False, default=False)
    streamflow_threshold = models.FloatField(null=True)
    peak_flow_threshold = models.FloatField(null=True)
    geopackage_source = models.ForeignKey('GeopackageSource', null=True, on_delete=models.RESTRICT)
    geopackage_eds_file_path = models.TextField(null=True)
    forcing_source_requested = models.ForeignKey('ForcingSource', null=True, on_delete=models.RESTRICT, related_name='+', related_query_name='+')
    forcing_source_actual = models.ForeignKey('ForcingSource', null=True, on_delete=models.RESTRICT, related_name='+', related_query_name='+')
    forcing_eds_dir_path = models.TextField(null=True)
    observational_source = models.ForeignKey('ObservationalSource', null=True, on_delete=models.RESTRICT)
    observational_eds_file_path = models.TextField(null=True)
    user_parameter_filename = models.TextField(null=True)
    realization_file_path = models.TextField(null=True)
    status = models.ForeignKey('Status', null=False, on_delete=models.RESTRICT, db_index=True)
    user_formulation_name = models.CharField(max_length=100, null=True)
    is_aet_rootzone = models.BooleanField(null=False, default=False)
    save_plot_iteration_frequency = models.PositiveIntegerField(null=True)
    save_output_iteration = models.BooleanField(default=False)
    automatic_validation = models.BooleanField(null=False, default=False)
    is_archived = models.BooleanField(default=False)
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
            f"geopackage_eds_file_path: {self.geopackage_eds_file_path}, "
            f"forcing_eds_dir_path: {self.forcing_eds_dir_path}, "
            f"observational_eds_file_path: {self.observational_eds_file_path}, "
            f"objective_function.name: {self.objective_function.name if self.objective_function else 'None'}, "
            f"optimization.name: {self.optimization.name if self.optimization else 'None'}, "
            f"status.name: {self.status.name}, "
            f"is_archived: {self.is_archived}"
        )
