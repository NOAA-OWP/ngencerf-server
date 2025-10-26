import ast
import itertools
import json
import sys
import threading
import time


def check_http_error(http_status: int, response: str, content_type: str | None = None, retry_func=None, retry_message: str | None = None) -> tuple[dict | None, bool]:
    """
    Handles HTTP errors, returning the parsed response for 200 status codes,
    and printing appropriate error messages for other status codes.

    - For 200 responses: parses and returns JSON if applicable; non-JSON content is treated as success.
    - For 401 Unauthorized: attempts token refresh; if that fails, performs a full login.
      If either succeeds and `retry_func` is provided, re-executes the original request once.
    - For 400 Bad Request and other errors: prints structured error messages.

    :param http_status: The HTTP status code returned by the server.
    :param response: The raw response text from the server.
    :param content_type: Optional content type string for handling non-JSON responses.
    :param retry_func: Optional callable that performs the original request again after
        reauthentication succeeds.
        - Should take no arguments and return a response-like object with:
            - `status_code` (int)
            - `text` (str)
            - `json()` (callable returning parsed JSON)
    :param retry_message: Optional message to display with the spinner when retrying
        (e.g., "Importing job...").
    :return: A tuple of (parsed_response, success_flag)
             - parsed_response: dict or None
             - success_flag: True if request succeeded or was retried successfully; False otherwise.
    """
    try:
        # If it's a binary response (e.g., ZIP file), don't try to parse it as JSON
        if http_status == 200:
            # Non-JSON (e.g., streaming ZIP) is still success
            if content_type and not content_type.startswith("application/json"):
                return None, True

            try:
                response_json = json.loads(response)
                return response_json, True
            except json.JSONDecodeError:
                print("Warning: Response is not valid JSON.")
                return None, False

        # 401 Unauthorized → refresh, then full login, then single retry (with spinner) if possible
        if http_status == 401:
            print("Unauthorized (401): Access token may have expired. Attempting refresh...")

            from ngencerf.cli_user import refresh_access_token, perform_full_login

            token_fixed = False
            if refresh_access_token():
                token_fixed = True
            else:
                # Access token expired and refresh failed (or not present).
                # Prompt user for credentials (shows default email; allows enter-to-accept).
                print("[DEBUG] Refresh failed. Prompting for full login...")
                if perform_full_login():
                    token_fixed = True

            if token_fixed and retry_func:
                print("[DEBUG] Retrying request with new token...")
                # Show spinner on the retry using the same message, prefixed
                spinner_label = f"Retrying: {retry_message}..." if retry_message else "Retrying request..."

                # Give user a spinner while retry request is in progress ===
                retry_spinner = Spinner(spinner_label)
                retry_spinner.start()
                try:
                    new_response = retry_func()
                finally:
                    retry_spinner.stop()
                # === END NEW ===

                # If retry succeeded
                if new_response.status_code == 200:
                    try:
                        return new_response.json(), True
                    except Exception:
                        return None, True
                else:
                    # On failed retry after token fix → treat like normal failure
                    return new_response.text, False

            return {"detail": "Token fixed, but no retry performed."}, token_fixed

        # Handle 400 Bad Request
        if http_status == 400:
            print("Server returned HTTP 400 Bad Request.")
            response_json = json.loads(response)
            response_type = response_json.get("response_type", "")

            # Handle known response types separately
            if response_type == "error":
                raw_message = response_json.get("message", "Unknown error occurred.")

                # Handle stringified list or dict
                if isinstance(raw_message, str):
                    try:
                        if raw_message.strip().startswith(("[", "{")):
                            try:
                                # Try parsing as JSON first
                                parsed = json.loads(raw_message)
                            except json.JSONDecodeError:
                                parsed = ast.literal_eval(raw_message)

                            if isinstance(parsed, list):
                                # list of dicts? list of strings? handle both safely
                                for item in parsed:
                                    if isinstance(item, dict):
                                        print(item.get("message", str(item)))
                                    else:
                                        print(str(item))
                                return None, False  # and bail out cleanly

                            elif isinstance(parsed, dict):
                                print(parsed.get("message", str(parsed)))
                                return None, False

                            else:
                                print(parsed)
                                return None, False
                        else:
                            print(raw_message)
                    except Exception as e:
                        print("Fallback parse failed:", e)
                        print(raw_message)
                else:
                    print(raw_message)

                if validation_errors := response_json.get("validation_errors"):
                    _print_validation_errors(validation_errors)
                if errors := response_json.get("errors"):
                    print("Errors:")
                    for e in errors:
                        print(f"   {e}")

            elif response_type == "validation_error":
                message = response_json.get("message", "Validation error occurred.")
                print(message)
                validation_errors = response_json.get("validation_errors", {})
                _print_validation_errors(validation_errors)

            else:
                _pretty_print_json(response)

            return None, False

        # Handle all other non-200 status codes
        try:
            print(f"Error: Server returned HTTP status code {http_status}. Response:")
            _pretty_print_json(response)
        except json.JSONDecodeError:
            # Fallback for non-JSON responses
            print(f"Error: Server returned HTTP status code {http_status}. Response:")
            lines = response.strip().splitlines()
            print("\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else ""))

        return None, False

    except json.JSONDecodeError:
        # Fallback to raw response if JSON parsing fails at the initial check
        print(f"Error: Server returned HTTP status code {http_status}. Response:")
        lines = response.strip().splitlines()
        print("\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else ""))
        return None, False


def _print_validation_errors(errors: dict | list, prefix: str = "  ") -> None:
    """
    Recursively prints validation errors, handling both field-specific and nested errors.
    """
    print("Validation errors:")
    if isinstance(errors, dict):
        for field, error_list in errors.items():
            # Handle nested dictionaries
            if isinstance(error_list, dict):
                _print_validation_errors(error_list, prefix=f"{prefix}{field}.")
            # Handle lists of errors
            elif isinstance(error_list, list):
                for error in error_list:
                    # Handle nested error objects like ErrorDetail
                    if isinstance(error, dict):
                        _print_validation_errors(error, prefix=f"{prefix}{field}.")
                    else:
                        print(f"{prefix}{field}: {error}")
            else:
                print(f"{prefix}{field}: {error_list}")
    elif isinstance(errors, list):
        for error in errors:
            print(f"{prefix}{error}")
    else:
        print(f"{prefix}{errors}")


def _pretty_print_json(response: str, suppress_html: bool = False):
    try:
        parsed = json.loads(response)
        print(json.dumps(parsed, indent=3))
    except json.JSONDecodeError:
        if suppress_html:
            return  # do not print anything for 404 HTML errors
        # print only the first 10 lines of non-JSON response
        lines = response.strip().splitlines()
        print("\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else ""))


class Spinner:
    def __init__(self, message="Processing..."):
        self.spinner = itertools.cycle(["|", "/", "-", "\\"])
        self.running = False
        self.thread = None
        self.message = message

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin)
        self.thread.start()

    def _spin(self):
        print(self.message, end=" ", flush=True)
        while self.running:
            sys.stdout.write(next(self.spinner))
            sys.stdout.flush()
            time.sleep(0.1)
            sys.stdout.write("\b")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        sys.stdout.write(" \n")
        sys.stdout.flush()
