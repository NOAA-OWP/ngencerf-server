# calibration/util/aorc_date_range.py
"""
Utilities for determining the available NOAA AORC CONUS BMI date range.

The AORC forcing dataset is stored in a public NOAA S3 bucket with one
top-level prefix per year:

    s3://noaa-nws-aorc-v1-1-1km/
        1980/
        1981/
        ...
        2025/

Rather than hard-coding the latest available year in Django settings, this
module queries the bucket at application startup and derives the end date
dynamically. This allows newly published AORC years to become available
without requiring a code change.

If the bucket lookup fails, a configurable fallback year is used so that
application startup can continue.
"""

import logging
import re

from datetimerange import DateTimeRange

from calibration.util.cloud_util import list_s3_common_prefixes

logger = logging.getLogger(__name__)

AORC_BUCKET = "noaa-nws-aorc-v1-1-1km"
DEFAULT_END_YEAR = 2024


def get_latest_aorc_conus_bmi_year() -> int:
    """
    Determine the most recent year available in the public NOAA AORC bucket.

    The NOAA AORC CONUS BMI dataset is organized using top-level Zarr dataset
    prefixes of the form:

        s3://noaa-nws-aorc-v1-1-1km/
            1980.zarr/
            1981.zarr/
            ...
            2025.zarr/

    This function performs an unsigned public S3 listing of the bucket's
    immediate child prefixes, extracts the year from each ``<year>.zarr``
    prefix, and returns the highest year found.

    The result is used at application startup to dynamically determine the
    valid date range for AORC forcing data rather than hard-coding an end
    year in settings.py.

    :return:
        Latest available AORC year.

    :raises ValueError:
        If no valid ``<year>.zarr`` prefixes are found in the bucket.
    """
    prefixes = list_s3_common_prefixes(
        bucket=AORC_BUCKET,
        prefix="",
        delimiter="/",
        public=True,
    )

    years = []

    for prefix in prefixes:
        folder = prefix.strip("/")

        # NOAA AORC datasets are published as top-level prefixes such as:
        #   1980.zarr
        #   2025.zarr
        match = re.fullmatch(r"(19\d{2}|20\d{2})\.zarr", folder)

        if match:
            years.append(int(match.group(1)))

    if not years:
        raise ValueError(
            f"No valid <year>.zarr prefixes found in s3://{AORC_BUCKET}/"
        )

    return max(years)


def get_aorc_conus_bmi_date_range() -> DateTimeRange:
    """
    Return the valid AORC CONUS BMI forcing date range.

    The start date is fixed at the beginning of the dataset
    (1980-01-01 UTC). The end date is determined dynamically by inspecting
    the public NOAA AORC S3 bucket and selecting the highest available year.

    If the lookup fails for any reason (network issue, AWS outage, NOAA bucket
    changes, etc.), a conservative fallback year is used so application startup
    can continue.

    This function is intended to be called from Django settings.py during
    process startup. When Gunicorn is configured with ``--preload``, the
    lookup occurs once in the master process before worker processes are
    forked.

    :return:
        DateTimeRange covering the available AORC CONUS BMI data.
    """
    try:
        end_year = get_latest_aorc_conus_bmi_year()
    except Exception:
        logger.exception(
            "Unable to determine latest AORC CONUS BMI year; falling back to %s",
            DEFAULT_END_YEAR,
        )
        end_year = DEFAULT_END_YEAR

    date_range = DateTimeRange(
        "1980-01-01T00:00:00+0000",
        f"{end_year}-12-31T23:59:59+0000",
    )

    logger.info("FORCING_AORC_CONUS_BMI_DATE_RANGE resolved to %s", date_range)
    return date_range
