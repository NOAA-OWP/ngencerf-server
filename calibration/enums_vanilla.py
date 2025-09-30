from enum import StrEnum


# These enums are used from settings.py.  We need to avoid any references to the model

class ScriptEnum(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"
    VALIDATION_ITERATION = "validation_iteration"
    FORECAST = "forecast"


class NgenEnvironmentEnum(StrEnum):
    LOCAL = "LOCAL"
    PARALLEL_WORKS = "PARALLEL_WORKS"
    DOCKER = "DOCKER"


class JobType(StrEnum):
    CALIBRATION = 'calibration'
    VALIDATION = 'validation'
    FORECAST = 'forecast'
    COMPARISON = 'comparison'
