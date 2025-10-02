# Job registry to store subprocess objects keyed by a unique string (e.g., "calibration_123")
job_registry: dict[str, subprocess.Popen] = {}


def get_job_registry_key(run: BaseRun) -> str:
    """
    Generate a unique string key for the job registry based on run type.

    Format: "<run_class>_<id>" (all lowercase).
    Examples:
      - CalibrationRun(id=123) → "calibrationrun_123"
      - ValidationRun(id=45)   → "validationrun_45"
      - ForecastRun(id=67)     → "forecastrun_67"
      - ForecastForcingDownloadRun(id=89) → "forecastforcingdownloadrun_89"

    :param run: The CalibrationRun, ValidationRun, ForecastRun, or ForecastForcingDownloadRun object.
    :return: A unique string key for the job registry.
    """
    return f"{run.__class__.__name__.lower()}_{run.id}"
