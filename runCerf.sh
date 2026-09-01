#! /bin/bash

MSWM_REPO="https://github.com/NOAA-OWP/nwm-msw-mgr.git"
DATA_ASSIM_REPO="https://github.com/NOAA-OWP/nwm-data-assimilation.git"
EWTS_REPO="https://github.com/NOAA-OWP/nwm-ewts.git"

# Branches/tags for git repos
MSWM_REF='development'
DATA_ASSIMILATION_REF='development'
NGEN_FORCING_REF='development'
EWTS_REF='development'

#=======================================================================
# Usage / help
#   - Checked before anything else so it works without cerfserver.env,
#     a venv, or AWS credentials being set up.
#=======================================================================
if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    cat <<EOF
Usage: ./runCerf.sh [COMMAND] [OPTIONS]

Starts the ngenCERF Django server (dev runserver or Gunicorn/Uvicorn ASGI),
handling venv setup, dependency installs, gage/static data initialization,
database migrations and Redis cache flushing.

Commands:
  (none)            Start the server normally.
  activate          Activate the project virtualenv in your current shell.
                     Must be sourced: 'source ./runCerf.sh activate'
  manage ARGS...    Run './manage.py ARGS...' (venv is activated first,
                     no other startup steps run). Example:
                       ./runCerf.sh manage migrate

Options (normal start only):
  --load-gages      Force init_gages to run even if gage records already
                     exist and the gage data fingerprint is unchanged.
  auto_reload       Enable the Django dev server's auto-reloader
                     (otherwise runs with --noreload).
  -h, --help        Show this help and exit.

Key environment variables (set in cerfserver.env, cerfServer/.env, or
cerfServer/.env-override):
  PORT                       Required. Port the server listens on.
  CERF_VENV                  Docker | <path> | unset. Venv location, or
                             'Docker' to skip local venv/AWS-check logic.
  REQUIRED_PYTHON            Required Python version, e.g. 3.11 or python3.11.
  RUN_CERF_FLAG_DIRECTORY    Directory for SHA/gage marker files (default ./).
  FORCE_REINSTALL_VCS        1 to force reinstall of git-based dependencies.
  DJANGO_SUPERUSER_EMAIL/
  DJANGO_SUPERUSER_PASSWORD  Bootstrap a superuser if not already present.
  CERF_ASGI / CERF_PRODUCTION
                             Set to 1 to launch Gunicorn+Uvicorn instead of
                             the Django dev server.
  GUNICORN_WORKERS, GUNICORN_TIMEOUT, GUNICORN_BIND,
  GUNICORN_MAX_REQUESTS, GUNICORN_MAX_REQUESTS_JITTER,
  GUNICORN_GRACEFUL_TIMEOUT
                             Gunicorn tuning, used only when the ASGI
                             server is launched.
EOF
    exit 0
fi

#=======================================================================
# Script must be sourced for 'activate' mode
#=======================================================================
if [[ "$1" == "activate" ]] && [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Error: This script must be sourced, not executed, when using the 'activate' command."
    echo "Use 'source ./runCerf.sh activate' to activate the virtual environment."
    exit 1
fi

#=======================================================================
# Resolve script directory
#   Ordering prerequisite:
#     - SCRIPT_DIR must be defined before any code/function that references
#       files relative to the repo (manage.py, requirements.txt, templates, etc.)
#=======================================================================
SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

# Use the same directory variable for cerfServer (needed by ensure_virtualenv)
cerfServer="$SCRIPT_DIR"

#=======================================================================
# Early env bootstrap
#   - Needed by ensure_virtualenv and Docker detection
#   - Kept near the top so the 'activate' fast path can exit early
#=======================================================================
set -a
# shellcheck source=./cerfserver.env
source "$SCRIPT_DIR/cerfserver.env"
set +a

if [ -z "${PORT}" ]; then
    echo "ERROR: PORT is not set in cerfserver.env"
    exit 1
fi

IN_DOCKER=false
if [ "${CERF_VENV}" = "Docker" ]; then
    IN_DOCKER=true
fi

if [ "$IN_DOCKER" = false ]; then
    #=======================================================================
    # Python interpreter validation
    #=======================================================================
    # Strip 'python' prefix.  Value can be python3.11 or 3.11
    REQUIRED_PYTHON_VERSION="${REQUIRED_PYTHON#python}"

    PYTHON_BIN="${PYTHON_BIN:-python${REQUIRED_PYTHON_VERSION}}"

    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "ERROR: Required Python interpreter not found."
        echo
        echo "This project requires Python ${REQUIRED_PYTHON_VERSION}."
        echo "The expected executable is:"
        echo "    $PYTHON_BIN"
        echo
        echo "Please install Python ${REQUIRED_PYTHON_VERSION} and rerun this script."
        exit 1
    fi

    PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

    if [ "$PYTHON_VERSION" != "$REQUIRED_PYTHON_VERSION" ]; then
        echo "ERROR: $PYTHON_BIN is Python $PYTHON_VERSION, but Python $REQUIRED_PYTHON_VERSION is required."
        exit 1
    fi

    echo "Using Python $PYTHON_VERSION ($PYTHON_BIN)"
fi


#=======================================================================
# Function: ensure_virtualenv
#   - If CERF_VENV is empty or "Docker", do nothing
#   - Derives the actual venv path from CERF_VENV + REQUIRED_PYTHON
#       Example:
#         CERF_VENV=./.venv-cerf
#         REQUIRED_PYTHON=3.11
#         => ./.venv-cerf_python3.11
#   - If the venv does not exist, create it with the required Python version
#   - If the venv exists, verify that it uses the required Python version
#   - If the existing venv is invalid or has the wrong Python version, fail
#   - Activate the venv so "python" and "pip" refer to the venv
#=======================================================================
ensure_virtualenv() {
    # Requires: CERF_VENV loaded, IN_DOCKER set, cerfServer set,
    #           REQUIRED_PYTHON_VERSION set, PYTHON_BIN validated

    if [ -z "${CERF_VENV}" ] || [ "$IN_DOCKER" = true ]; then
        return 0
    fi

    VENV_PATH="${cerfServer}/${CERF_VENV}_python${REQUIRED_PYTHON_VERSION}"
    VENV_PYTHON="$VENV_PATH/bin/python"

    if [ -d "$VENV_PATH" ]; then
        if [ ! -x "$VENV_PYTHON" ]; then
            echo "ERROR: Existing virtual environment is invalid: $VENV_PATH"
            echo "ERROR: Expected executable not found: $VENV_PYTHON"
            exit 1
        fi

        VENV_VERSION="$("$VENV_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

        if [ "$VENV_VERSION" != "$REQUIRED_PYTHON_VERSION" ]; then
            echo "ERROR: Virtual environment '$VENV_PATH' is inconsistent."
            echo "ERROR: Directory name indicates Python $REQUIRED_PYTHON_VERSION,"
            echo "ERROR: but it actually contains Python $VENV_VERSION."
            echo "ERROR: Delete this virtual environment and rerun the script."
            exit 1
        fi
    else
        echo "Creating virtual environment at $VENV_PATH using $PYTHON_BIN..."
        "$PYTHON_BIN" -m venv "$VENV_PATH"
    fi

    source "$VENV_PATH/bin/activate"
    echo "Activated virtual environment at $VENV_PATH"
    echo "Virtual environment Python: $(python --version)"
}

#=======================================================================
# Special case: if the first argument is "activate", just activate the
# venv and return immediately before any other startup work.
#=======================================================================
if [ "$1" == "activate" ]; then
    ensure_virtualenv
    return 0
fi

#=======================================================================
# Load optional local-only env files (non-Docker only)
#   Prerequisite: optional; missing files are not fatal
#=======================================================================
if [ "$IN_DOCKER" = false ]; then
    echo "Non-Docker environment: checking for local env files"

    ENV_FILE="$SCRIPT_DIR/cerfServer/.env"
    ENV_OVERRIDE_FILE="$SCRIPT_DIR/cerfServer/.env-override"

    if [ -f "$ENV_FILE" ]; then
        echo "Loaded env file: $ENV_FILE"
        # shellcheck source=./cerfServer/.env
        source "$ENV_FILE"
    else
        echo "WARNING: env file not found: $ENV_FILE"
    fi

    if [ -f "$ENV_OVERRIDE_FILE" ]; then
        echo "Loaded env override file: $ENV_OVERRIDE_FILE"
        # shellcheck source=./cerfServer/.env-override
        source "$ENV_OVERRIDE_FILE"
    fi
else
    echo "Docker environment detected: skipping local env files (.env, .env-override)"
fi

#=======================================================================
# Validate and create RUN_CERF_FLAG_DIRECTORY
#   Ordering prerequisite:
#     - Must happen before any code that writes marker files into it:
#         * SHA markers (.mswm.sha, .data_assimilation_engine.sha, .ewts.sha)
#         * gage flags/fingerprints (.load_gages, .gages_fingerprint)
#
# Docker requirement:
#   - When running in Docker, this directory must already exist on the
#     persistent host or mounted volume before the container starts.
#   - Creating it only inside the container does not make it persistent,
#     so marker files would be lost when the container is replaced.
#
# Ordering prerequisite:
#   - Must happen before any code that writes marker files into it:
#       * SHA markers (.mswm.sha, .data_assimilation_engine.sha, .ewts.sha)
#       * gage flags/fingerprints (.load_gages, .gages_fingerprint)
#=======================================================================
if [ -z "${RUN_CERF_FLAG_DIRECTORY}" ]; then
    echo "WARNING: RUN_CERF_FLAG_DIRECTORY is not set in cerfserver.env; defaulting to ./"
    RUN_CERF_FLAG_DIRECTORY="./"
fi

# Remove a trailing slash. For "./", this produces ".".
RUN_CERF_FLAG_DIRECTORY="${RUN_CERF_FLAG_DIRECTORY%/}"

if ! mkdir -p "$RUN_CERF_FLAG_DIRECTORY"; then
    echo "ERROR: Could not create RUN_CERF_FLAG_DIRECTORY:"
    echo "    $RUN_CERF_FLAG_DIRECTORY"
    exit 1
fi

if [ ! -d "$RUN_CERF_FLAG_DIRECTORY" ]; then
    echo "ERROR: RUN_CERF_FLAG_DIRECTORY is not a directory:"
    echo "    $RUN_CERF_FLAG_DIRECTORY"
    exit 1
fi

echo "Using runCerf flag directory: $RUN_CERF_FLAG_DIRECTORY"

#=======================================================================
# Bootstrap logging (MUST happen before any run_manage_command calls)
#   Ordering prerequisite:
#     - Must happen before any run_manage_command calls, migrations, init_sql, etc.
#     - Defines LOGFILE_DEV and saves FD 3/4 used by run_manage_command.
#=======================================================================
mkdir -p "$cerfServer/logs"
LOGFILE_DEV="$cerfServer/logs/ngencerf.log"
printf "\n------- Server starting at %s --------\n" "$(date)" | tee -a "$LOGFILE_DEV"

# Save original stdout/stderr
exec 3>&1 4>&2
# Redirect everything to the logfile
exec > >(tee -a "$LOGFILE_DEV") 2>&1

#=======================================================================
# Fingerprint globals (RUN_CERF_FLAG_DIRECTORY must already be valid)
#   Ordering prerequisite:
#     - Must be defined before store_gages_fingerprint is ever called.
#=======================================================================
CERF_GAGES_FPRINT="${RUN_CERF_FLAG_DIRECTORY}/.gages_fingerprint"

#=======================================================================
# Function: check_aws_credentials_early
#   - Uses AWS CLI (STS) for a fast sanity check
#   - Skips if running in Docker
#   - Skips if aws CLI is not installed
#   - Fails startup if credentials are invalid/expired
#   - Logs the resolved identity ARN on success
#
#   Prerequisites:
#     - IN_DOCKER has been set
#=======================================================================
check_aws_credentials_early() {
    # Skip in Docker.
    if [ "$IN_DOCKER" = true ]; then
        echo "Skipping AWS credential check (Docker environment)"
        return 0
    fi

    # Skip if AWS CLI not installed
    if ! command -v aws >/dev/null 2>&1; then
        echo "Skipping AWS credential check (aws CLI not found)"
        return 0
    fi

    echo "Checking AWS credentials (early STS sanity check)..."

    if aws sts get-caller-identity \
        --output json \
        --cli-connect-timeout 3 \
        --cli-read-timeout 3 \
        >/dev/null 2>&1
    then
        IDENTITY=$(aws sts get-caller-identity --output text --query 'Arn' 2>/dev/null)
        echo "AWS credentials OK ($IDENTITY)"
        return 0
    else
        echo "ERROR: AWS credentials are missing, expired, or invalid"

        # If this script is being sourced, don't kill the caller's shell.
        if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
            return 2
        fi

        exit 2
    fi
}


#=======================================================================
# Function: run_manage_command
#
# PREREQUISITES (must be true before calling this function):
#   - Logging must already be initialized
#   - LOGFILE_DEV must be defined
#   - FD 3 and 4 must contain the original stdout/stderr:
#         exec 3>&1 4>&2
#   - stdout/stderr must currently be redirected to LOGFILE_DEV
#
# REASON:
#   This function temporarily restores the original stdout/stderr for
#   interactive Django output, then re-applies the logfile redirection.
#   If those file descriptors or variables are missing, output will break.
#=======================================================================
run_manage_command() {
    echo "Running manage.py $*"

    # Temporarily restore original stdout/stderr
    exec 1>&3 2>&4
    python "$SCRIPT_DIR/manage.py" "$@"
    local status=$?   # Capture the Python exit code

    # Re-redirect to the logfile
    exec > >(tee -a "$LOGFILE_DEV") 2>&1

    if [ $status -ne 0 ]; then
        echo "manage.py $* failed with exit code $status"
    fi
    return $status
}


#=======================================================================
# Function: gage_records_exist
#   - Returns 0 if the gage table contains at least one record
#   - Returns 10 if the gage table is empty
#   - Returns 2 if Django could not perform the check
#=======================================================================
gage_records_exist() {
    echo "Checking whether the gage table contains any records..."

    python "$SCRIPT_DIR/manage.py" shell -c '
from calibration.models import Gage
raise SystemExit(0 if Gage.objects.exists() else 10)
' --verbosity 0

    local status=$?

    case $status in
        0)
            echo "The gage table contains records."
            return 0
            ;;
        10)
            echo "The gage table is empty."
            return 10
            ;;
        *)
            echo "ERROR: Could not determine whether the gage table contains records."
            return 2
            ;;
    esac
}

#=======================================================================
# Function: generate_git_info
#
#   - Writes <repo>_git_info.json with commit metadata similar to how the Dockerfile does it
#=======================================================================
generate_git_info() {
    repo_url=$(git config --get remote.origin.url)
    # Extract the repo name (everything after the last slash) and remove any trailing .git
    key=${repo_url##*/}
    key=${key%.git}
    GIT_INFO_PATH=$SCRIPT_DIR/${key}_git_info.json

    echo "Generating ${GIT_INFO_PATH}..."
    jq -n \
        --arg commit_hash "$(git rev-parse HEAD)" \
        --arg branch "$(git rev-parse --abbrev-ref HEAD)" \
        --arg tags "$(git tag --points-at HEAD | tr '\n' ' ')" \
        --arg author "$(git log -1 --pretty=format:'%an')" \
        --arg commit_date "$(date -u -d @"$(git log -1 --pretty=format:'%ct')" +'%Y-%m-%d %H:%M:%S UTC')" \
        --arg message "$(git log -1 --pretty=format:'%s' | tr '\n' ';')" \
        --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
        "{\"${key}\": {commit_hash: \$commit_hash, branch: \$branch, tags: \$tags, author: \$author, commit_date: \$commit_date, message: \$message, build_date: \$build_date}}" \
        > "$GIT_INFO_PATH"
    echo "Generated $GIT_INFO_PATH"
}

#=======================================================================
# Fingerprint logic for init_gages inputs
#   - Computes a stable SHA256 for init_gages.py + files in gage_data/
#   - Stores/compares to decide whether to re-run init_gages
#
#   Prerequisites:
#     - SCRIPT_DIR must be set (paths are relative to it)
#     - CERF_GAGES_FPRINT must be set before store_gages_fingerprint is called
#=======================================================================
compute_gages_fingerprint() {
    set -o pipefail
    local base="$SCRIPT_DIR/calibration/management/commands"
    local files=()

    # Must have init_gages.py
    if [ ! -f "$base/init_gages.py" ]; then
        echo "compute_gages_fingerprint: missing $base/init_gages.py" >&2
        return 1
    fi
    files+=("$base/init_gages.py")

    # Hash everything inside gage_data/ if present (excluding pyc/__pycache__)
    if [ -d "$base/gage_data" ]; then
        while IFS= read -r -d '' f; do
            files+=("$f")
        done < <(find "$base/gage_data" -type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 | sort -z)
    fi

    # Hash each file then hash the list into a single digest
    sha256sum "${files[@]}" | sha256sum | awk '{print $1}'
}

store_gages_fingerprint() {
    # Prerequisite: CERF_GAGES_FPRINT is set to a writable path
    local fp="$1"
    if [ -z "$fp" ]; then
        echo "store_gages_fingerprint: empty fingerprint" >&2
        return 1
    fi
    echo "$fp" > "$CERF_GAGES_FPRINT"
    echo "Saved gage fingerprint: ${fp:0:12}… -> $CERF_GAGES_FPRINT"
}


#=======================================================================
# Helper: run init_gages and store a provided fingerprint (or recompute if empty)
#   Ordering prerequisites:
#     - run_manage_command must be usable (logging bootstrapped + FD 3/4 saved)
#     - CERF_GAGES_FPRINT must be set before calling store_gages_fingerprint
#=======================================================================
run_init_gages_and_store() {
    local fp="$1"

    echo "Running init_gages..."
    run_manage_command init_gages
    local status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        return $status   # propagate the exact failure code upward
    fi

    # If init_gages succeeded, store or recompute fingerprint
    if [ -n "$fp" ]; then
        store_gages_fingerprint "$fp"
    else
        if FP_NOW="$(compute_gages_fingerprint)"; then
            store_gages_fingerprint "$FP_NOW"
        else
            echo "Warning: could not compute fingerprint after init_gages"
        fi
    fi

    return 0
}

#=======================================================================
# Superuser helpers (email is the USERNAME_FIELD)
#=======================================================================
# Function: superuser_exists
#   - Checks if a superuser with the given email already exists.
#   - We have to shell out to Python/Django here because the user model
#     lives in Django (with a custom AUTH_USER_MODEL using email).
#
#   Implementation details:
#     * We run "manage.py shell -c" with a short Python snippet:
#         from django.contrib.auth import get_user_model
#         User = get_user_model()
#         print(User.objects.filter(is_superuser=True, email=...).exists())
#       This prints exactly "True" or "False".
#
#     * Unfortunately, "manage.py shell" also triggers app startup, which
#       prints a bunch of INFO log lines (database info, settings, etc.).
#       Those would otherwise get captured into our Bash variable.
#
#     * To make this robust:
#         --verbosity 0   => suppresses most management command chatter
#         2>/dev/null     => discards stderr noise
#         tail -n 1       => keeps only the *last* line of stdout (our True/False)
#         tr -d "\r"      => strip any stray carriage returns
#
#   Return value:
#     * Echoes a log line with the raw "True"/"False".
#     * Returns 0 (success) if "True", else 1.
#=======================================================================
superuser_exists() {
    local email="$1"
    echo "Checking for superuser [$email]..."
    local out
    out=$(
        DJANGO_EMAIL="$email" \
        python "$SCRIPT_DIR/manage.py" shell -c '
from django.contrib.auth import get_user_model
import os
User = get_user_model()
print(User.objects.filter(is_superuser=True, email=os.environ["DJANGO_EMAIL"]).exists())
' --verbosity 0 2>/dev/null | tail -n 1 | tr -d "\r"
    )
    echo "superuser_exists result: $out"
    [[ "$out" == "True" ]]
}

#=======================================================================
# Function: ensure_superuser
#   - If DJANGO_SUPERUSER_EMAIL/PASSWORD are set and the account is missing, create it.
#   - If vars are missing, log a WARNING and skip creation.
#=======================================================================
ensure_superuser() {
    # Pull from env; set these in cerfserver.env
    local email="${DJANGO_SUPERUSER_EMAIL}"
    local password="${DJANGO_SUPERUSER_PASSWORD}"

    if [ -z "$email" ] || [ -z "$password" ]; then
        echo "WARNING: ensure_superuser: DJANGO_SUPERUSER_EMAIL and/or DJANGO_SUPERUSER_PASSWORD not set."
        echo "WARNING: No superuser will be created automatically. Set these env vars to bootstrap an admin account."
        return 0
    fi

    if superuser_exists "$email"; then
        echo "Superuser [$email] already exists; skipping creation."
        return 0
    fi

    echo "Creating superuser [$email]..."
    DJANGO_SUPERUSER_EMAIL="$email" \
    DJANGO_SUPERUSER_PASSWORD="$password" \
    python "$SCRIPT_DIR/manage.py" createsuperuser --noinput
    local status=$?
    if [ $status -ne 0 ]; then
        echo "createsuperuser failed with exit code $status"
        return $status
    fi
}


#=======================================================================
# Function: run_migrate_with_showmigrations
#   - Always runs 'showmigrations' immediately after 'migrate'.
#   - If 'migrate' fails, still shows migrations, then exits with failure.
#   - If 'migrate' succeeds, execution continues normally.
#=======================================================================
run_migrate_with_showmigrations() {
    echo
    echo --------------------------------------------------------
    run_manage_command migrate
    local status=$?

    echo
    echo --------------------------------------------------------
    run_manage_command showmigrations calibration || true

    if [ $status -ne 0 ]; then
        echo "migrate failed"
        exit $status
    fi
}

validate_git_ref_or_exit() {
    local repo_url="$1"
    local ref="$2"
    local label="$3"  # just for nicer error messages

    # Allow exact commit SHA refs (7-40 hex chars)
    if [[ "$ref" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        return 0
    fi

    # Check for branch or tag on the remote
    if git ls-remote --exit-code --heads "$repo_url" "$ref" >/dev/null 2>&1; then
        return 0
    fi
    if git ls-remote --exit-code --tags "$repo_url" "$ref" >/dev/null 2>&1; then
        return 0
    fi

    echo "ERROR: Invalid git ref for ${label}: '${ref}'"
    echo "ERROR: Not found as branch or tag on: ${repo_url}"
    exit 2
}

#=======================================================================
# Validate git refs early so we fail before any installs or setup work
#=======================================================================
validate_git_ref_or_exit "$MSWM_REPO" "$MSWM_REF" "mswm"
validate_git_ref_or_exit "$DATA_ASSIM_REPO" "$DATA_ASSIMILATION_REF" "data_assimilation_engine"
validate_git_ref_or_exit "$EWTS_REPO" "$EWTS_REF" "ewts"

#=======================================================================
# Special case: if the first argument is “manage”, just run manage.py <args>
#=======================================================================
if [ "$1" == "manage" ]; then
    shift
    ensure_virtualenv  # Activates and creates virtualenv if needed
    run_manage_command "$@"
    exit $?
fi



check_aws_credentials_early
echo
echo --------------------------------------------------------


#=======================================================================
# Parse flags
#   --load-gages
#   auto_reload   (enables Django auto-reloader; disables --noreload)
#=======================================================================
LOAD_GAGE_DATA=false
AUTO_RELOAD=false

for arg in "$@"; do
  case $arg in
    --load-gages)
      LOAD_GAGE_DATA=true
      ;;
    auto_reload)
      AUTO_RELOAD=true
      ;;
  esac
done

#=======================================================================
# Non-Docker environment setup (packages, deps, git info)
#=======================================================================
if [ "$IN_DOCKER" = false ]; then
    if [ -n "${CERF_VENV}" ]; then
        ensure_virtualenv

        echo
        echo --------------------------------------------------------
        echo "Upgrading pip"
        python -m pip install --upgrade pip
        python -m pip --version

        echo
        echo --------------------------------------------------------
        echo "Installing requirements.txt"
        python -m pip install -r "$SCRIPT_DIR/requirements.txt"

        FORCE_REINSTALL_VCS="${FORCE_REINSTALL_VCS:-0}"

        # SHA caching is branch-oriented. It resolves refs/heads/<ref>
        # and skips reinstall when the branch tip SHA has not changed.
        #
        # Tags and commit hashes are still valid pip install refs, but this
        # helper does not cache them. If a *_REF is set to a tag or commit hash,
        # the install falls back to a full reinstall on each startup. That is
        # acceptable because these override refs are mainly for development use.
        resolve_branch_sha() {
            local repo_url="$1"
            local branch="$2"
            local sha
            sha="$(git ls-remote "$repo_url" "refs/heads/${branch}" | awk '{print $1}')"
            if [ -z "$sha" ]; then
                echo "WARNING: Could not resolve SHA for $repo_url branch $branch (will reinstall as fallback)"
                return 1
            fi
            echo "$sha"
        }

        should_reinstall_git_pkg() {
            local pkg_name="$1"
            local desired_sha="$2"
            local sha_marker="$3"

            if [ "$FORCE_REINSTALL_VCS" != "0" ]; then
                echo "FORCE_REINSTALL_VCS=1; will reinstall $pkg_name"
                return 0
            fi

            if ! python -m pip show "$pkg_name" >/dev/null 2>&1; then
                echo "$pkg_name not installed; will install"
                return 0
            fi

            if [ ! -f "$sha_marker" ]; then
                echo "No SHA marker for $pkg_name; will reinstall"
                return 0
            fi

            if ! grep -qx "$desired_sha" "$sha_marker"; then
                echo "$pkg_name SHA changed; will reinstall"
                return 0
            fi

            echo "$pkg_name already at $desired_sha; skipping reinstall"
            return 1
        }

        record_sha_marker() {
            local sha="$1"
            local sha_marker="$2"
            echo "$sha" > "$sha_marker"
        }


        # requirements.txt does not reliably pick up changes in the git-installed repos.
        # With SHA caching, reinstall when the branch tip SHA changes
        # (or FORCE_REINSTALL_VCS=1). Do NOT use --no-deps here, because these
        # packages may add or change dependencies in pyproject.toml.

        echo
        echo --------------------------------------------------------
        echo "Installing mswm from branch '$MSWM_REF'"
        MSWM_SHA_MARKER="${RUN_CERF_FLAG_DIRECTORY}/.mswm.sha"
        if MSWM_SHA="$(resolve_branch_sha "$MSWM_REPO" "$MSWM_REF")"; then
            echo "mswm ${MSWM_REF} -> ${MSWM_SHA}"

            if should_reinstall_git_pkg "mswm" "$MSWM_SHA" "$MSWM_SHA_MARKER"; then
                python -m pip install --force-reinstall --no-cache-dir "git+${MSWM_REPO}@${MSWM_REF}"
                record_sha_marker "$MSWM_SHA" "$MSWM_SHA_MARKER"
            fi
        else
            # Fallback: could not resolve the branch SHA; revert to branch-based install behavior.
            python -m pip install --force-reinstall --no-cache-dir "git+${MSWM_REPO}@${MSWM_REF}"
        fi

        echo
        echo --------------------------------------------------------
        echo "Installing data_assimilation_engine from branch '$DATA_ASSIMILATION_REF'"
        DATA_ASSIM_SHA_MARKER="${RUN_CERF_FLAG_DIRECTORY}/.data_assimilation_engine.sha"
        if DATA_ASSIM_SHA="$(resolve_branch_sha "$DATA_ASSIM_REPO" "$DATA_ASSIMILATION_REF")"; then
            echo "data_assimilation_engine ${DATA_ASSIMILATION_REF} -> ${DATA_ASSIM_SHA}"

            if should_reinstall_git_pkg "data_assimilation_engine" "$DATA_ASSIM_SHA" "$DATA_ASSIM_SHA_MARKER"; then
                python -m pip install --force-reinstall --no-cache-dir "git+${DATA_ASSIM_REPO}@${DATA_ASSIMILATION_REF}"
                record_sha_marker "$DATA_ASSIM_SHA" "$DATA_ASSIM_SHA_MARKER"
            fi
        else
            # Fallback: could not resolve the branch SHA; revert to branch-based install behavior.
            python -m pip install --force-reinstall --no-cache-dir "git+${DATA_ASSIM_REPO}@${DATA_ASSIMILATION_REF}"
        fi

        echo
        echo --------------------------------------------------------
        echo "Installing ewts from branch '$EWTS_REF'"
        EWTS_SHA_MARKER="${RUN_CERF_FLAG_DIRECTORY}/.ewts.sha"
        if EWTS_SHA="$(resolve_branch_sha "$EWTS_REPO" "$EWTS_REF")"; then
            echo "ewts ${EWTS_REF} -> ${EWTS_SHA}"

            if should_reinstall_git_pkg "ewts" "$EWTS_SHA" "$EWTS_SHA_MARKER"; then
                pip install --force-reinstall --no-cache-dir "git+${EWTS_REPO}@${EWTS_REF}#subdirectory=runtime/python/ewts"
                record_sha_marker "$EWTS_SHA" "$EWTS_SHA_MARKER"
            fi
        else
            # Fallback: could not resolve the branch SHA; revert to branch-based install behavior.
            pip install --force-reinstall --no-cache-dir "git+${EWTS_REPO}@${EWTS_REF}#subdirectory=runtime/python/ewts"
        fi

        echo
        echo --------------------------------------------------------
        echo "Running pip check..."
        if ! python -m pip check; then
            echo
            echo "######################################################################"
            echo "##############################  WARNING  #############################"
            echo "######################################################################"
            echo "# pip check found broken requirements. Continuing startup anyway."
            echo "# You may see runtime import errors or unexpected behavior until deps are fixed."
            echo "# To diagnose: run 'python -m pip check' and reinstall the missing/conflicting packages."
            echo "# If you suspect the git-installed packages are in a bad state, uninstall them and rerun this script:"
            echo "#   python -m pip uninstall -y data_assimilation_engine"
            echo "#   python -m pip uninstall -y mswm"
            echo "#   pip uninstall -y ewts"
            echo "######################################################################"
            echo
        fi

        echo
        echo --------------------------------------------------------
        generate_git_info

        echo
        echo --------------------------------------------------------

        #=======================================================================
        # Generate dev logrotate config from template using the user's path to the log directory
        #=======================================================================
        DEV_LOGROTATE_CONF="$SCRIPT_DIR/logrotate-ngencerf.dev.conf"
        if [ -f "$SCRIPT_DIR/logrotate-ngencerf.dev.template" ]; then
            sed "s#__LOG_DIR__#${SCRIPT_DIR}/logs#g" \
                "$SCRIPT_DIR/logrotate-ngencerf.dev.template" > "$DEV_LOGROTATE_CONF"
            echo "Generated dev logrotate config at $DEV_LOGROTATE_CONF"
        else
            echo "WARNING: dev logrotate template not found at $SCRIPT_DIR/logrotate-ngencerf.dev.template"
        fi

        #=======================================================================
        # Install dev logrotate cron job
        #=======================================================================
        echo "Setting up dev logrotate cron job..."
        # Remove any existing ngencerf dev cron jobs
        crontab -l 2>/dev/null | grep -v 'logrotate-ngencerf.dev.conf' > /tmp/mycron

        # Add the new cron job (points at the generated dev config)
        echo "0 10,22 * * * /usr/sbin/logrotate -s ~/.logrotate-status -f $DEV_LOGROTATE_CONF" >> /tmp/mycron

        crontab /tmp/mycron
        status=$?
        rm /tmp/mycron

        if [ $status -ne 0 ]; then
            echo "Warning: could not install cron job (exit $status)"
        fi

        echo "Current crontab:"
        crontab -l
    else
        echo "CERF_VENV is not set. Please set the virtual environment variable."
        exit 1
    fi
fi

#=======================================================================
# Run migrations, always ensure superuser, then run init_sql once
#=======================================================================
run_migrate_with_showmigrations

echo
echo --------------------------------------------------------
ensure_superuser

echo
echo --------------------------------------------------------
run_manage_command init_sql
status=$?

if [ $status -ne 0 ]; then
    echo "init_sql failed with exit code $status"
    exit $status
fi

#=======================================================================
# Init data handling
#   - Empty gage table => unconditional init_gages
#   - '--load-gages' or missing marker => unconditional init_gages
#   - Otherwise compare the stored and current fingerprints
#=======================================================================
GAGE_DATA_FLAG_FILE="${RUN_CERF_FLAG_DIRECTORY}/.load_gages"

echo
echo --------------------------------------------------------

# Load gage data if the table is empty, the flag is provided, or the
# load-gages marker does not exist. Also reload if the inputs changed.
gage_records_exist
GAGE_RECORDS_STATUS=$?

if [ $GAGE_RECORDS_STATUS -eq 2 ]; then
    echo "ERROR: Could not check the gage table."
    exit 1
fi

if [ $GAGE_RECORDS_STATUS -eq 10 ]; then
    echo "Running init_gages because the gage table is empty."

    run_init_gages_and_store ""
    status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        exit $status
    fi

    touch "$GAGE_DATA_FLAG_FILE"

elif [ "$LOAD_GAGE_DATA" = true ]; then
    echo "Running init_gages because --load-gages was specified."

    run_init_gages_and_store ""
    status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        exit $status
    fi

    touch "$GAGE_DATA_FLAG_FILE"

elif [ ! -f "$GAGE_DATA_FLAG_FILE" ]; then
    echo "Running init_gages because the load-gages marker does not exist."

    # Unconditional run in this branch
    run_init_gages_and_store ""
    status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        exit $status
    fi

    touch "$GAGE_DATA_FLAG_FILE"

else
    # Auto-run init_gages if inputs changed; if hashing fails, run to be safe.
    if FP_NOW="$(compute_gages_fingerprint)"; then
        if [ ! -f "$CERF_GAGES_FPRINT" ]; then
            echo "Running init_gages because no stored gage fingerprint exists."

            run_init_gages_and_store "$FP_NOW"
            status=$?

            if [ $status -ne 0 ]; then
                echo "init_gages failed with exit code $status"
                exit $status
            fi
        else
            read -r FP_OLD < "$CERF_GAGES_FPRINT" || FP_OLD=""

            if [ "$FP_NOW" != "$FP_OLD" ]; then
                echo "Running init_gages because the gage fingerprint changed."

                run_init_gages_and_store "$FP_NOW"
                status=$?

                if [ $status -ne 0 ]; then
                    echo "init_gages failed with exit code $status"
                    exit $status
                fi
            else
                echo "Gage fingerprint matches; skipping init_gages."
            fi
        fi
    else
        echo "WARNING: Could not compute the gage fingerprint."
        echo "Running init_gages to be safe."

        run_init_gages_and_store ""
        status=$?

        if [ $status -ne 0 ]; then
            echo "init_gages failed with exit code $status"
            exit $status
        fi
    fi
fi

echo
echo --------------------------------------------------------
#=======================================================================
# Ensure bmi_forcing_templates in ngen-static-files
#   - Docker: copy from image-staged /ngencerf/prebuilt into bind-mounted dir
#   - Non-Docker: clone from Git into /ngencerf/data/ngen-static-files
#=======================================================================
STATIC_DIR="/ngencerf/data/ngen-static-files"
TARGET_DIR="${STATIC_DIR}/bmi_forcing_templates"

# Create static base and ensure a clean target location (shared logic)
mkdir -p "$STATIC_DIR"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

if [ "$IN_DOCKER" = true ]; then
    echo "Running in Docker: replacing bmi_forcing_templates from prebuilt data"

    PREBUILT_DIR="/ngencerf/prebuilt/bmi_forcing_templates"

    # Verify Dockerfile populated this directory
    if [ ! -d "$PREBUILT_DIR" ]; then
        echo "ERROR: Prebuilt bmi_forcing_templates not found at $PREBUILT_DIR"
        echo "Dockerfile must populate this directory during build."
        exit 1
    fi

    echo "Copying from $PREBUILT_DIR -> $TARGET_DIR"
    # Copy contents only
    cp -a "$PREBUILT_DIR"/. "$TARGET_DIR"/

else
    NGEN_FORCING_URL="https://github.com/NOAA-OWP/ngen-forcing.git"

    echo "Not running in Docker: cloning bmi_forcing_templates from ${NGEN_FORCING_URL}, branch: ${NGEN_FORCING_REF}"

    cd "$STATIC_DIR" || {
    echo "ERROR: could not cd to $STATIC_DIR"
    exit 1
    }

    git clone --depth 1 --filter=blob:none --sparse \
        -b "${NGEN_FORCING_REF}" \
        "$NGEN_FORCING_URL" tmp-ngen-forcing

    cd tmp-ngen-forcing || {
    echo "ERROR: could not cd to tmp-ngen-forcing"
    exit 1
    }
    git sparse-checkout set NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates

    # Move *contents* of config_templates into TARGET_DIR
    cp -a NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates/. \
        "$TARGET_DIR"/

    cd "$STATIC_DIR" || {
    echo "ERROR: could not cd to $STATIC_DIR"
    exit 1
    }
    rm -rf tmp-ngen-forcing

    echo "bmi_forcing_templates updated successfully in $TARGET_DIR (non-Docker)."
fi

#=======================================================================
# Flush Redis cache at startup (all environments)
#   - Redis is cache-only; safe to clear on every server start
#   - Uses Django's configured cache (REDIS_URL + TLS) via the clear_cache
#     management command, so it works on AWS ElastiCache and local
#     docker-compose alike.
#=======================================================================
echo
echo --------------------------------------------------------
echo "Flushing Redis cache..."
run_manage_command clear_cache || echo "WARNING: Redis cache clear failed"

#=======================================================================
# Pre-start hook and start server
#=======================================================================
echo
echo --------------------------------------------------------
run_manage_command pre_start
status=$?

if [ $status -ne 0 ]; then
    echo "pre_start failed with exit code $status"
    exit $status
fi

echo
echo --------------------------------------------------------
echo "Starting server"

ASGI_FLAG="${CERF_ASGI:-}" # explicit override
PROD_FLAG="${CERF_PRODUCTION:-}" # general prod indicator

# Restore original stdout/stderr before starting the server (no /dev/tty dependency)
exec 1>&3 2>&4

# use ASGI server if ASGI_FLAG or PROD_FLAG are set
if [ "$ASGI_FLAG" = "1" ] || [ "$PROD_FLAG" = "1" ]; then
    echo "Launching Gunicorn (Uvicorn workers) ASGI server"
    # if GUNICORN_WORKERS is not set, calculate default value for WORKERS
    # Gunicorn recommends (2 x num of cores) + 1 as a reasonable default
    # but we cap it at 8 workers to avoid excessive memory use on small servers
    # and set a minimum of 2 workers to handle multiple requests
    WORKERS=${GUNICORN_WORKERS:-$(
        cpu=$(nproc)
        workers=$((cpu * 2 + 1))
        if [ "$workers" -lt 2 ]; then
            workers=2
        elif [ "$workers" -gt 8 ]; then
            workers=8
        fi
        echo "$workers"
    )}

    TIMEOUT=${GUNICORN_TIMEOUT:-120}
    BIND_ADDR="${GUNICORN_BIND:-0.0.0.0:${PORT}}"
    # --graceful-timeout extra time to finish in-flight requests on restart
    exec gunicorn cerfServer.asgi:application \
            --name ngencerf \
            --workers "${WORKERS}" \
            --worker-class uvicorn.workers.UvicornWorker \
            --max-requests "${GUNICORN_MAX_REQUESTS:-300}" \
            --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-100}" \
            --preload \
            --bind "${BIND_ADDR}" \
            --timeout "${TIMEOUT}" \
            --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
            --config "$(dirname "$0")/gunicorn_conf.py"
else
    echo "Launching Django development server (runserver)"

    if [ "$AUTO_RELOAD" = true ]; then
        echo "Auto-reload ENABLED"
        python "$cerfServer"/manage.py runserver "0.0.0.0:${PORT}"
    else
        echo "Auto-reload DISABLED (--noreload)"
        python "$cerfServer"/manage.py runserver "0.0.0.0:${PORT}" --noreload
    fi
fi

if [ -n "${CERF_VENV}" ] && [ "$IN_DOCKER" = false ]; then
    deactivate
fi
