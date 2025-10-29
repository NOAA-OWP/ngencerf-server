from django.core.validators import RegexValidator
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.fields import empty
from rest_framework.settings import api_settings

from calibration.enums import DataTypeEnum, UnitsEnum, LocationEnum, ForcingSourceEnum, ObservationalSourceEnum, DomainEnum, StatusEnum, \
    OptimizationEnum, GeopackageSourceEnum, SlurmStatusEnum, JobGenesis, PlotDefinitionsEnum, ForecastConfigEnum, LogCategory, LogName, NgenLogging, \
    CalibrationSortField, ForecastSortField
from calibration.util.caching import get_cached_modules_with_groups


class BaseSerializer(serializers.Serializer):
    def run_validation(self, data=None):
        if data is not None and data != empty:
            unknown = set(data) - set(self.fields)
            if unknown:
                errors = ["Unknown field: {}".format(f) for f in unknown]
                raise serializers.ValidationError({
                    api_settings.NON_FIELD_ERRORS_KEY: errors,
                })

        return super().run_validation(data)


def enum_validator(enum_class):
    """
    Validates if the value is a valid name or alias of the enum class, case-insensitively.
    Raises a fatal error if the enum class does not implement get_names() or get_all_valid_names().
    """

    def validate_enum(value):
        # Convert input value to lowercase for case-insensitive comparison
        original_value = value  # Store original value for error message
        value = value.lower()

        # Retrieve valid names, converting each to lowercase for case-insensitive comparison
        if hasattr(enum_class, 'get_all_valid_names'):
            # Enum with get_all_valid_names() method (typically from AbstractEnum)
            valid_names = [name.lower() for name in enum_class.get_all_valid_names()]
        elif hasattr(enum_class, 'get_names'):
            valid_names = [name.lower() for name in enum_class.get_names()]
        else:
            # Fatal configuration error – enum must define one of these methods
            raise RuntimeError(
                f"Enum class '{enum_class.__name__}' must define either "
                f"'get_names()' or 'get_all_valid_names()' to work with enum_validator()."
            )

        # Perform the actual validation
        if value not in valid_names:
            raise serializers.ValidationError(f"Invalid value '{original_value}'. This field must be one of {valid_names}.")

    return validate_enum


def no_space_validator(value):
    if ' ' in value:
        raise serializers.ValidationError("This field must not contain spaces.")


def greater_than_zero(value):
    if value <= 0:
        raise serializers.ValidationError("This field must be greater than 0.")


class EmptySerializer(BaseSerializer):
    pass


class GenericMessageResponseSerializer(BaseSerializer):
    message = serializers.CharField(required=True)


class GenericMessageWithIdResponseSerializer(GenericMessageResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)


class DataValidationResponseSerializer(GenericMessageResponseSerializer):
    data_validation_id = serializers.IntegerField(required=True)


class GenericMessageAndStatusResponseSerializer(GenericMessageResponseSerializer):
    message = serializers.CharField(required=True)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)


class GenericResponseSerializer(GenericMessageAndStatusResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)


class GenericResponseSerializerWithValidator(GenericMessageAndStatusResponseSerializer):
    validation_run_id = serializers.IntegerField(required=False)


class CalibrationRunSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)


class ColdStartRunSerializer(BaseSerializer):
    cold_start_run_id = serializers.IntegerField(required=True)


class ForecastRunSerializer(BaseSerializer):
    forecast_run_id = serializers.IntegerField(required=True)


class VerificationRunSerializer(BaseSerializer):
    verification_run_id = serializers.IntegerField(required=True)


class DeleteForecastRunResponseSerializer(GenericMessageResponseSerializer):
    forecast_run_id = serializers.IntegerField(required=True)


class CalibrationRunIdList(BaseSerializer):
    calibration_run_ids = serializers.ListSerializer(child=serializers.IntegerField(), required=True)


class GetStatusRequestSerializer(CalibrationRunSerializer):
    include_performance_metrics = serializers.BooleanField(required=False, default=False)


class GetStatusForComparisonRequestSerializer(CalibrationRunIdList):
    pass


class ValidationRunSerializer(BaseSerializer):
    validation_run_id = serializers.IntegerField(required=True)


# TODO Do we still need this after we've fully implemented Forecast
class CalibrationOrValidationRunSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, allow_null=True)
    validation_run_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, data):
        calibration_run_id = data.get('calibration_run_id')
        validation_run_id = data.get('validation_run_id')

        # Ensure that only one of them is specified
        if bool(calibration_run_id) == bool(validation_run_id):  # Both are specified or both are None
            raise serializers.ValidationError(
                "You must specify either 'calibration_run_id' or 'validation_run_id', but not both."
            )

        return data


class CalibrationOrValidationOrForecastOrVerificationRunSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, allow_null=False)
    validation_run_id = serializers.IntegerField(required=False, allow_null=False)
    forecast_run_id = serializers.IntegerField(required=False, allow_null=False)
    verification_job_id = serializers.IntegerField(required=False, allow_null=False)

    def validate(self, data):
        """
        Ensure that only one of calibration_run_id, validation_run_id, forecast_run_id, or verification_job_id is specified.
        """
        calibration_run_id = data.get('calibration_run_id')
        validation_run_id = data.get('validation_run_id')
        forecast_run_id = data.get('forecast_run_id')
        verification_job_id = data.get('verification_job_id')

        # Collect the IDs that are specified (non-null and non-zero values)
        specified_ids = [
            id_value
            for id_value in [calibration_run_id, validation_run_id, forecast_run_id, verification_job_id]
            if id_value is not None
        ]

        # Check that exactly one ID is specified
        if len(specified_ids) != 1:
            raise serializers.ValidationError(
                "You must specify exactly one of 'calibration_run_id', 'validation_run_id', 'forecast_run_id' or 'verification_job_id'."
            )

        return data


class CancelJobResponseSerializer(GenericMessageAndStatusResponseSerializer, CalibrationOrValidationOrForecastOrVerificationRunSerializer):
    def validate(self, data):
        # Call the parent validate method to include its logic
        return super().validate(data)


class CreateValidationRequestSerializer(CalibrationRunSerializer):
    iteration_id = serializers.IntegerField(required=True)


# TODO Fix these
class CreateColdStartRequestSerializer(CalibrationRunSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=False)


class CreateForecastRequestSerializer(CalibrationRunSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=True)


##################################
# Common serializers that need to be defined before usage
##################################
class SlothParameters(BaseSerializer):
    param_name = serializers.CharField(required=True, allow_blank=False)
    param_count = serializers.IntegerField(required=True)
    param_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    param_units = serializers.CharField(required=True, validators=[enum_validator(UnitsEnum)])
    param_location = serializers.CharField(required=True, validators=[enum_validator(LocationEnum)])
    param_value = serializers.FloatField(required=True)
    maps_to_module = serializers.CharField(required=True, allow_blank=False)
    maps_to_variable_name = serializers.CharField(required=True, allow_blank=False)


class TimeRangeSerializerAllowEmpty(BaseSerializer):
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    end_time = serializers.DateTimeField(required=False, allow_null=True)


class CalibrationTimeControls(BaseSerializer):
    calibration_start_time = serializers.DateTimeField()
    calibration_end_time = serializers.DateTimeField()
    simulation_start_time = serializers.DateTimeField()
    simulation_end_time = serializers.DateTimeField()

    def __init__(self, *args, allow_empty=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_empty = allow_empty  # Explicitly define allow_empty attribute

        # If allow_empty is True, make the fields not required
        if allow_empty:
            self.fields['calibration_start_time'].required = False
            self.fields['calibration_end_time'].required = False
            self.fields['simulation_start_time'].required = False
            self.fields['simulation_end_time'].required = False
        else:
            self.fields['calibration_start_time'].required = True
            self.fields['calibration_end_time'].required = True
            self.fields['simulation_start_time'].required = True
            self.fields['simulation_end_time'].required = True


class ValidationTimeControls(BaseSerializer):
    validation_start_time = serializers.DateTimeField()
    validation_end_time = serializers.DateTimeField()
    simulation_start_time = serializers.DateTimeField()
    simulation_end_time = serializers.DateTimeField()

    def __init__(self, *args, allow_empty=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_empty = allow_empty  # Explicitly define allow_empty attribute

        # If allow_empty is True, make the fields not required
        if allow_empty:
            self.fields['validation_start_time'].required = False
            self.fields['validation_end_time'].required = False
            self.fields['simulation_start_time'].required = False
            self.fields['simulation_end_time'].required = False
        else:
            self.fields['validation_start_time'].required = True
            self.fields['validation_end_time'].required = True
            self.fields['simulation_start_time'].required = True
            self.fields['simulation_end_time'].required = True


class LoggingConfigSerializer(BaseSerializer):
    logging_enabled = serializers.BooleanField(required=False, default=True)
    modules = serializers.DictField(child=serializers.CharField(), default=[])

    def validate_modules(self, value: dict) -> dict:
        """
        Lowercase all module names and validate:
        - Keys (module names) must match known modules (case-insensitive),
          or be the special case 'ngen' or 'ngen-forcing'
        - Values must be valid log levels from NgenLogging

        Returns a new dict with all lowercase keys.
        """
        validator = enum_validator(NgenLogging)

        valid_modules = {m.name.lower() for m in get_cached_modules_with_groups().values()}
        valid_modules.add('ngen')  # Special case
        valid_modules.add('ngen-forcing')  # Special case

        errors = {}
        normalized = {}

        for module_name, log_level in value.items():
            lowered_name = module_name.lower()
            if lowered_name not in valid_modules:
                errors[module_name] = f"Invalid module name: '{module_name}'"
                continue
            try:
                validator(log_level)
                normalized[lowered_name] = log_level
            except ValueError as e:
                errors[module_name] = f"Invalid log level for module '{module_name}': {e}"

        if errors:
            raise serializers.ValidationError(errors)

        return normalized


class SaveTuningParametersSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    minimum = serializers.FloatField(required=True, allow_null=False)
    maximum = serializers.FloatField(required=True, allow_null=False)
    initial_value = serializers.FloatField(required=True, allow_null=False)
    module = serializers.CharField(required=True, allow_blank=False)

    def __init__(self, *args, allow_empty=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_empty = allow_empty

        # Adjust field requirements based on allow_empty
        if self.allow_empty:
            for field in ['minimum', 'maximum', 'initial_value']:
                self.fields[field].required, self.fields[field].allow_null = False, True

    def validate(self, data):
        # Only validate ranges if minimum, maximum, and initial_value are provided
        min_val = data.get('minimum')
        max_val = data.get('maximum')

        if min_val is not None and max_val is not None:
            if min_val > max_val:
                raise serializers.ValidationError(
                    f"Minimum ({min_val}) must be less than maximum ({max_val}) for parameter {data['name']} in module {data['module']}"
                )

        return data


class LoadTuningParametersSerializer(BaseSerializer):
    """
    This serializer is used when loading, so min, max and initial_value are not required
    """
    name = serializers.CharField(required=True, allow_blank=False)
    minimum = serializers.FloatField(required=False, allow_null=True)
    maximum = serializers.FloatField(required=False, allow_null=True)
    initial_value = serializers.FloatField(required=False, allow_null=True)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    description = serializers.CharField(required=True, allow_blank=False)
    user_selected_for_tuning = serializers.BooleanField(required=True)
    units = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OptimizationInputsSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    value = serializers.FloatField(required=True)


class GageSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True, allow_blank=False)
    agency = serializers.CharField(required=True, allow_blank=False)
    station_name = serializers.CharField(required=True, allow_blank=False)
    latitude = serializers.FloatField(required=True, allow_null=True)
    longitude = serializers.FloatField(required=True, allow_null=True)
    altitude = serializers.FloatField(required=True, allow_null=True)


class EdsErrorsSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_null=False)
    message = serializers.CharField(required=True, allow_null=False)
    status_code = serializers.IntegerField(required=True, allow_null=True)


# This class extends the original serializers.Serializer, since we want to ignore extra fields
# Parameters from Data Services
# initial_value, min and max are strings, since Data Services sometimes has some extra crap in there, like units
# We save them in the db as floats, so we'll have to sanitize them
class ModuleParametersSerializer(serializers.Serializer):
    name = serializers.CharField(required=True, allow_blank=False)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    description = serializers.CharField(required=True, allow_blank=False)
    min = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    max = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    initial_value = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    units = serializers.CharField(required=False, allow_null=True, allow_blank=True)


# Used by LoadTuningParameters
class ModuleMetadataStaticSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    parameters = LoadTuningParametersSerializer(required=True, many=True)


##################################
# Landing page
##################################

class ValidationStatusSerializer(ValidationRunSerializer):
    validation_type = serializers.CharField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])


class CalibrationJobsResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    gage_id = serializers.CharField(required=True, allow_null=True)
    domain_name = serializers.CharField(required=True, allow_null=True)
    job_genesis = serializers.CharField(required=True, validators=[enum_validator(JobGenesis)])
    created_at = serializers.DateTimeField(required=True)
    last_updated_on = serializers.DateTimeField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    calibration_start_period = serializers.DateTimeField(required=False, allow_null=True)
    calibration_end_period = serializers.DateTimeField(required=False, allow_null=True)
    formulation_name = serializers.CharField(required=False, allow_null=True, validators=[no_space_validator])
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    objective_function = serializers.CharField(required=False, allow_null=True)
    optimization_algorithm = serializers.CharField(required=False, allow_null=True)
    validation_runs = serializers.IntegerField(required=False)
    validation_run_ids = serializers.ListSerializer(required=False, child=serializers.IntegerField())
    validations = serializers.ListSerializer(child=ValidationStatusSerializer(), required=False, allow_empty=True)
    modules = serializers.ListSerializer(child=serializers.CharField(required=True, allow_null=False, allow_blank=False), required=True)
    is_archived = serializers.BooleanField(required=True, allow_null=True)
    is_locked = serializers.BooleanField(required=True, allow_null=True)
    is_downloadable = serializers.BooleanField(required=True, allow_null=False)
    stop_criteria = serializers.IntegerField(required=False, allow_null=True)


class CalibrationJobsForValidationResponseSerializer(CalibrationJobsResponseSerializer):
    validation_runs = serializers.IntegerField(required=False)
    validation_run_ids = serializers.ListSerializer(child=serializers.IntegerField())


class GetCalibrationJobsResponseSerializer(BaseSerializer):
    jobs = serializers.ListSerializer(child=CalibrationJobsResponseSerializer(), required=True, allow_empty=True)
    total_count = serializers.IntegerField(required=True)


class GetCalibrationJobsForEvaluationResponseSerializer(BaseSerializer):
    jobs = serializers.ListSerializer(child=CalibrationJobsForValidationResponseSerializer(), required=True, allow_empty=True)
    total_count = serializers.IntegerField(required=True)


class ValidationJobsParameter(BaseSerializer):
    name = serializers.CharField(required=True, allow_null=False, allow_blank=False)
    value = serializers.FloatField(required=True, allow_null=False)


class LoadCalibrationJobSerializer(CalibrationRunSerializer):
    include_gpkg_map = serializers.BooleanField(required=False, default=True)


class JobElement(GenericMessageResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    success = serializers.BooleanField(required=True, allow_null=False)


class CalibrationRunListResponse(BaseSerializer):
    jobs = JobElement(many=True, required=True, allow_null=False)


class FooterResponseSerializer(BaseSerializer):
    ngenCerf_version = serializers.CharField(required=True)
    ngenCerf_date = serializers.CharField(required=True)
    ngenCerf_copyright = serializers.CharField(required=True)
    contact_email = serializers.CharField(required=True, allow_blank=True)


def validate_automatic_validation(value):
    if value is not True:
        raise serializers.ValidationError("automatic_validation must always be True.")
    return value


class LoadCalibrationRunResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    last_updated_on = serializers.DateTimeField(required=True)
    job_data_dir = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    gage = GageSerializer(required=True, allow_null=True)
    forcing_source_requested = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_source_actual = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    observational_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    geopackage_image_url = serializers.CharField(required=False)
    external_data_status = serializers.JSONField(required=False)
    modules = serializers.ListField(child=serializers.CharField(required=False))
    is_aet_rootzone = serializers.BooleanField(required=False)
    formulation_name = serializers.CharField(required=True, allow_null=True, allow_blank=False, validators=[no_space_validator])
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    parameters_selected = serializers.BooleanField(required=True)
    use_sloth = serializers.BooleanField(default=False)
    sloth_parameters = SlothParameters(many=True, default=[])
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    time_range = TimeRangeSerializerAllowEmpty(required=False)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    num_catchments = serializers.IntegerField(required=True, allow_null=True)
    logging_config = LoggingConfigSerializer(required=False)
    objective_function = serializers.CharField(required=True, allow_null=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    optimization = serializers.CharField(allow_blank=False, required=True, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    optimization_inputs = OptimizationInputsSerializer(many=True, default=[])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=True, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=True, allow_null=True)
    stop_criteria = serializers.IntegerField(required=True, allow_null=True, min_value=2)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)
    failure_messages = serializers.DictField(required=False, allow_null=False)


class GitInfoSerializer(BaseSerializer):
    release = serializers.CharField(required=False, allow_blank=True)
    build_date = serializers.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M:%S %Z'])
    commit_hash = serializers.CharField(required=True)
    commit_date = serializers.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M:%S %Z'])
    author = serializers.CharField(required=False)
    message = serializers.CharField(required=False)
    modules = serializers.ListField(child=serializers.DictField(), required=False)

    @staticmethod
    def validate_modules(modules):
        """
        Ensure that each module entry is a dict with exactly one key-value pair,
        and validate its value using GitInfoSerializer.
        """
        validated_modules = []
        for module in modules:
            if not isinstance(module, dict):
                raise serializers.ValidationError("Each module must be an object.")
            if len(module) != 1:
                raise serializers.ValidationError("Each module must have exactly one key.")
            # Get the single key and its associated value
            module_name, module_data = list(module.items())[0]
            # Validate the module_data using this serializer recursively
            serializer = GitInfoSerializer(data=module_data)
            serializer.is_valid(raise_exception=True)
            validated_modules.append({module_name: serializer.validated_data})
        return validated_modules


class GetGitInfoResponseSerializer(BaseSerializer):
    git_info = serializers.DictField(child=GitInfoSerializer())


class ArchiveJobRequestSerializer(CalibrationRunIdList):
    archive = serializers.BooleanField(default=True, allow_null=False, required=False)


class LockJobRequestSerializer(CalibrationRunIdList):
    lock = serializers.BooleanField(default=True, allow_null=False, required=False)


class FilterSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=False)
    status = serializers.ListField(child=serializers.CharField(validators=[enum_validator(StatusEnum)]), required=False, allow_empty=False)
    modules = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=False)
    include_archived = serializers.BooleanField(default=False, required=False)


class CalibrationSortSerializer(BaseSerializer):
    field = serializers.CharField(required=True, validators=[enum_validator(CalibrationSortField)])
    direction = serializers.ChoiceField(choices=['asc', 'desc'], required=False, default='asc')


class ForecastSortSerializer(BaseSerializer):
    field = serializers.CharField(required=True, validators=[enum_validator(ForecastSortField)])
    direction = serializers.ChoiceField(choices=['asc', 'desc'], required=False, default='asc')


class PaginationSerializer(BaseSerializer):
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    filters = FilterSerializer(required=False, allow_null=True)


class CalibrationPaginationSerializer(PaginationSerializer):
    sort = CalibrationSortSerializer(required=False, allow_null=True)


class ForecastPaginationSerializer(PaginationSerializer):
    sort = ForecastSortSerializer(required=False, allow_null=True)


##################################
# Gage Tab
##################################

class GageIdSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True, allow_blank=False)


class GetValidationJobsRequestSerializer(BaseSerializer):
    validation_run_id = serializers.IntegerField(required=True)
    include_validations = serializers.BooleanField(required=False, default=False)


class UploadForcingSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    forcing_files = serializers.FileField(required=True)

    def validate_forcing_files(self, value):
        request = self.context.get('request')
        files = request.FILES.getlist('forcing_files')

        if len(files) == 0:
            raise serializers.ValidationError("Forcing files must be uploaded")

        return value


class UploadObservationalSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    observational_file = serializers.FileField(required=True)

    def validate_observational_file(self, value):
        request = self.context.get('request')
        files = request.FILES.getlist('observational_file')
        if len(files) != 1:
            raise serializers.ValidationError("Only one observational file should be uploaded.")

        return value


class UploadGeopackageSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    geopackage_file = serializers.FileField(required=True)
    return_geopackage_url = serializers.BooleanField(default=True)

    def validate_geopackage_file(self, value):
        request = self.context.get('request')
        files = request.FILES.getlist('geopackage_file')
        if len(files) != 1:
            raise serializers.ValidationError("Only one geopackage file should be uploaded.")

        return value


class UploadGeopackageResponseSerializer(GenericResponseSerializer):
    geopackage_image_url = serializers.CharField(required=False)
    num_catchments = serializers.IntegerField(required=True, allow_null=True)


class SaveGageRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    gage_id = serializers.CharField(required=False, allow_blank=False)
    forcing_source_requested = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    observational_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])


class SaveGageResponseSerializer(GenericResponseSerializer):
    geopackage_image_url = serializers.CharField(required=False, allow_null=True)
    eds_errors = EdsErrorsSerializer(many=True, required=False)
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    num_catchments = serializers.IntegerField(required=True, allow_null=True)
    forcing_source_requested = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_source_actual = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])


class DomainResponseSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(DomainEnum)])
    description = serializers.CharField(required=True, allow_blank=False)


class GagesSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True, allow_blank=False)
    nws_id = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    domain = serializers.CharField(required=True, validators=[enum_validator(DomainEnum)])
    headwater_calibration = serializers.BooleanField(required=True)


class ForcingSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ForcingSourceEnum)])
    description = serializers.CharField(required=True)


class ObservationalSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ObservationalSourceEnum)])
    description = serializers.CharField(required=True)


class GeopackageSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(GeopackageSourceEnum)])
    description = serializers.CharField(required=True)


class LoadGageResponseSerializer(BaseSerializer):
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    calibration_run_id = serializers.IntegerField(required=True)
    forcing_source_values = ForcingSourceSerializer(many=True)
    observational_source_values = ObservationalSourceSerializer(many=True)
    geopackage_source_values = GeopackageSourceSerializer(many=True)
    gages = GagesSerializer(required=True, many=True)
    gage = GageSerializer(required=False)
    geopackage_image_url = serializers.CharField(required=False)
    domain_values = DomainResponseSerializer(many=True)


class UpdateGageStatusRequestSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True)
    is_active = serializers.BooleanField(required=False)


class UpdateGageStatusResponseSerializer(GenericMessageResponseSerializer):
    gage_id = serializers.CharField(required=True)
    is_active = serializers.BooleanField(required=True)


class CreateCalibrationRunResponseSerializer(GenericMessageResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    job_data_dir = serializers.CharField(required=True)


class CreateAndRunValidationResponseSerializer(GenericResponseSerializer):
    validation_run_id = serializers.IntegerField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class CreateAndRunColdStartResponseSerializer(BaseSerializer):
    message = serializers.CharField(required=True)
    calibration_run_id = serializers.IntegerField(required=True)
    coldstart_run_id = serializers.IntegerField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])


class CreateAndRunForecastResponseSerializer(BaseSerializer):
    message = serializers.CharField(required=True)
    calibration_run_id = serializers.IntegerField(required=True)
    forecast_run_id = serializers.IntegerField(required=True)
    cold_start_run_id = serializers.IntegerField(required=True, allow_null=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


# Geopackage from Data Services
class GeopackageSerializer(BaseSerializer):
    uri = serializers.CharField(required=True, allow_blank=False)
    creation_date = serializers.DateTimeField(required=True)


##################################
# Plot Definitions Tab
##################################

class PlotListStaticSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    display_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    description = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    timeseries_available = serializers.BooleanField(required=True, allow_null=False)


class GetPlotNamesResponseSerializer(CalibrationOrValidationRunSerializer):
    plot_names = PlotListStaticSerializer(many=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])


class GetPlotNamesForComparisonResponseSerializer(BaseSerializer):
    plot_names = PlotListStaticSerializer(many=True)


class GetPlotRequestSerializer(CalibrationOrValidationRunSerializer):
    plot_name = serializers.CharField(required=True, allow_null=False, validators=[enum_validator(PlotDefinitionsEnum)])
    include_data = serializers.BooleanField(required=False, default=False)
    force_include_plot = serializers.BooleanField(required=False, default=False)
    start = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class GetPlotsForComparisonRequestSerializer(CalibrationRunIdList):
    plot_name = serializers.CharField(required=True, allow_null=False, validators=[enum_validator(PlotDefinitionsEnum)])
    gage_id = serializers.CharField(required=True)
    start = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class PaginationMetadataSerializer(BaseSerializer):
    start = serializers.IntegerField(required=True)
    limit = serializers.IntegerField(required=True)
    count = serializers.IntegerField(required=True)


class GetPlotResponseSerializer(CalibrationRunSerializer):
    validation_run_id = serializers.IntegerField(required=False)
    forecast_run_id = serializers.IntegerField(required=False)
    plot_name = serializers.CharField(required=True, allow_null=False)
    plot_file_path = serializers.CharField(required=False, allow_null=False)
    plot_url = serializers.CharField(required=False, allow_null=False)
    plot_data = serializers.JSONField(required=False)
    pagination_metadata = PaginationMetadataSerializer(required=False)


class ForecastRunDataResponseSerializer(BaseSerializer):
    forecast_run_id = serializers.IntegerField(required=True)
    timeseries_data = serializers.JSONField(required=True)


class GetPlotErrorResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    message = serializers.CharField(required=True)


class GetPlotForComparisonResponseSerializer(GetPlotResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=False)


class GetPlotsForComparisonResponseSerializer(CalibrationRunIdList):
    plots = GetPlotForComparisonResponseSerializer(many=True, required=False)
    errors = serializers.ListField(required=False, child=GetPlotErrorResponseSerializer(required=True))


##################################
# Formulation Tab
##################################


class S3UriField(serializers.CharField):
    def __init__(self, validate_directory=False, **kwargs):
        regex = r'^s3://([^/]+)/(.*?([^/]+)/)$' if validate_directory else r'^s3://([^/]+)/(.*?([^/]+))$'
        self.default_validators = [RegexValidator(regex, 'This field must be a valid S3 URI')]
        super().__init__(**kwargs)


class S3DirectoryValidator(BaseSerializer):
    uri = S3UriField(validate_directory=True)


class S3FileValidator(BaseSerializer):
    # TODO We need to allow_null due to EDS error handling.  Need to get EDS to change their data when an error is returned
    uri = S3UriField(allow_null=True)


class ValidateFormulationRequestSerializer(BaseSerializer):
    modules = serializers.ListField(child=serializers.CharField(required=True), required=False)


class SaveFormulationRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    formulation_name = serializers.CharField(required=False, allow_blank=False, validators=[no_space_validator])
    is_aet_rootzone = serializers.BooleanField(required=False)
    modules = serializers.ListField(child=serializers.CharField(required=True), required=False)
    use_sloth = serializers.BooleanField(required=True)
    sloth_parameters = SlothParameters(required=False, many=True)


class ValidateFormulationResponseSerializer(BaseSerializer):
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    formulation_messages = serializers.JSONField(required=False)


class SaveFormulationResponseSerializer(GenericResponseSerializer):
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    formulation_messages = serializers.JSONField(required=False)
    eds_errors = EdsErrorsSerializer(many=True, required=False)


class ModuleStaticSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    display_name = serializers.CharField(required=True, allow_blank=False)
    description = serializers.CharField(required=True, allow_blank=False)
    groups = serializers.ListField(child=serializers.CharField(required=True))
    is_active = serializers.BooleanField(required=True)


class GetModulesResponseSerializer(BaseSerializer):
    modules = ModuleStaticSerializer(many=True)
    module_groups = serializers.ListSerializer(child=serializers.CharField(required=True), required=True, allow_null=False, allow_empty=False)


##################################
# Tuning Tab
##################################
class UploadUserParameterFile(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    user_parameter_file = serializers.FileField(required=True)

    def validate_user_parameter_file(self, value):
        request = self.context.get('request')
        files = request.FILES.getlist('user_parameter_file')
        if len(files) != 1:
            raise serializers.ValidationError("Only one parameter file should be uploaded.")
        return value


class ParameterFileSerializer(BaseSerializer):
    param = serializers.CharField(required=True)
    min = serializers.FloatField(required=True)
    max = serializers.FloatField(required=True)
    init = serializers.FloatField(required=True)
    model = serializers.CharField(required=True)


class UserParameterFileUploadResponse(BaseSerializer):
    message = serializers.CharField(required=True)
    calibration_run_id = serializers.IntegerField(required=True)
    user_parameter_file = serializers.ListField(child=ParameterFileSerializer(), required=True)


# Module object from Data Services containing module parameters and output variables
class ModuleMetadataSerializer(BaseSerializer):
    module_name = serializers.CharField(required=True, allow_blank=False)
    calibrate_parameters = ModuleParametersSerializer(many=True)
    # TODO We are ignoring this so EDS can get rid of it
    output_variables = serializers.JSONField(required=False)
    parameter_file = S3FileValidator(required=True)
    error = serializers.CharField(required=False)


# List of module objects from Data Services containing module parameters and output variables
class ModuleDataListSerializer(BaseSerializer):
    modules = ModuleMetadataSerializer(many=True, min_length=1, required=True)


class SaveTuningRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    parameters = SaveTuningParametersSerializer(many=True, required=False)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=False)
    validation_times = ValidationTimeControls(required=False, allow_empty=False)
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])


class LoadTuningResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    modules = ModuleMetadataStaticSerializer(many=True, required=False)
    time_range = TimeRangeSerializerAllowEmpty(required=True)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])


##################################
# Optimization Tab
##################################


class SaveOptimizationRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    optimization_inputs = OptimizationInputsSerializer(many=True, required=False)
    optimization = serializers.CharField(allow_blank=False, required=False, validators=[enum_validator(OptimizationEnum)])
    objective_function = serializers.CharField(allow_blank=False, required=False)
    streamflow_threshold = serializers.FloatField(required=False, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, validators=[greater_than_zero])
    stop_criteria = serializers.IntegerField(required=False, min_value=2)
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=False)
    save_output_iteration = serializers.BooleanField(required=False)


class OptimizationInputStaticSerializer(serializers.Serializer):
    name = serializers.CharField(required=True)
    description = serializers.CharField(required=True)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    default_value = serializers.FloatField(required=True)
    min = serializers.FloatField(required=False, allow_null=True)
    max = serializers.FloatField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=True)


class OptimizationInputsUserSerializer(serializers.Serializer):
    name = serializers.CharField(required=True)
    value = serializers.FloatField(required=True)


class MetricSerializer(serializers.Serializer):
    name = serializers.CharField()
    display_name = serializers.CharField()
    categorical = serializers.BooleanField()
    event_based = serializers.BooleanField()


class OptimizationStaticSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField()
    is_active = serializers.BooleanField()
    inputs = OptimizationInputStaticSerializer(many=True)


class LoadOptimizationResponseSerializer(serializers.Serializer):
    calibration_run_id = serializers.IntegerField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    metrics = MetricSerializer(many=True)
    optimizations = OptimizationStaticSerializer(many=True)


##################################
# Run Tab
##################################

class PerformanceMetricsSerializer(BaseSerializer):
    elapsed_time = serializers.DurationField(required=True, allow_null=True)
    num_cpus = serializers.IntegerField(required=True, allow_null=True)
    cpu_time = serializers.DurationField(required=True, allow_null=True)
    max_rss = serializers.CharField(required=True, allow_null=True)
    max_disk_read = serializers.CharField(required=True, allow_null=True)
    max_disk_write = serializers.CharField(required=True, allow_null=True)
    reserved_time = serializers.DurationField(required=False, allow_null=True)
    io_throughput = serializers.CharField(required=False, allow_null=True)


class CommonStatusFieldsMixin(serializers.Serializer):
    calibration_run_id = serializers.IntegerField(required=False)
    formulation_name = serializers.CharField(required=False)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)
    failure_messages = serializers.DictField(required=False, allow_null=False)
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)


class GetStatusValidationsResponseSerializer(CommonStatusFieldsMixin, ValidationRunSerializer):
    validation_type = serializers.CharField(required=True)
    iteration_num = serializers.IntegerField(allow_null=True)


class GetStatusColdStartSerializer(BaseSerializer):
    cold_start_run_id = serializers.IntegerField(required=True)
    status = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    failure_messages = serializers.DictField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)


class GetStatusForecastsResponseSerializer(CommonStatusFieldsMixin, ForecastRunSerializer):
    configuration = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=True)
    cold_start_run = GetStatusColdStartSerializer(required=False, allow_null=True)


class GetStatusResponseSerializer(GenericResponseSerializer):
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)
    failure_messages = serializers.DictField(required=False, allow_null=False)
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    validations = GetStatusValidationsResponseSerializer(many=True)
    forecasts = GetStatusForecastsResponseSerializer(many=True)
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)


class GetStatusForComparisonResponseSerializer(CalibrationRunIdList):
    statuses = CommonStatusFieldsMixin(many=True, required=False)
    errors = serializers.ListField(required=False, child=GetPlotErrorResponseSerializer(required=True))


class ImportResponseSerializer(GenericResponseSerializer):
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    messages = serializers.JSONField(required=False)


class SubmitCalibrationJobResponseSerializer(GenericResponseSerializer):
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class GetIterationsResponseSerializer(GenericResponseSerializer):
    iteration = serializers.IntegerField(required=True, allow_null=True)


class CalibrationJobSlurmCallbackRequestSerializer(CalibrationRunSerializer):
    job_status = serializers.CharField(required=True, validators=[SlurmStatusEnum])


class ValidationJobSlurmCallbackRequestSerializer(ValidationRunSerializer):
    job_status = serializers.CharField(required=True, validators=[SlurmStatusEnum])


class ColdStartJobSlurmCallbackRequestSerializer(ColdStartRunSerializer):
    job_status = serializers.CharField(required=True, validators=[SlurmStatusEnum])


class ForecastJobSlurmCallbackRequestSerializer(ForecastRunSerializer):
    job_status = serializers.CharField(required=True, validators=[SlurmStatusEnum])


class VerificationJobSlurmCallbackRequestSerializer(VerificationRunSerializer):
    job_status = serializers.CharField(required=True, validators=[SlurmStatusEnum])


class RunCalibrationJob(CalibrationRunSerializer):
    logging_config = LoggingConfigSerializer(required=False)


def get_mpi_rules_field(required: bool = True) -> serializers.ListField:
    return serializers.ListField(
        required=required,
        allow_null=not required,
        child=serializers.ListField(
            child=serializers.IntegerField(),
            min_length=2,
            max_length=2
        )
    )


class MPINodesRulesSerializer(BaseSerializer):
    mpi_rules = get_mpi_rules_field(required=False)

    def validate_mpi_rules(self, value):
        if value in (None, []):
            # Accept empty input for GET-style query (no validation needed)
            return value

        previous_threshold = -1
        for i, rule in enumerate(value):
            max_catchments, num_nodes = rule

            if max_catchments < -1:
                raise serializers.ValidationError(f"Invalid threshold {max_catchments} at index {i}")

            if num_nodes < 1:
                raise serializers.ValidationError(f"Number of nodes must be ≥ 1 at index {i}")

            if i < len(value) - 1:
                if max_catchments == -1:
                    raise serializers.ValidationError(f"-1 (infinite) threshold must only appear as the last rule (index {i})")
                if max_catchments <= previous_threshold:
                    raise serializers.ValidationError(f"Thresholds must be strictly increasing (problem at index {i})")

            previous_threshold = max_catchments

        if value[-1][0] != -1:
            raise serializers.ValidationError("The final rule must have max_catchments = -1 to cover all cases")

        return value


class MPINodesRulesResponseSerializer(GenericMessageResponseSerializer):
    mpi_rules = get_mpi_rules_field(required=False)


##################################
# Forecast Tab
##################################
class ForecastConfigSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    data_sources = serializers.CharField(required=False, allow_null=True)
    time_range = serializers.CharField(required=False, allow_null=True)
    domain = serializers.CharField(required=False, validators=[enum_validator(DomainEnum)])
    availability_lag = serializers.IntegerField(required=True)
    cycle_start = serializers.IntegerField(required=True)
    cycle_end = serializers.IntegerField(required=True)
    cycle_freq = serializers.IntegerField(required=True)
    fcst_win = serializers.IntegerField(required=True)
    fcst_timestep = serializers.FloatField(required=True)


class LoadForecastTabResponseSerializer(BaseSerializer):
    forecast_configuration_values = ForecastConfigSerializer(many=True)


class ColdStartJobsResponseSerializer(BaseSerializer):
    cold_start_status = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(StatusEnum)])
    cold_start_date = serializers.DateTimeField(required=True, allow_null=True)
    cold_start_submit_date = serializers.DateTimeField(required=True, allow_null=True)


class ForecastJobsResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    forecast_run_id = serializers.IntegerField(required=True)
    domain_name = serializers.CharField(required=True)
    configuration = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    gage_id = serializers.CharField(required=True)
    forecast_status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    cold_start = ColdStartJobsResponseSerializer(required=False, allow_null=False)


class GetForecastJobsResponseSerializer(BaseSerializer):
    forecast_jobs = serializers.ListSerializer(child=ForecastJobsResponseSerializer(), required=True, allow_empty=True)
    total_count = serializers.IntegerField(required=True)


##################################
# Verification Tab
##################################
class VerificationJobSerializer(BaseSerializer):
    verification_job_id = serializers.IntegerField(required=True)


class VerificationJobsResponseSerializer(BaseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    forecast_run = ForecastJobsResponseSerializer(required=False, allow_null=True)
    forecast_run_id = serializers.IntegerField(required=True, allow_null=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    created_at = serializers.DateTimeField(required=True, allow_null=True)


class VerificationJobDetailsResponseSerializer(VerificationJobsResponseSerializer):
    forecast_run_id = serializers.IntegerField(required=False, allow_null=False)
    run_start = serializers.DateTimeField(required=True, allow_null=True)
    run_end = serializers.DateTimeField(required=True, allow_null=True)


class GetVerificationJobsResponseSerializer(BaseSerializer):
    verification_jobs = serializers.ListSerializer(child=VerificationJobsResponseSerializer(), required=True, allow_empty=True)


class CreateVerificationJobRequestSerializer(BaseSerializer):
    forecast_run_id = serializers.IntegerField(required=False)


class CreateVerificationJobResponseSerializer(GenericMessageResponseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    job_data_dir = serializers.CharField(required=True)


class GetVerificationStatusRequestSerializer(VerificationJobSerializer):
    include_performance_metrics = serializers.BooleanField(required=False, default=False)


class RunVerificationJob(VerificationJobSerializer):
    logging_config = LoggingConfigSerializer(required=False)


class SubmitVerificationJobResponseSerializer(GenericMessageAndStatusResponseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class GetVerificationStatusResponseSerializer(GenericMessageAndStatusResponseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)
    failure_messages = serializers.DictField(required=False, allow_null=False)


class GetVerificationPlotNamesResponseSerializer(BaseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    plot_names = PlotListStaticSerializer(many=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])


class GetVerificationPlotRequestSerializer(BaseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    plot_name = serializers.CharField(required=True, allow_null=False)


class GetVerificationPlotResponseSerializer(BaseSerializer):
    verification_job_id = serializers.IntegerField(required=True)
    plot_name = serializers.CharField(required=True, allow_null=False)
    plot_file_path = serializers.CharField(required=False, allow_null=False)
    plot_url = serializers.CharField(required=False, allow_null=False)


class DeleteVerificationJobResponseSerializer(GenericMessageResponseSerializer):
    verification_job_id = serializers.IntegerField(required=True)


##################################
# Import/Export
##################################


# All fields are required, so that the user can see what is missing.
# Any objects will be set to an empty object, {} or []
# Booleans will default to False
# Scalers will be set to None
class ExportResponseSerializer(BaseSerializer):
    metadata = serializers.JSONField(required=False)
    run_after_import = serializers.BooleanField(default=False)
    gage_id = serializers.CharField(required=True, allow_null=True)
    forcing_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_user_uploaded_dir_path = serializers.CharField(required=False, allow_blank=False, allow_null=True)
    observational_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    observational_user_uploaded_file_path = serializers.CharField(required=False, allow_blank=False, allow_null=True)
    geopackage_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    geopackage_user_uploaded_file_path = serializers.CharField(required=False, allow_blank=False, allow_null=True)
    modules = serializers.ListField(child=serializers.CharField(required=False), default=[])
    is_aet_rootzone = serializers.BooleanField(required=False)
    formulation_name = serializers.CharField(required=True, allow_null=True, allow_blank=False, validators=[no_space_validator])
    use_sloth = serializers.BooleanField(default=False)
    sloth_parameters = SlothParameters(many=True, default={})
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    parameters = SaveTuningParametersSerializer(many=True, required=True)
    objective_function = serializers.CharField(required=True, allow_null=True)
    optimization_inputs = OptimizationInputsSerializer(many=True, default={})
    optimization = serializers.CharField(allow_blank=False, required=True, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=True, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=True, allow_null=True)
    stop_criteria = serializers.IntegerField(required=True, allow_null=True, min_value=2)
    logging_config = LoggingConfigSerializer(required=False)


class ImportDataSerializer(BaseSerializer):
    run_after_import = serializers.BooleanField(required=False, default=False)
    metadata = serializers.JSONField(required=False)
    gage_id = serializers.CharField(required=False, allow_null=True)
    forcing_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_user_dir = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    forcing_user_uploaded_dir_path = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    observational_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    observational_user_file_path = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    observational_user_uploaded_file_path = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    geopackage_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    geopackage_user_uploaded_file_path = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    modules = serializers.ListField(child=serializers.CharField(required=False), required=False, allow_empty=True)
    is_aet_rootzone = serializers.BooleanField(required=False)
    sloth_parameters = SlothParameters(required=False, many=True, allow_empty=True)
    formulation_name = serializers.CharField(required=False, allow_null=True, allow_blank=False, validators=[no_space_validator])
    use_sloth = serializers.BooleanField(required=False, default=False)
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    parameters = serializers.ListSerializer(child=SaveTuningParametersSerializer(allow_empty=True), required=False)
    objective_function = serializers.CharField(required=False, allow_null=True)
    optimization_inputs = OptimizationInputsSerializer(many=True, required=False)
    optimization = serializers.CharField(allow_blank=False, required=False, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=False, allow_null=False, default=False)
    stop_criteria = serializers.IntegerField(required=False, allow_null=True, min_value=2)
    logging_config = LoggingConfigSerializer(required=False)


class ImportSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False)
    data = ImportDataSerializer(required=True)


##################################
# Misc
##################################
class ReportIterationSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True)
    iteration = serializers.IntegerField(required=True, min_value=0)
    worker_name = serializers.CharField(required=True)
    first_iteration_for_worker = serializers.BooleanField(required=True)


class ErrorDetailListField(serializers.ListField):
    child = serializers.CharField()

    def to_representation(self, value):
        # Ensure that the value is a list of ErrorDetail objects
        if not all(isinstance(item, ErrorDetail) for item in value):
            raise serializers.ValidationError("All items must be instances of ErrorDetail.")
        return [str(item) for item in value]

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError("Expected a list of strings.")
        return [ErrorDetail(item) for item in data]


class ErrorResponseSerializer(BaseSerializer):
    response_type = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    message = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    validation_errors = serializers.JSONField(required=False, allow_null=False)
    errors = serializers.JSONField(required=False, allow_null=False)


##################################
# Evaluation
##################################
class ParameterDataByIteration(BaseSerializer):
    parameter_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    parameter_value = serializers.FloatField(required=True, allow_null=False)


class MetricDataByIteration(BaseSerializer):
    metric_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    metric_display_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    # Need to allow Null for NaN
    metric_value = serializers.FloatField(required=True, allow_null=True)


class CalibrationDataByIteration(BaseSerializer):
    iteration_num = serializers.IntegerField(required=True, allow_null=False, min_value=0)
    iteration_id = serializers.IntegerField(required=True, allow_null=False)
    validation_run_id = serializers.IntegerField(required=False)
    worker_name = serializers.CharField(required=True, allow_null=False, allow_blank=False)
    best_params = serializers.BooleanField(required=True, allow_null=False)
    objective_function_value = serializers.FloatField(required=True, allow_null=True)
    parameters = ParameterDataByIteration(many=True, required=True)
    metrics = MetricDataByIteration(many=True, required=True)


class RetrospectiveData(BaseSerializer):
    name = serializers.CharField(required=True)
    data = MetricDataByIteration(many=True, required=True)


class GetCalibrationDataByIterationResponseSerializer(GenericMessageResponseSerializer):
    objective_function_metric = serializers.CharField(required=True, allow_null=True)
    iteration_data = CalibrationDataByIteration(many=True, required=True)
    retrospective_data = RetrospectiveData(many=True, required=True)


class ValidationJobsResponseSerializer(BaseSerializer):
    validation_run_id = serializers.IntegerField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    validation_type = serializers.CharField(required=True)
    iteration_num = serializers.IntegerField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum)])
    # Can be empty for LSTM
    parameters = serializers.ListSerializer(child=ValidationJobsParameter(), required=True, allow_empty=True)
    best = serializers.BooleanField(required=True)


class GetValidationJobsResponseSerializer(BaseSerializer):
    validation_jobs = serializers.ListSerializer(child=ValidationJobsResponseSerializer(), required=True, allow_empty=True)


class GetLogRequestSerializer(CalibrationOrValidationRunSerializer):
    log_category = serializers.CharField(required=True, validators=[enum_validator(LogCategory)])
    log_name = serializers.CharField(required=True, validators=[enum_validator(LogName)])
    start = serializers.IntegerField(required=False, default=0, min_value=-1)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class GetLogStatusRequestSerializer(CalibrationOrValidationRunSerializer):
    log_path = serializers.CharField(required=True)
    byte_offset = serializers.IntegerField(required=True, min_value=0)


class LogCategoryDictField(serializers.DictField):
    def __init__(self, **kwargs):
        # Define the child as a ListField for log names
        super().__init__(**kwargs)
        self.child = serializers.ListField(
            child=serializers.CharField(), required=False
        )
        # Attach the enum validator for dictionary keys
        self.key_validator = enum_validator(LogCategory)

    def to_internal_value(self, data):
        # Validate all keys using the enum_validator
        for key in data.keys():
            if not isinstance(key, str):
                raise serializers.ValidationError(f"Invalid key type: {type(key)}. Expected string.")
            self.key_validator(key)  # Validate the key as a string
        return super().to_internal_value(data)


class GetLogNamesResponseSerializer(BaseSerializer):
    log_names = serializers.ListSerializer(child=LogCategoryDictField(), required=True)


class GetLogsResponseSerializer(GenericMessageResponseSerializer):
    log_data = serializers.ListSerializer(child=serializers.CharField(allow_blank=True), required=True, allow_null=False)
    pagination_metadata = PaginationMetadataSerializer(required=False)
    log_path = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    byte_offset = serializers.IntegerField(required=False)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=False)


class GetLogStatusResponseSerializer(GenericMessageResponseSerializer):
    file_updated = serializers.BooleanField(required=True)
    status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)


##################################
# Snowdas/SWE/Soil Moisture
##################################
class GetSWEImagesByDateRequestSerializer(ValidationRunSerializer):
    date = serializers.DateField(required=True, allow_null=False)


class GetSoilMoistureImagesByDateRequestSerializer(ValidationRunSerializer):
    datetime = serializers.DateTimeField(required=True, allow_null=False)

    def validate_datetime(self, value):
        """
        Ensure that the datetime is at hour precision (minutes and seconds are zero).
        Example of valid value: 2025-10-01T12:00:00
        """
        if value.minute != 0 or value.second != 0 or value.microsecond != 0:
            raise serializers.ValidationError(
                "Datetime must be at the top of the hour (e.g., 2025-10-01T12:00:00)."
            )
        return value


class GetImagesByDateResponseSerializer(GenericMessageResponseSerializer):
    lumped_map = serializers.CharField(required=True, allow_null=False)
    raw_map = serializers.CharField(required=True, allow_null=False)
    sim_map = serializers.CharField(required=True, allow_null=False)


class GetTimeseriesDataResponseSerializer(GenericMessageResponseSerializer):
    timeseries_image = serializers.CharField(required=True, allow_null=False)
    timeseries_data = serializers.JSONField(required=True, allow_null=False)


##################################
# Slurm
##################################
class SlurmSubmitResponseSerializer(BaseSerializer):
    slurm_job_id = serializers.IntegerField(required=False, allow_null=False)
