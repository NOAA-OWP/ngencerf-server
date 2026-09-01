# calibration/util/logging_filters.py

import logging


class SuppressSuccessfulHealthCheckFilter(logging.Filter):
    """
    Suppress successful AWS health-check access logs.

    Failed health checks should still be logged.
    """

    HEALTH_CHECK_PATH = "/api/health_check/"

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        if (
            self.HEALTH_CHECK_PATH in message
            and '"GET ' in message
            and message.rstrip().endswith(" 200")
        ):
            return False

        return True
