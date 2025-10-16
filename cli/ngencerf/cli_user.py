import getpass
import os

import requests

from ngencerf.cli_util import check_http_error

LOGIN_ENDPOINT = "http://localhost:8000/auth/jwt/create"
REFRESH_ENDPOINT = "http://localhost:8000/auth/jwt/refresh"
REGISTER_ENDPOINT = "http://localhost:8000/auth/users/"
ENV_FILE = os.path.join(os.path.expanduser("~"), ".ngencerf_env")


def save_to_env_file(key: str, value: str):
    """
    Save or update a key-value pair in ~/.ngencerf_env without duplication.

    If the key already exists, its value is updated. Otherwise, it's appended.
    """
    lines = []

    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        found = False
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")


def load_ngencerf_env():
    """
    Load variables from ~/.ngencerf_env into the environment if not already present.
    Ignores comments and blank lines.
    """
    if not os.path.exists(ENV_FILE):
        return

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key not in os.environ:
                os.environ[key] = value


def save_credentials_to_env_file(email: str, password: str):
    """
    Persist email and password to ~/.ngencerf_env so user isn't prompted every time.
    """
    save_to_env_file("NGEN_EMAIL", email)
    save_to_env_file("NGEN_PASSWORD", password)


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
        print("[DEBUG] Using existing ACCESS_TOKEN (with REFRESH_TOKEN available).")
        return True

    # Case 2: Access token exists but no refresh token → treat as expired
    if access_token and not refresh_token:
        print("[DEBUG] ACCESS_TOKEN found but no REFRESH_TOKEN. Treating as expired → full login required.")
        return perform_full_login()

    # Case 3: No access token, but refresh token exists → try refresh
    if refresh_token:
        print("[DEBUG] No ACCESS_TOKEN found. Attempting refresh...")
        if refresh_access_token():
            print("[DEBUG] Refresh succeeded. Using new ACCESS_TOKEN.")
            return True
        else:
            print("[DEBUG] Refresh failed. Falling back to full login...")
            return perform_full_login()

    # Case 4: Neither token exists → full login
    print("[DEBUG] No tokens found. Performing full login.")
    return perform_full_login()


def perform_full_login(_retry=False) -> bool:
    """
    Perform a full login using stored or prompted credentials.
    Will re-prompt once on failure (but never loops indefinitely).
    """
    print("[DEBUG] Performing full login with email/password.")

    # Always get latest email, but don't reload the password file on retry
    email = os.environ.get("NGEN_EMAIL") or os.environ.get("NGEN_USERNAME")
    if not email:
        email = input("ngenCerf email: ")
    else:
        # Prompt showing default email in brackets
        entered = input(f"ngenCerf email [{email}]: ").strip()
        if entered:
            email = entered

    # If we're retrying, force password prompt (don't trust any saved value)
    if _retry:
        # On retry, always force prompt for new password
        os.environ.pop("NGEN_PASSWORD", None)
        password = getpass.getpass("ngenCerf password: ")
    else:
        # Use stored password or prompt if missing
        password = os.environ.get("NGEN_PASSWORD")
        if not password:
            password = getpass.getpass("ngenCerf password: ")

    # Attempt login
    payload = {"email": email, "password": password}
    response = requests.post(LOGIN_ENDPOINT, json=payload)

    # Handle failed login attempts
    if response.status_code != 200:
        if response.status_code == 401:
            print("Login failed — incorrect email or password.")
        else:
            check_http_error(response.status_code, response.text)
            print(f"Login failed with HTTP {response.status_code}. Please try again.")

        # Clear stored password for retry
        _clear_saved_password()
        os.environ.pop("NGEN_PASSWORD", None)

        if not _retry:
            print("[DEBUG] Saved password failed. Prompting for new credentials...")
            return perform_full_login(_retry=True)
        else:
            print("[DEBUG] Second login attempt failed. Aborting.")
            return False

    # Success case
    response_json = response.json()
    access_token = response_json.get("access")
    refresh_token = response_json.get("refresh")

    if access_token:
        os.environ["ACCESS_TOKEN"] = access_token
        os.environ["NGEN_EMAIL"] = email
        os.environ["NGEN_PASSWORD"] = password
        save_credentials_to_env_file(email, password)
        save_to_env_file("ACCESS_TOKEN", access_token)
        if refresh_token:
            os.environ["REFRESH_TOKEN"] = refresh_token
            save_to_env_file("REFRESH_TOKEN", refresh_token)
        print(f"{email} login successful.\n")
        return True
    else:
        print("Login succeeded, but access token missing.")
        return False


def _clear_saved_password():
    """Remove only the saved password so user is reprompted."""
    os.environ.pop("NGEN_PASSWORD", None)
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if not line.startswith("NGEN_PASSWORD="):
                    f.write(line)
        print("[DEBUG] Cleared invalid saved password from .ngencerf_env.")
    except Exception as e:
        print(f"[DEBUG] Failed to clear password: {e}")


def _clear_auth_state():
    """Remove tokens and stored password to ensure a clean retry."""
    for key in ("ACCESS_TOKEN", "REFRESH_TOKEN", "NGEN_PASSWORD"):
        os.environ.pop(key, None)
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if not line.startswith(("ACCESS_TOKEN=", "REFRESH_TOKEN=", "NGEN_PASSWORD=")):
                    f.write(line)
        print("[DEBUG] Cleared invalid tokens and password from .ngencerf_env.")
    except Exception as e:
        print(f"[DEBUG] Failed to clean invalid credentials: {e}")


def refresh_access_token() -> bool:
    """
    Attempts to refresh the access token using REFRESH_TOKEN.
    Updates ~/.ngencerf_env if successful.

    Returns:
        True if refresh succeeded, False otherwise.
    """
    load_ngencerf_env()
    refresh_token = os.environ.get("REFRESH_TOKEN")
    if not refresh_token:
        print("[DEBUG] No refresh token available.")
        return False

    payload = {"refresh": refresh_token}
    response = requests.post(REFRESH_ENDPOINT, json=payload)

    if response.status_code != 200:
        print(f"[DEBUG] Refresh failed with status {response.status_code}: {response.text}")
        return False

    response_json = response.json()
    access_token = response_json.get("access")
    if not access_token:
        print("[DEBUG] Refresh response missing access token.")
        return False

    os.environ["ACCESS_TOKEN"] = access_token
    save_to_env_file("ACCESS_TOKEN", access_token)
    print("Access token refreshed.\n")
    return True


def ngen_register(optional_email: str = None):
    """
    Registers a new user for the NGEN API. Prompts for password input and confirmation.
    """
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

    response = requests.post(REGISTER_ENDPOINT, json=payload)
    if check_http_error(response.status_code, response.text):
        print(f"User '{email}' registered successfully.")
