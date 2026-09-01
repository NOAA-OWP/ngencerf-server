import getpass
import os

import qrcode
import requests
from qrcode.image.pil import PilImage

from ngencerf.cli_config import get_ngencerf_base_url, load_ngencerf_env, save_to_env_file, decode_env_password, encode_env_password, \
    remove_from_env_file
from ngencerf.cli_util import check_http_error


def _endpoint(path: str) -> str:
    """
    Build a full API endpoint URL using the currently configured ngenCerf server.
    """
    return f"{get_ngencerf_base_url()}{path}"


def save_credentials_to_env_file(email: str, password: str) -> None:
    """
    Persist email and obfuscated password to ~/.ngencerf_env so user isn't prompted every time.
    """
    save_to_env_file("NGEN_EMAIL", email)
    save_to_env_file("NGEN_PASSWORD", encode_env_password(password))


def remember_login_credentials(email: str, password: str) -> None:
    """
    Remember the email and password entered during a successful login step.

    This is used before MFA completion so a restarted login does not prompt
    the user to re-enter credentials that were already accepted by the server.
    """
    os.environ["NGEN_EMAIL"] = email
    os.environ["NGEN_PASSWORD"] = encode_env_password(password)

    save_credentials_to_env_file(email, password)


def ngen_login() -> bool:
    """
    Ensures there is some ACCESS_TOKEN available.

    Logic:
      1. If ACCESS_TOKEN exists AND REFRESH_TOKEN exists → use access token (refresh will be attempted on 401).
      2. If ACCESS_TOKEN exists BUT no REFRESH_TOKEN → treat as expired, do full login.
      3. If no ACCESS_TOKEN but REFRESH_TOKEN exists → try refresh.
      4. If neither exist → full login.
    """
    load_ngencerf_env()

    access_token = os.environ.get("ACCESS_TOKEN")
    refresh_token = os.environ.get("REFRESH_TOKEN")

    # Case 1: Both tokens exist → trust access token, let 401 trigger refresh
    if access_token and refresh_token:
        print("Using existing access token (refresh token available).")
        return True

    # Case 2: Access token exists but no refresh token → treat as expired
    if access_token and not refresh_token:
        print("Access token found but no refresh token. Performing full login.")
        return perform_full_login()

    # Case 3: No access token, but refresh token exists → try refresh
    if refresh_token:
        print("No access token found. Attempting refresh...")
        if refresh_access_token():
            print("Refresh succeeded. Using new access token.")
            return True

        print("Refresh failed. Performing full login...")
        return perform_full_login()

    # Case 4: Neither token exists → full login
    print("No tokens found. Performing full login.")
    return perform_full_login()


def perform_full_login(_retry: bool = False) -> bool:
    """
    Perform a full login using stored or prompted credentials.
    Supports MFA setup and verification flows.

    Will re-prompt once on failure (but never loops indefinitely).
    """
    print("Performing full login with email/password.")

    # Load latest env
    load_ngencerf_env()

    # Always load the latest email from env if available
    email = os.environ.get("NGEN_EMAIL") or os.environ.get("NGEN_USERNAME")

    # Decide whether to prompt for email
    # RULE:
    #   - If no email at all → must prompt
    #   - If retry → do NOT prompt (email is trusted)
    #   - If first attempt AND no saved password → allow optional override
    #   - If first attempt AND saved password exists → SKIP prompt entirely
    if not email:
        remove_from_env_file({"NGEN_PASSWORD"})
        # Only prompt if email truly unknown
        email = input("ngenCerf email: ").strip()
    elif not _retry and "NGEN_PASSWORD" not in os.environ:
        # Only offer override on FIRST attempt
        entered = input(f"ngenCerf email [{email}]: ").strip()
        if entered:
            email = entered
            remove_from_env_file({"NGEN_PASSWORD"})
    # else: email prompt is skipped entirely

    # PASSWORD STRATEGY:
    #   - First attempt: use saved password if present, otherwise prompt
    #   - Retry attempt: always force prompt
    if _retry:
        print("Your saved credentials appear to be invalid. Please re-enter your password.")
        remove_from_env_file({"NGEN_PASSWORD"})
        # On retry, always force prompt for new password
        password = getpass.getpass("ngenCerf password: ")
    else:
        # Use stored password or prompt if missing
        password = os.environ.get("NGEN_PASSWORD")
        if password:
            password = decode_env_password(password)
        else:
            password = getpass.getpass("ngenCerf password: ")

    # ───────────────────────────────
    # Step 1: Call /auth/login/
    # ───────────────────────────────
    payload = {"email": email, "password": password}
    print("Logging in with", email)

    response = requests.post(_endpoint("/auth/login/"), json=payload)

    if response.status_code != 200:
        if response.status_code == 401:
            print("Login failed — incorrect email or password.")

            # Only clear saved password when the server explicitly rejected credentials.
            print("Saved password failed. Prompting for new credentials...")
            _clear_saved_password()
            os.environ.pop("NGEN_PASSWORD", None)

            if not _retry:
                print("Saved password failed — retrying full login...")
                return perform_full_login(_retry=True)

            print("Second login attempt failed. Aborting.")
            return False

        check_http_error(
            response.status_code,
            response.text,
            response.url,
            response.headers.get("Content-Type"),
        )
        print(f"Login failed with HTTP {response.status_code}. Check the configured server URL: {get_ngencerf_base_url()}")
        return False

    # Success case
    response_json = response.json()

    # Normal login path: MFA is not required if the server returned tokens directly.
    if _handle_token_response(response_json, email, password):
        return True

    # MFA setup path: user has not configured MFA yet.
    if response_json.get("mfa_setup_required"):
        mfa_token = response_json.get("mfa_token")

        print("\nMFA setup required.")

        # Request MFA setup information from the server.
        # Response includes the QR-code URL and authenticator secret.
        setup_resp = requests.post(
            _endpoint("/auth/mfa/setup/"),
            json={"mfa_token": mfa_token},
        )

        if setup_resp.status_code != 200:
            check_http_error(
                setup_resp.status_code,
                setup_resp.text,
                setup_resp.url,
                setup_resp.headers.get("Content-Type"),
            )
            return False

        setup_json = setup_resp.json()
        otpauth_url = setup_json.get("otpauth_url")
        authenticator_key = setup_json.get("authenticator_key")

        print("\nOpening QR code for MFA setup...")
        print("\nScan the QR code or enter this secret key into your authenticator app.")

        try:
            img = qrcode.make(otpauth_url, image_factory=PilImage)
            img.show()
        except Exception as e:
            print(f"Failed to open QR code window: {e}")
            print("\nFallback: paste this into a QR generator or enter manually:")
            print(otpauth_url)

        print(f"Secret key: {authenticator_key}")

        code = _prompt_mfa_code("\nEnter the 6-digit code from your authenticator app: ")

        # Confirm MFA setup using the authenticator code.
        # Server returns recovery codes after successful confirmation.
        confirm_resp = requests.post(
            _endpoint("/auth/mfa/setup/confirm/"),
            json={
                "mfa_token": mfa_token,
                "code": code,
            },
        )

        if confirm_resp.status_code != 200:
            check_http_error(
                confirm_resp.status_code,
                confirm_resp.text,
                confirm_resp.url,
                confirm_resp.headers.get("Content-Type"),
            )
            return False

        confirm_json = confirm_resp.json()

        recovery_codes = confirm_json.get("recovery_codes", [])
        if isinstance(recovery_codes, list):
            _print_recovery_codes(recovery_codes)

        remember_login_credentials(email, password)

        input("\nPress Enter after saving recovery codes...")

        print("Restarting login to complete MFA...")
        return perform_full_login(_retry=_retry)

    # MFA verification path: user already has MFA configured.
    if response_json.get("mfa_required"):
        mfa_token = response_json.get("mfa_token")

        code = _prompt_mfa_code("Enter the 6-digit authenticator code or a recovery code: ")

        # Verify MFA login challenge using an authenticator code or recovery code.
        verify_resp = requests.post(
            _endpoint("/auth/mfa/verify/"),
            json={
                "mfa_token": mfa_token,
                "code": code,
            },
        )

        if verify_resp.status_code != 200:
            check_http_error(
                verify_resp.status_code,
                verify_resp.text,
                verify_resp.url,
                verify_resp.headers.get("Content-Type"),
            )
            return False

        verify_json = verify_resp.json()
        return _handle_token_response(verify_json, email, password)

    print("Unexpected login response.")
    return False


def create_local_user(optional_email: str | None = None) -> int:
    """
    Create a local-only user.

    Requires the current CLI user to be authenticated and authorized as staff/admin.
    """
    email = optional_email or input("Local-only user email: ").strip()
    first_name = input("First name: ").strip()
    last_name = input("Last name: ").strip()

    while True:
        password = getpass.getpass("New local-only user password: ")
        password_confirm = getpass.getpass("Confirm password: ")

        if password == password_confirm:
            break

        print("Passwords do not match. Please try again.")

    payload = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "password": password,
    }

    headers = {
        "Authorization": f"Bearer {os.environ.get('ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        _endpoint("/auth/create_local_user/"),
        headers=headers,
        json=payload,
    )

    response_json, success = check_http_error(
        response.status_code,
        response.text,
        response.url,
        response.headers.get("Content-Type"),
    )

    if not success:
        return 1

    if isinstance(response_json, dict):
        print(response_json.get("message", "Local-only user created successfully."))

    return 0


def change_password(target_email: str | None = None) -> int:
    """
    Change a password.

    If target_email is supplied, this is an admin reset for another user.
    If target_email is omitted, this is a self-service password change.
    """
    if target_email:
        print(f"Changing password for user: {target_email}")
    else:
        print("Changing password for current user.")

    current_password = None
    if not target_email:
        current_password = getpass.getpass("Current password: ")

    while True:
        new_password = getpass.getpass("New password: ")
        new_password_confirm = getpass.getpass("Confirm new password: ")

        if new_password == new_password_confirm:
            break

        print("Passwords do not match. Please try again.")

    payload = {
        "new_password": new_password,
    }

    # Admin reset mode. Server determines whether the current user is allowed.
    if target_email:
        payload["email"] = target_email.strip().lower()
    else:
        payload["current_password"] = current_password

    headers = {
        "Authorization": f"Bearer {os.environ.get('ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        _endpoint("/auth/change_password/"),
        headers=headers,
        json=payload,
    )

    response_json, success = check_http_error(
        response.status_code,
        response.text,
        response.url,
        response.headers.get("Content-Type"),
    )

    if not success:
        return 1

    if isinstance(response_json, dict):
        print(response_json.get("message", "Password changed successfully."))
    else:
        print("Password changed successfully.")

    # If the current user's password changed, clear the saved password.
    if not target_email:
        _clear_saved_password()
        print("Saved password cleared. Use the new password at next login.")

    return 0


def _clear_saved_password() -> None:
    """
    Remove only the saved password so the user is reprompted.
    """
    print("Clearing invalid saved password from ~/.ngencerf_env...")
    remove_from_env_file({"NGEN_PASSWORD"})


def _clear_auth_state() -> None:
    """
    Remove tokens and stored password to ensure a clean retry.
    """
    remove_from_env_file({"ACCESS_TOKEN", "REFRESH_TOKEN", "NGEN_PASSWORD"})
    print("Cleared invalid tokens and password from ~/.ngencerf_env.")


def refresh_access_token() -> bool:
    """
    Attempts to refresh the access token using REFRESH_TOKEN in environment variables.

    This avoids prompting the user for credentials when the refresh token is still valid.
    Updates ~/.ngencerf_env if successful.

    Returns:
        True if refresh succeeded, False otherwise.
    """
    load_ngencerf_env()

    refresh_token = os.environ.get("REFRESH_TOKEN")
    if not refresh_token:
        return False

    payload = {"refresh": refresh_token}
    response = requests.post(_endpoint("/auth/jwt/refresh"), json=payload)

    if response.status_code != 200:
        print(
            f"Refresh failed with status {response.status_code} "
            f"for URL: {response.url}. Response: {response.text}"
        )
        return False

    response_json = response.json()
    access_token = response_json.get("access")
    if not access_token:
        print("Refresh response missing access token.")
        return False

    os.environ["ACCESS_TOKEN"] = access_token
    save_to_env_file("ACCESS_TOKEN", access_token)

    print("Access token refreshed.\n")
    return True


def ngen_register(optional_email: str | None = None) -> int:
    """
    Registers a new user for the NGEN API.
    Prompts for password input and confirmation.
    """
    load_ngencerf_env()

    auth_config = get_auth_config()

    if not auth_config.get("allow_self_registration", True):
        print("Self-registration is disabled for this server.")
        print("Contact your system administrator if you need access.")
        return 1

    email = optional_email or os.environ.get("NGEN_EMAIL") or os.environ.get("NGEN_USERNAME")
    if not email:
        email = input("Enter a new email for ngenCerf registration: ")

    while True:
        password = getpass.getpass("Enter a new password for ngenCerf registration: ")
        password_confirm = getpass.getpass("Confirm your password: ")
        if password == password_confirm:
            break
        print("Passwords do not match. Please try again.")

    payload = {
        "email": email,
        "password": password,
        "re_password": password_confirm,
    }

    response = requests.post(_endpoint("/auth/users/"), json=payload)
    _, success = check_http_error(
        response.status_code,
        response.text,
        response.url,
        response.headers.get("Content-Type"),
    )

    if success:
        print(f"User '{email}' registered successfully.")
        return 0

    return 1


def _save_tokens(access_token: str, refresh_token: str | None, email: str, password: str) -> None:
    """
    Persist authentication tokens and user credentials to the environment file.

    Stores:
      - ACCESS_TOKEN
      - REFRESH_TOKEN, if present
      - NGEN_EMAIL
      - NGEN_PASSWORD
    """
    os.environ["ACCESS_TOKEN"] = access_token

    remember_login_credentials(email, password)
    save_to_env_file("ACCESS_TOKEN", access_token)

    if refresh_token:
        os.environ["REFRESH_TOKEN"] = refresh_token
        save_to_env_file("REFRESH_TOKEN", refresh_token)


def _handle_token_response(response_json: dict, email: str, password: str) -> bool:
    """
    Handle successful token responses from normal login or MFA verification.

    If the response contains an access token, persist the token, refresh token,
    email, and password for future CLI calls.

    Returns:
        True if tokens were found and saved, False otherwise.
    """
    access_token = response_json.get("access")
    refresh_token = response_json.get("refresh")

    if not isinstance(access_token, str):
        return False
    assert isinstance(access_token, str)

    if refresh_token is not None and not isinstance(refresh_token, str):
        refresh_token = None

    _save_tokens(access_token, refresh_token, email, password)
    print(f"{email} login successful.\n")
    return True


def _prompt_mfa_code(prompt: str = "MFA code or recovery code: ") -> str:
    """
    Prompt the user for an MFA authenticator code or recovery code.
    """
    return input(prompt).strip()


def _print_recovery_codes(recovery_codes: list[str]) -> None:
    """
    Display MFA recovery codes returned by the server after MFA setup.
    """
    print("\nMFA setup completed.")
    print("Save these recovery codes now. They will not be shown again.\n")

    for code in recovery_codes:
        print(f"  {code}")

    print()


def get_auth_config() -> dict:
    """
    Retrieve server authentication configuration.

    This endpoint is unauthenticated and tells the CLI whether public
    registration and password changes are allowed.
    """
    try:
        response = requests.get(_endpoint("/auth/config/"), timeout=10)

        if response.status_code == 200:
            response_json = response.json()
            if isinstance(response_json, dict):
                return response_json

    except requests.RequestException:
        pass

    # Conservative fallback for older servers or temporary config endpoint failure.
    return {
        "active_directory_enabled": False,
        "allow_self_registration": True,
        "allow_password_change": True,
    }
