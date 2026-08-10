import logging
import time

from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

CALIBRATION_PREFIX = "/calibration/"


class TimingMiddleware:
    """
    Middleware that measures request performance.

    - Records the total elapsed time for the request.
    - Also captures total database execution time and number of queries (if available).
    """

    def __init__(self, get_response):
        """
        Store the next middleware or view callable.

        :param get_response: The next middleware or final view in the request chain.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """
        Measure timing for the request end-to-end and attempt to include DB time.

        :param request: The incoming HttpRequest object.
        :return: The generated HttpResponse object.
        """
        # Capture high-precision start time
        request._start_time = time.perf_counter()

        # Let Django process the request (views + later middleware)
        response = self.get_response(request)

        # Measure total elapsed time
        total_elapsed = time.perf_counter() - request._start_time

        # Attempt to calculate database query time
        try:
            db_time = sum(float(q.get("time", 0)) for q in connection.queries)
            query_count = len(connection.queries)
        except Exception:
            db_time = None
            query_count = None

        # Log either full timing or fallback if DB metrics unavailable
        logger.debug(
            f"TimingMiddleware: total={total_elapsed:.3f}s, "
            f"db={db_time:.3f}s, queries={query_count} for path {request.path}"
            if db_time is not None
            else f"TimingMiddleware: total={total_elapsed:.3f}s (db stats unavailable) for path {request.path}"
        )

        return response


class LogUnmatchedCalibrationRequestsMiddleware(MiddlewareMixin):
    """
    Logs requests to /calibration/* that never reach DRF views
    and would normally only show 'Not Found' or 'Method Not Allowed'.
    """

    def process_view(self, request, view_func, view_args, view_kwargs):
        # If a view is matched, do nothing
        return None

    def process_response(self, request, response):
        path = request.path

        # Only inspect calibration endpoints
        if not path.startswith(CALIBRATION_PREFIX):
            return response

        # Case: URL not found or method not allowed
        if response.status_code in (404, 405):
            try:
                user = getattr(request, "user", None)
                user_str = (
                    user.email
                    if hasattr(user, "email") and user.is_authenticated
                    else "Anonymous"
                )
            except Exception:
                user_str = "Unknown"

            # Best-effort body capture
            try:
                body = request.body.decode("utf-8", errors="ignore")
            except Exception:
                body = "<unreadable>"

            user_agent = request.META.get("HTTP_USER_AGENT", "")
            ip = request.META.get("REMOTE_ADDR")

            logger.warning(
                f"UNMATCHED API REQUEST: {path} "
                f"method={request.method} user={user_str} ip={ip} "
                f"user_agent='{user_agent}' body='{body}'"
            )

        return response
