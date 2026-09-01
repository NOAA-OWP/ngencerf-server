"""
Utilities for converting supported legacy ngenCerf calibration job JSON
to the current import format.

This module does not support every historical job format. It only converts
the legacy time configuration in which:

* calibration_times and validation_times are stored at the top level
* time_controls is not present

The conversion:

* moves calibration_times under metadata
* moves validation_times under metadata
* infers and adds the top-level time_controls object

Other legacy differences are not handled. Imports may still fail if older
files contain fields that were renamed, removed, or otherwise changed.

Keeping this conversion in the CLI allows supported legacy files to be
updated before they are sent to the server, without requiring the server
to accept legacy input formats.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def convert_legacy_job_data(
        job_data: dict,
) -> tuple[dict, list[str]]:
    """
    Convert legacy calibration job data to the current import format.

    A legacy job is identified by all of the following:

    - time_controls is not present
    - calibration_times is present at the top level
    - validation_times is present at the top level

    Current-format job data is returned unchanged.

    :param job_data: Loaded calibration job JSON.
    :return: A tuple containing the job data and any conversion messages.
    :raises ValueError: If legacy time data is incomplete or invalid.
    """
    if not isinstance(job_data, dict):
        raise ValueError(
            "The root value in the job file must be a JSON object"
        )

    if "time_controls" in job_data:
        return job_data, []

    has_calibration_times = "calibration_times" in job_data
    has_validation_times = "validation_times" in job_data

    if not has_calibration_times and not has_validation_times:
        return job_data, []

    calibration_times = job_data.get("calibration_times")
    validation_times = job_data.get("validation_times")

    if not isinstance(calibration_times, dict):
        raise ValueError(
            "Legacy job contains calibration_times, but it is not "
            "a JSON object"
        )

    if not isinstance(validation_times, dict):
        raise ValueError(
            "Legacy job contains validation_times, but it is not "
            "a JSON object"
        )

    time_controls, messages = _infer_time_controls(
        calibration_times,
        validation_times,
    )

    metadata = job_data.get("metadata", {})

    if not isinstance(metadata, dict):
        raise ValueError(
            "metadata must be a JSON object"
        )

    transformed_data = dict(job_data)
    transformed_metadata = dict(metadata)

    transformed_metadata["calibration_times"] = calibration_times
    transformed_metadata["validation_times"] = validation_times

    transformed_data["metadata"] = transformed_metadata
    transformed_data["time_controls"] = time_controls

    transformed_data.pop("calibration_times", None)
    transformed_data.pop("validation_times", None)

    messages.insert(
        0,
        "Moved calibration_times and validation_times from the "
        "top level into metadata.",
    )

    return transformed_data, messages


def _infer_time_controls(
        calibration_times: dict,
        validation_times: dict,
) -> tuple[dict, list[str]]:
    """
    Infer current time_controls from legacy calibration and validation ranges.

    Durations and gaps are represented as whole calendar months. When a
    legacy timestamp does not fall on the expected calendar-month boundary,
    the nearest boundary is used and an explanatory message is returned.

    :param calibration_times: Legacy calibration time ranges.
    :param validation_times: Legacy validation time ranges.
    :return: Inferred time controls and explanatory messages.
    """
    messages: list[str] = []

    calibration = _parse_time_object(
        "calibration_times",
        calibration_times,
        (
            "simulation_start_time",
            "simulation_end_time",
            "calibration_start_time",
            "calibration_end_time",
        ),
        messages,
    )

    validation = _parse_time_object(
        "validation_times",
        validation_times,
        (
            "simulation_start_time",
            "simulation_end_time",
            "validation_start_time",
            "validation_end_time",
        ),
        messages,
    )

    simulation_start = calibration["simulation_start_time"]
    calibration_start = calibration["calibration_start_time"]
    calibration_end = calibration["calibration_end_time"]

    validation_start = validation["validation_start_time"]
    validation_end = validation["validation_end_time"]

    warmup = _nearest_calendar_months(
        simulation_start,
        calibration_start,
    )

    calibration_duration = _nearest_calendar_months(
        calibration_start,
        calibration_end,
        inclusive_end=True,
    )

    validation_duration = _nearest_calendar_months(
        validation_start,
        validation_end,
        inclusive_end=True,
    )

    _append_inference_message(
        messages,
        "warmup_duration",
        calibration_start,
        warmup,
    )

    _append_inference_message(
        messages,
        "calibration_duration",
        calibration_end,
        calibration_duration,
        inclusive_end=True,
    )

    _append_inference_message(
        messages,
        "validation_duration",
        validation_end,
        validation_duration,
        inclusive_end=True,
    )

    calibration_end_boundary = _add_calendar_months(
        calibration_start,
        calibration_duration.months,
    )

    validation_end_boundary = _add_calendar_months(
        validation_start,
        validation_duration.months,
    )

    after_gap = _nearest_calendar_months(
        calibration_end_boundary,
        validation_start,
    )

    before_gap = _nearest_calendar_months(
        validation_end_boundary,
        calibration_start,
    )

    clearly_after = (
            validation_start >= calibration_end_boundary
    )

    clearly_before = (
            calibration_start >= validation_end_boundary
    )

    if clearly_after and not clearly_before:
        validation_after_calibration = True
        validation_gap = after_gap
        supplied_gap_timestamp = validation_start

    elif clearly_before and not clearly_after:
        validation_after_calibration = False
        validation_gap = before_gap
        supplied_gap_timestamp = calibration_start

    else:
        after_score = _orientation_score(
            after_calibration=True,
            calibration=calibration,
            validation=validation,
            warmup_months=warmup.months,
            calibration_end_used=calibration_duration.used,
            validation_end_used=validation_duration.used,
            gap=after_gap,
        )

        before_score = _orientation_score(
            after_calibration=False,
            calibration=calibration,
            validation=validation,
            warmup_months=warmup.months,
            calibration_end_used=calibration_duration.used,
            validation_end_used=validation_duration.used,
            gap=before_gap,
        )

        if after_score <= before_score:
            validation_after_calibration = True
            validation_gap = after_gap
            supplied_gap_timestamp = validation_start
        else:
            validation_after_calibration = False
            validation_gap = before_gap
            supplied_gap_timestamp = calibration_start

        messages.append(
            "The validation position was ambiguous. "
            "Used validation_window_after_calibration="
            f"{str(validation_after_calibration).lower()} because "
            "that orientation required the smaller total timestamp "
            "adjustment."
        )

    _append_inference_message(
        messages,
        "validation_window_gap",
        supplied_gap_timestamp,
        validation_gap,
    )

    time_controls = {
        "simulation_start_time": _format_datetime(
            simulation_start
        ),
        "warmup_duration": warmup.months,
        "calibration_duration": calibration_duration.months,
        "validation_window_gap": validation_gap.months,
        "validation_window_after_calibration": (
            validation_after_calibration
        ),
        "validation_duration": validation_duration.months,
    }

    return time_controls, messages


class _MonthMatch:
    """
    Result of matching a timestamp to a whole-calendar-month boundary.
    """

    def __init__(
            self,
            months: int,
            used: datetime,
            difference: timedelta,
            used_inclusive_end: bool,
    ):
        self.months = months
        self.used = used
        self.difference = difference
        self.used_inclusive_end = used_inclusive_end


def _parse_time_object(
        object_name: str,
        values: dict,
        required_fields: tuple[str, ...],
        messages: list[str],
) -> dict[str, datetime]:
    """
    Validate and parse one legacy time-range object.
    """
    missing_fields = [
        field
        for field in required_fields
        if not values.get(field)
    ]

    if missing_fields:
        raise ValueError(
            f"{object_name} is missing required field(s): "
            f"{', '.join(missing_fields)}"
        )

    return {
        field: _parse_datetime(
            values[field],
            f"{object_name}.{field}",
            messages,
        )
        for field in required_fields
    }


def _parse_datetime(
        value: Any,
        field_name: str,
        messages: list[str],
) -> datetime:
    """
    Parse an ISO-8601 datetime from a legacy job.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a non-empty ISO-8601 string"
        )

    normalized = value.strip()

    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError(
            f"{field_name} is not a valid ISO-8601 datetime: "
            f"{value!r}"
        ) from e

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

        messages.append(
            f"{field_name} did not include a timezone; UTC was used."
        )

    return parsed


def _nearest_calendar_months(
        start: datetime,
        supplied: datetime,
        *,
        inclusive_end: bool = False,
) -> _MonthMatch:
    """
    Find the nearest non-negative whole-calendar-month boundary.

    For duration end timestamps, both the supplied timestamp and one hour
    after it are considered. This supports inclusive 23:00 end timestamps
    and exclusive 00:00 boundaries.
    """
    candidates = [
        (
            supplied,
            False,
        )
    ]

    if inclusive_end:
        candidates.append(
            (
                supplied + timedelta(hours=1),
                True,
            )
        )

    best_match: _MonthMatch | None = None

    for effective_boundary, used_inclusive_end in candidates:
        approximate_months = (
                (effective_boundary.year - start.year) * 12
                + effective_boundary.month
                - start.month
        )

        first_month = max(
            0,
            approximate_months - 2,
        )

        last_month = max(
            0,
            approximate_months + 2,
        )

        for months in range(
                first_month,
                last_month + 1,
        ):
            expected_boundary = _add_calendar_months(
                start,
                months,
            )

            difference = _absolute_timedelta(
                expected_boundary - effective_boundary
            )

            used_timestamp = (
                expected_boundary - timedelta(hours=1)
                if used_inclusive_end
                else expected_boundary
            )

            candidate = _MonthMatch(
                months=months,
                used=used_timestamp,
                difference=difference,
                used_inclusive_end=used_inclusive_end,
            )

            if (
                    best_match is None
                    or candidate.difference < best_match.difference
            ):
                best_match = candidate

            elif (
                    candidate.difference == best_match.difference
                    and candidate.used_inclusive_end
                    and not best_match.used_inclusive_end
            ):
                best_match = candidate

    if best_match is None:
        raise ValueError(
            "Unable to infer a calendar-month duration"
        )

    return best_match


def _orientation_score(
        *,
        after_calibration: bool,
        calibration: dict[str, datetime],
        validation: dict[str, datetime],
        warmup_months: int,
        calibration_end_used: datetime,
        validation_end_used: datetime,
        gap: _MonthMatch,
) -> timedelta:
    """
    Score a possible validation orientation.

    The lower score represents the orientation that requires the smaller
    total adjustment to the legacy ranges.
    """
    if after_calibration:
        expected_simulation_start = calibration[
            "simulation_start_time"
        ]

        expected_simulation_end = validation_end_used

    else:
        expected_simulation_start = _add_calendar_months(
            validation["validation_start_time"],
            -warmup_months
        )

        expected_simulation_end = calibration_end_used

    return (
            gap.difference
            + _absolute_timedelta(validation["simulation_start_time"] - expected_simulation_start)
            + _absolute_timedelta(validation["simulation_end_time"] - expected_simulation_end)
    )


def _append_inference_message(
        messages: list[str],
        control_name: str,
        supplied: datetime,
        match: _MonthMatch,
        *,
        inclusive_end: bool = False,
) -> None:
    """
    Report any assumption or rounding used for an inferred control.
    """
    if match.difference:
        messages.append(
            f"{control_name} was rounded to "
            f"{match.months} calendar "
            f"month{'s' if match.months != 1 else ''}. "
            f"Received {_format_datetime(supplied)}; "
            f"used {_format_datetime(match.used)} instead "
            f"(difference: "
            f"{_format_timedelta(match.difference)})."
        )

    elif (
            inclusive_end
            and not match.used_inclusive_end
    ):
        messages.append(
            f"{control_name} used the exclusive boundary "
            f"{_format_datetime(match.used)} rather than an "
            "inclusive 23:00 ending timestamp."
        )


def _add_calendar_months(
        value: datetime,
        months: int,
) -> datetime:
    """
    Add calendar months, clamping the day when necessary.
    """
    month_index = (
            value.year * 12
            + value.month
            - 1
            + months
    )

    year, zero_based_month = divmod(
        month_index,
        12,
    )

    month = zero_based_month + 1

    day = min(
        value.day,
        calendar.monthrange(year, month)[1],
    )

    return value.replace(
        year=year,
        month=month,
        day=day,
    )


def _format_datetime(value: datetime) -> str:
    """
    Format a datetime as ISO-8601.

    UTC values use a trailing Z.
    """
    if value.utcoffset() == timedelta(0):
        return (
            value.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    return value.isoformat()


def _format_timedelta(value: timedelta) -> str:
    """
    Format a timedelta as a readable duration.
    """
    total_seconds = int(
        _absolute_timedelta(value).total_seconds()
    )

    days, remainder = divmod(
        total_seconds,
        86400,
    )

    hours, remainder = divmod(
        remainder,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    parts = []

    for amount, unit in (
            (days, "day"),
            (hours, "hour"),
            (minutes, "minute"),
            (seconds, "second"),
    ):
        if amount:
            parts.append(
                f"{amount} "
                f"{unit}{'' if amount == 1 else 's'}"
            )

    return ", ".join(parts) if parts else "0 seconds"


def _absolute_timedelta(
        value: timedelta,
) -> timedelta:
    """
    Return the absolute value of a timedelta.
    """
    return (
        value
        if value >= timedelta(0)
        else -value
    )


def save_converted_job_data(
        source_path: str,
        job_data: dict,
) -> str:
    """
    Save converted job data beside the original legacy JSON file.

    The converted filename is created by adding ``_converted`` to the
    original filename. For example:

        calibration_job.json
        calibration_job_converted.json

    An existing converted file with the same name is overwritten.

    :param source_path: Path to the original legacy JSON file.
    :param job_data: Converted job data.
    :return: Path to the saved converted JSON file.
    :raises OSError: If the converted file cannot be written.
    """
    original_path = Path(source_path)

    extension = original_path.suffix or ".json"
    converted_path = original_path.with_name(
        f"{original_path.stem}_converted{extension}"
    )

    with converted_path.open("w", encoding="utf-8") as output_file:
        json.dump(
            job_data,
            output_file,
            indent=2,
        )
        output_file.write("\n")

    return str(converted_path)
