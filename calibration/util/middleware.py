import json
import logging
import time

from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

CALIBRATION_PREFIX = "/calibration/"
AUTH_PREFIX = "/auth/"


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


class ApiRequestDiagnosticsMiddleware(MiddlewareMixin):
    """
    Logs diagnostic details for failed API requests.
    """

    def process_request(self, request):
        path = request.path

        if not path.startswith((CALIBRATION_PREFIX, AUTH_PREFIX)):
            return None

        try:
            raw_body = request.body.decode("utf-8", errors="ignore")

            if "application/json" in request.META.get("CONTENT_TYPE", ""):
                parsed = json.loads(raw_body)
                sanitized = redact_sensitive_data(parsed)
                request._diagnostic_body = json.dumps(sanitized)
            else:
                request._diagnostic_body = raw_body

        except Exception as e:
            request._diagnostic_body = f"<unreadable: {type(e).__name__}: {e}>"

        return None

    def process_response(self, request, response):
        path = request.path

        # Only inspect calibration endpoints
        if not path.startswith((CALIBRATION_PREFIX, AUTH_PREFIX)):
            return response

        body = getattr(request, "_diagnostic_body", "<not captured>")

        try:
            user = getattr(request, "user", None)
            user_str = (
                user.email
                if hasattr(user, "email") and user.is_authenticated
                else "Anonymous"
            )
        except Exception:
            user_str = "Unknown"

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        ip = request.META.get("REMOTE_ADDR")

        if path.startswith(CALIBRATION_PREFIX) and response.status_code in (404, 405):
            logger.warning(
                f"UNMATCHED API REQUEST: {path} "
                f"method={request.method} user={user_str} ip={ip} "
                f"user_agent='{user_agent}' body='{body}'"
            )

        elif path.startswith(AUTH_PREFIX) and response.status_code >= 400:
            try:
                response_body = response.content.decode("utf-8", errors="ignore")
            except Exception as e:
                response_body = f"<unreadable: {type(e).__name__}: {e}>"

            logger.warning(
                f"AUTH API REQUEST FAILED: {path} "
                f"method={request.method} status={response.status_code} "
                f"user={user_str} ip={ip} "
                f"content_type='{request.META.get('CONTENT_TYPE', '')}' "
                f"user_agent='{user_agent}' "
                f"body='{body}' response_body='{response_body}'"
            )

        return response


SENSITIVE_FIELDS = {
    "password",
    "re_password",
    "current_password",
    "new_password",
    "token",
    "access",
    "refresh",
    "mfa_token",
}


def redact_sensitive_data(value):
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k in SENSITIVE_FIELDS else redact_sensitive_data(v))
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [redact_sensitive_data(v) for v in value]

    return value
