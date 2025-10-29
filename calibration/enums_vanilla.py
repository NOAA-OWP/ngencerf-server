from enum import StrEnum


# These enums are used from settings.py.  We need to avoid any references to the model

class ScriptEnum(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"
    VALIDATION_ITERATION = "validation_iteration"
    COLD_START = "cold_start"
    FORECAST = "forecast"
    VERIFICATION = "verification"


class NgenEnvironmentEnum(StrEnum):
    LOCAL = "LOCAL"
    PARALLEL_WORKS = "PARALLEL_WORKS"
    DOCKER = "DOCKER"


class JobType(StrEnum):
    CALIBRATION = 'calibration'
    VALIDATION = 'validation'
    COLD_START = 'cold_start'
    FORECAST = 'forecast'
    VERIFICATION = 'verification'
    COMPARISON = 'comparison'


class SecondaryDataEnum(StrEnum):
    SWE = 'SWE'
    SOIL_MOISTURE = 'Soil Moisture'

