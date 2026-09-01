import base64
import json
import os

ENV_FILE = os.path.join(os.path.expanduser("~"), ".ngencerf_env")
DEFAULT_NGENCERF_BASE_URL = "http://localhost:8000/api"
NGENCERF_BASE_URL_KEY = "NGENCERF_BASE_URL"


def load_ngencerf_env() -> None:
    """
    Load variables from ~/.ngencerf_env into the environment if not already present.

    Ignores comments and blank lines.

    :return: None.
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


def save_to_env_file(key: str, value: str) -> None:
    """
    Save or update a key-value pair in ~/.ngencerf_env without duplication.

    If the key already exists, its value is updated. Otherwise, it is appended.

    :param key: Environment variable name to save.
    :param value: Environment variable value to save.
    :return: None.
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


def remove_from_env_file(keys: set[str]) -> None:
    """
    Remove one or more keys from both the current environment and ~/.ngencerf_env.

    :param keys: Environment variable names to remove.
    :return: None.
    """
    for key in keys:
        os.environ.pop(key, None)

    if not os.path.exists(ENV_FILE):
        return

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        for line in lines:
            if not any(line.startswith(f"{key}=") for key in keys):
                f.write(line)


def get_ngencerf_base_url() -> str:
    """
    Get the active ngenCerf server URL.

    :return: Active ngenCerf server URL.
    """
    load_ngencerf_env()
    return os.environ.get(NGENCERF_BASE_URL_KEY, DEFAULT_NGENCERF_BASE_URL).rstrip("/")


SERVERS_FILE = os.path.join(os.path.expanduser("~"), ".ngencerf_servers.json")


def load_saved_server_urls() -> list[str]:
    """
    Load saved ngenCerf server URLs from ~/.ngencerf_servers.json.

    Returns an empty list if the file does not exist, is empty,
    contains invalid JSON, or disappears between the existence check and read.

    :return: List of saved server URLs.
    """
    if not os.path.exists(SERVERS_FILE):
        return []

    try:
        with open(SERVERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    urls = data.get("servers", [])
    if not isinstance(urls, list):
        return []

    return [url for url in urls if isinstance(url, str)]


def save_server_urls(urls: list[str]) -> None:
    """
    Save the list of ngenCerf server URLs to ~/.ngencerf_servers.json.

    :param urls: List of server URLs to persist.
    :return: None.
    """
    with open(SERVERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"servers": urls}, f, indent=2)


# noinspection HttpUrlsUsage
def normalize_server_url(base_url: str) -> str:
    """
    Normalize and validate a server URL.

    Removes surrounding whitespace and trailing slashes, validates
    that the URL starts with either http:// or https://, and ensures
    the URL ends with '/api'.

    :param base_url: Raw server URL.
    :return: Normalized server URL.
    :raises ValueError: If the URL does not start with http:// or https://.
    """
    base_url = base_url.strip().rstrip("/")

    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Server URL must start with http:// or https://")

    if not base_url.endswith("/api"):
        base_url += "/api"

    return base_url


def add_saved_server_url(base_url: str) -> str:
    """
    Add a server URL to the saved URL list if it is not already present.

    :param base_url: Server URL to add.
    :return: The normalized server URL.
    """
    base_url = normalize_server_url(base_url)

    urls = load_saved_server_urls()
    if base_url not in urls:
        urls.append(base_url)
        save_server_urls(urls)

    return base_url


def delete_saved_server_url(base_url: str) -> None:
    """
    Delete a server URL from the saved URL list.

    :param base_url: Server URL to remove.
    :return: None.
    """
    urls = [url for url in load_saved_server_urls() if url != base_url]
    save_server_urls(urls)


def set_ngencerf_base_url(base_url: str) -> None:
    """
    Set the active ngenCerf server URL.

    The URL is normalized, stored in the current environment,
    and persisted to ~/.ngencerf_env.

    Existing access and refresh tokens are cleared because they
    are server-specific.

    :param base_url: Server URL to activate.
    :return: None.
    :raises ValueError: If the URL is invalid.
    """
    base_url = normalize_server_url(base_url)

    os.environ[NGENCERF_BASE_URL_KEY] = base_url
    save_to_env_file(NGENCERF_BASE_URL_KEY, base_url)

    # Active tokens are server-specific.
    remove_from_env_file({"ACCESS_TOKEN", "REFRESH_TOKEN"})


ENCODED_PASSWORD_PREFIX = "b64:"


def encode_env_password(password: str) -> str:
    """
    Encode a password for storage in ~/.ngencerf_env.

    This is obfuscation only, not encryption.

    :param password: Plain-text password.
    :return: Encoded password with prefix.
    """
    encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
    return f"{ENCODED_PASSWORD_PREFIX}{encoded}"


def decode_env_password(value: str) -> str:
    """
    Decode a password loaded from ~/.ngencerf_env.

    Supports both new encoded passwords and legacy plain-text passwords.

    :param value: Stored password value.
    :return: Plain-text password.
    """
    if not value.startswith(ENCODED_PASSWORD_PREFIX):
        return value

    encoded = value[len(ENCODED_PASSWORD_PREFIX):]
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
