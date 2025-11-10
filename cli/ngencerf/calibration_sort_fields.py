from enum import Enum


# ────────────────────────────────────────────────────────────────────────────────
# NOTE:
# This class is a direct duplicate of the `CalibrationSortField` enum defined in
# `calibration/enums_vanilla.py` within the cerfServer backend.
#
# It exists here inside the CLI package so that the CLI can be built and run
# independently of the full Django server codebase (e.g., when distributed as
# a standalone executable via PyInstaller).
#
# A consistency check (`check_enum_consistency.py`) runs during build to ensure
# this definition remains identical to the server-side version.
# ────────────────────────────────────────────────────────────────────────────────
class CalibrationSortField(Enum):
    CALIBRATION_RUN_ID = ("calibration_run_id", "id")
    GAGE_ID = ("gage_id", "gage__gage_id")
    DOMAIN_NAME = ("domain_name", "gage__domain__name")
    FORMULATION_NAME = ("formulation_name", "user_formulation_name")
    SUBMIT_DATE = ("submit_date", "submit_date")
    CREATED_AT = ("created_at", "created_at")
    LAST_UPDATED_ON = ("last_updated_on", "updated_at")
    JOB_GENESIS = ("job_genesis", "job_genesis")
    STATUS = ("status", "status__name")
    OBJECTIVE_FUNCTION = ("objective_function", "objective_function__name")
    OPTIMIZATION_ALGORITHM = ("optimization_algorithm", "optimization__name")
    PERIOD = ("period", ["calibration_start_period", "calibration_end_period"])
    STOP_CRITERIA = ("stop_criteria", "calibrationstopcriteria__value")
    IS_ARCHIVED = ("is_archived", "is_archived")
    IS_LOCKED = ("is_locked", "is_locked")
    VALIDATION_RUNS = ("validation_runs", "validation_run_count")

    @property
    def orm_field(self):
        return self.value[1]

    @classmethod
    def from_name(cls, name: str) -> "Self":
        return next(member for member in cls if member.value[0] == name)

    @classmethod
    def get_names(cls) -> list[str]:
        """Return the canonical API names (i.e., the first slot of each tuple)."""
        return [member.value[0] for member in cls]

