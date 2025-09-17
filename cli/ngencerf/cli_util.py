import ast
import json


def check_http_error(http_status: int, response: str, content_type: str | None = None) -> tuple[dict | None, bool]:
    """
    Handles HTTP errors, returning the parsed response for 200 status codes,
    and printing appropriate error messages for other status codes.

    If a 401 Unauthorized is received, attempt to refresh or fall back to full login.

    :param http_status: The HTTP status code returned by the server.
    :param http_status: The HTTP status code returned by the server.
    :param response: The raw response text from the server.
    :param content_type: Optional content type string for handling non-JSON responses.
    :return: A tuple containing the parsed JSON response (or None) and a boolean indicating success.
    """
    try:
        # If it's a binary response (e.g., ZIP file), don't try to parse it as JSON
        if http_status == 200:
            if content_type and not content_type.startswith("application/json"):
                return None, True

            try:
                response_json = json.loads(response)
                return response_json, True
            except json.JSONDecodeError:
                print("Warning: Response is not valid JSON.")
                return None, False

        # Handle expired/invalid token
        if http_status == 401:
            print("Unauthorized (401): Access token may have expired. Attempting refresh...")
            from ngencerf.cli_user import refresh_access_token, _perform_full_login

            if refresh_access_token():
                print("[DEBUG] Refresh succeeded. Please retry request.")
                return {"detail": "Access token refreshed. Please retry request."}, False
            else:
                print("[DEBUG] Refresh failed. Prompting for full login...")
                if _perform_full_login():
                    return {"detail": "Full login performed. Please retry request."}, False
                else:
                    return {"detail": "Full login failed."}, False

        # Handle 400 Bad Request with specific error handling
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
                                for item in parsed:
                                    print(item.get("message", str(item)))
                            elif isinstance(parsed, dict):
                                print(parsed.get("message", str(parsed)))
                            else:
                                print(parsed)
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
