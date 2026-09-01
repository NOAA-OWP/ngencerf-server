import ast
import json
import sys
try:
    # Not available on Windows
    import termios
except ImportError:
    termios = None


def check_http_error(
        http_status: int,
        response: str,
        url: str,
        content_type: str | None = None,
        retry_func=None
) -> tuple[dict | None, bool]:
    """
    Handles HTTP responses, returning the parsed response for 200 status codes
    and printing appropriate error messages for other status codes.

    - For 200 responses: parses and returns JSON if applicable; non-JSON content is treated as success.
    - For 401 Unauthorized: attempts token refresh; if that fails, performs a full login.
      If either succeeds and `retry_func` is provided, re-executes the original request once.
    - For 400 Bad Request and other errors: prints structured error messages.

    :param http_status: The HTTP status code returned by the server.
    :param response: The raw response text from the server.
    :param url: The URL used for the request.
    :param content_type: Optional content type string for handling non-JSON responses.
    :param retry_func: Optional callable that performs and handles the original request
        again after reauthentication succeeds.
        - Should take no arguments and return a tuple of
          (parsed_response, success_flag).
    :return: A tuple of (parsed_response, success_flag)
             - parsed_response: dict or None
             - success_flag: True if request succeeded or was retried successfully; False otherwise.
    """
    try:
        # If it's a non-JSON response (e.g., a ZIP file), don't try to parse it as JSON
        if http_status == 200:
            # Non-JSON (e.g., streaming ZIP) is still success
            if content_type and not content_type.startswith("application/json"):
                return None, True

            try:
                response_json = json.loads(response)
                return response_json, True
            except json.JSONDecodeError:
                print(f"Warning: Response from {url} is not valid JSON.")
                return None, False

        # 401 Unauthorized → attempt refresh or full login, then retry once
        if http_status == 401:
            print(
                f"Unauthorized (401) from {url}: "
                "Access token may have expired. Attempting refresh..."
            )

            from ngencerf.cli_user import refresh_access_token, perform_full_login

            token_fixed = False
            if refresh_access_token():
                token_fixed = True
            else:
                # Access token expired and refresh failed (or not present).
                # Prompt user for credentials (shows default email; allows enter-to-accept).
                print("Refresh failed. Prompting for full login...")
                if perform_full_login():
                    token_fixed = True

            if token_fixed:
                if retry_func:
                    print("Retrying request with new token...")
                    return retry_func()

                return {
                    "detail": "Authentication succeeded, but no retry was performed."
                }, False

            return {"detail": "Authentication failed."}, False

        # Handle 400 Bad Request
        if http_status == 400:
            print(f"Server returned HTTP 400 Bad Request for URL: {url}")
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

        if http_status == 404:
            print(f"Error: Server returned HTTP 404 Not Found for URL: {url}")
            print(
                "Check that the configured server URL is correct and points "
                "to an ngenCerf server."
            )
            return None, False

        # Handle all other non-200 status codes
        print(
            f"Error: Server returned HTTP status code {http_status} "
            f"for URL: {url}. Response:"
        )
        _pretty_print_json(response)

        return None, False

    except json.JSONDecodeError:
        # Fallback to the raw response when an error response expected to contain JSON is invalid
        print(
            f"Error: Server returned HTTP status code {http_status} "
            f"for URL: {url}. Response:"
        )
        lines = response.strip().splitlines()
        print("\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else ""))
        return None, False


def _print_validation_errors(errors: dict | list, prefix: str = "  ") -> None:
    """
    Print only leaf-level validation messages, with a single heading.


    Examples of leaf nodes:
      - {"data": {"modules": {"0": ["Invalid module name ..."]}}}
        -> prints "data.modules.0: Invalid module name ..."
      - {"field": "This field is required."}
        -> prints "field: This field is required."
    """
    print("Validation errors:")

    def _walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                new_path = f"{path}.{key}" if path else str(key)
                _walk(value, new_path)
            return

        if isinstance(node, list):
            # If this is a leaf list (strings / scalars), print them.
            # If it's a list of dicts/lists, recurse into each item.
            for item in node:
                if isinstance(item, (dict, list)):
                    _walk(item, path)
                else:
                    print(f"{prefix}{path}: {item}")
            return

        # Scalar leaf
        print(f"{prefix}{path}: {node}")

    _walk(errors, "")


def _pretty_print_json(response: str, suppress_html: bool = False):
    try:
        parsed = json.loads(response)
        print(json.dumps(parsed, indent=3))
    except json.JSONDecodeError:
        if suppress_html:
            return  # Do not print non-JSON response content
        # print only the first 10 lines of non-JSON response
        lines = response.strip().splitlines()
        print("\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else ""))


def configure_terminal_backspace() -> None:
    """
    Normalize terminal input handling so Backspace works in interactive prompts.

    Some shells, containers, IDE terminals, or exec sessions can leave the TTY
    in a mode where Backspace is echoed as ^? instead of being handled as an
    erase character. This restores canonical line editing, enables normal echo,
    and sets DEL (^?) as the erase character.

    This is best-effort and does nothing when terminal settings cannot be changed.
    """
    if termios is None:
        return

    tty_file = None

    try:
        if sys.stdin.isatty():
            terminal = sys.stdin
        else:
            try:
                tty_file = open("/dev/tty", "rb", buffering=0)
                terminal = tty_file
            except OSError:
                return

        attrs = termios.tcgetattr(terminal)
        cc = attrs[6]
        current_erase = cc[termios.VERASE]

        # Use DEL (^?) as the erase character.
        if isinstance(current_erase, int):
            cc[termios.VERASE] = 0x7f
        else:
            cc[termios.VERASE] = b"\x7f"

        # Restore normal cooked/canonical terminal line editing.
        attrs[3] |= termios.ICANON
        attrs[3] |= termios.ECHO
        attrs[3] |= termios.ECHOE
        attrs[3] |= termios.ECHOK

        # Prevent control characters from being displayed as ^?.
        if hasattr(termios, "ECHOCTL"):
            attrs[3] &= ~termios.ECHOCTL

        termios.tcsetattr(terminal, termios.TCSANOW, attrs)

    except Exception:
        return

    finally:
        if tty_file is not None:
            tty_file.close()
