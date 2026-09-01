"""
Django settings for cerfServer project.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.0/ref/settings/
"""
import json
import os
from datetime import timedelta, datetime, timezone
from enum import StrEnum, auto
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

from datetimerange import DateTimeRange
from dotenv import load_dotenv

from calibration.enums_vanilla import JobExecutionMode
from calibration.util.aorc_date_range import get_aorc_conus_bmi_date_range

DJANGO_START_TIME = datetime.now(tz=timezone.utc)

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

FILE_PATH = os.path.abspath(str(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(FILE_PATH))
THIS_DIR = os.path.dirname(FILE_PATH)

dotenv_path = os.path.join(THIS_DIR, '.env')
print(f'Loading values from {dotenv_path}')
load_dotenv(dotenv_path)

version_path = os.path.join(BASE_DIR, 'version.env')
print(f'Loading values from {version_path}')
load_dotenv(version_path)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() == 'true'

NGENCERF_VERSION = os.getenv("NGENCERF_VERSION", "<unknown>")
NGENCERF_DATE = os.getenv("NGENCERF_DATE", "<unknown>")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "<unknown>")
NGENCERF_COPYRIGHT = f"© 2024-{datetime.now().year}, RTX"

# used to find ngencerf-ui Docker image
NGENCERF_UI_TAG = os.getenv("NGENCERF_UI_TAG", "latest")

# Base URL of the running ngencerf-ui service.
# Local development normally serves the UI on port 3000.
NGENCERF_UI_URL = os.getenv(
    "NGENCERF_UI_URL",
    "http://localhost:3000",
).strip().rstrip("/")

# URL of the build-time Git-information file published by ngencerf-ui.
# This may be overridden independently if the file is served elsewhere.
NGENCERF_UI_GIT_INFO_URL = os.getenv(
    "NGENCERF_UI_GIT_INFO_URL",
    f"{NGENCERF_UI_URL}/ngencerf-ui_git_info.json",
).strip()

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("CERF_SERVER_SECRET_KEY", "not-so-secret-key")

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django_dbconn_retry',
    'django.contrib.staticfiles',
    'drf_spectacular',
    'calibration.apps.CalibrationConfig',
    "rest_framework",
    "rest_framework.authtoken",
    "djoser",
    "rest_framework_simplejwt",
    'corsheaders',
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
]

# Points to which token model should be used for authentication. In case if only stateless
# tokens (e.g. JWT) are used in project it should be set to None.
TOKEN_MODEL = None

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'NgenCerf',
    'DESCRIPTION': 'Backend server for ngenCerf',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "calibration.util.middleware.TimingMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "calibration.util.middleware.ApiRequestDiagnosticsMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_currentuser.middleware.ThreadLocalUserMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
]

# Comma separated list in the env
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        'ALLOWED_HOSTS',
        '.localhost,127.0.0.1'
    ).split(',')
    if host.strip()
]
# On ECS Fargate, the ALB health check reaches this task by its own private
# IP, so that IP arrives in the Host header. Add it to ALLOWED_HOSTS so the
# health check (and any direct in-VPC call) passes host validation. AWS
# injects ECS_CONTAINER_METADATA_URI_V4 into every Fargate container; the
# container metadata document carries this task's private IP.
_ecs_metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")
if _ecs_metadata_uri:
    try:
        with urlopen(_ecs_metadata_uri, timeout=1) as _resp:
            _meta = json.load(_resp)

        for _network in _meta.get("Networks", []):
            for _ip in _network.get("IPv4Addresses", []):
                if _ip and _ip not in ALLOWED_HOSTS:
                    ALLOWED_HOSTS.append(_ip)

    except Exception as exc:
        # Never block startup if metadata is unavailable (e.g. local dev).
        print(f"Unable to read ECS container metadata for ALLOWED_HOSTS: {exc}")

# Comma separated list in the env
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000",
    ).split(",")
    if origin.strip()
]

# Comma separated list in the env. Required when the app is reached through an
# HTTPS front end: Django rejects state-changing requests (admin, session
# POSTs) unless their Origin, scheme included, is listed here, e.g.
# "https://ngencerf-ea.nextgenwaterprediction.com".
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "",
    ).split(",")
    if origin.strip()
]

TRUST_X_FORWARDED_PROTO = os.getenv("TRUST_X_FORWARDED_PROTO", "false").lower() == "true"
if TRUST_X_FORWARDED_PROTO:
    # TLS terminates at a load balancer in front of us: the user speaks HTTPS
    # to the balancer, the balancer speaks plain HTTP to Django and records
    # the original scheme in the X-Forwarded-Proto header. Trusting that
    # header lets Django treat the request as secure (correct scheme in
    # redirects and absolute URLs). Enable only when deployed behind a proxy
    # that always sets the header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

MFA_ENABLED = os.getenv("MFA_ENABLED", "false").lower() == "true"

ACTIVE_DIRECTORY_ENABLED = os.getenv("ACTIVE_DIRECTORY_ENABLED", "false").lower() == "true"

# Active Directory / LDAP
LDAP_DOMAIN = os.getenv("LDAP_DOMAIN", "nextgenwaterprediction.com").strip()

# Use the AD DNS name, not a specific DC IP, so failover can work.
LDAP_SERVER_URI = os.getenv("LDAP_SERVER_URI", f"ldap://{LDAP_DOMAIN}").strip()

# Base DN derived from nextgenwaterprediction.com
LDAP_USER_SEARCH_BASE_DN = os.getenv(
    "LDAP_USER_SEARCH_BASE_DN",
    "DC=nextgenwaterprediction,DC=com"
).strip()

LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "").strip()
LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PASSWORD", "")

# sssd is using AD auth without SSL shown here, so default to ldap:// / non-SSL.
# Set LDAP_USE_SSL=true and LDAP_SERVER_URI=ldaps://... if LDAPS is configured later.
LDAP_USE_SSL = os.getenv("LDAP_USE_SSL", "false").lower() == "true"

LDAP_TIMEOUT = int(os.getenv("LDAP_TIMEOUT", "10"))

LDAP_SYSTEM_NAME = os.getenv("LDAP_SYSTEM_NAME", "local").strip().lower()
LDAP_REQUIRED_GROUP_USERS = f"ngencerf-{LDAP_SYSTEM_NAME}-users"
LDAP_ADMIN_GROUP = f"ngencerf-{LDAP_SYSTEM_NAME}-admins"

ROOT_URLCONF = 'cerfServer.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = [
    "calibration.auth.active_directory_backend.ActiveDirectoryBackend",
    "calibration.auth.active_directory_backend.LocalUserBackend",
]

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.getenv('REDIS_URL', "redis://127.0.0.1:6379/1"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient"
        }
    }
}

AUTH_USER_MODEL = 'calibration.CustomUser'

DJOSER = {
    "SEND_CONFIRMATION_EMAIL": False,
    "SEND_ACTIVATION_EMAIL": False,
    "SET_PASSWORD_RETYPE": True,
    "UPDATE_LAST_LOGIN": True,
    "PASSWORD_RESET_CONFIRM_URL": "reset-password-confirm/{uid}/{token}",
    "SERIALIZERS": {
        "user_create": "calibration.user_serializers.CustomUserCreateSerializer",
        "user": "calibration.user_serializers.CustomUserSerializer",
        "current_user": "calibration.user_serializers.CustomUserSerializer",
    },
}

# https://django-rest-framework-simplejwt.readthedocs.io/en/latest/settings.html#settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'UPDATE_LAST_LOGIN': True,
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_OBTAIN_SERIALIZER': 'calibration.user_serializers.CustomTokenObtainPairSerializer',
}

WSGI_APPLICATION = 'cerfServer.wsgi.application'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/api/static/'

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "downloads"),
]

STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# -----------------------------
# Enterprise Data
# -----------------------------
HYDROFABRIC_SOURCE = 'nhf'
ENTERPRISE_DATA_MODULE_METADATA_ENDPOINT = 'api/v1/modules/parameter_metadata/'
ENTERPRISE_DATA_OBSERVATION_DATA_INFO_ENDPOINT = 'api/v1/streamflow_observations/{gage_id}/info'
ENTERPRISE_DATA_OBSERVATION_DATA_ENDPOINT = 'api/v1/streamflow_observations/{gage_id}/csv'

ENTERPRISE_DATA_URL = os.getenv('ENTERPRISE_DATA_URL')
ENTERPRISE_DATA_ENV = os.getenv('ENTERPRISE_DATA_ENV')

# Default time range for BMI forcing data
FORCING_AORC_CONUS_BMI_DATE_RANGE = get_aorc_conus_bmi_date_range()
FORCING_NWM_RETROSPECTIVE_CONUS_BMI_DATE_RANGE = DateTimeRange("1979-01-01T00:00:00+0000", "2023-01-31T23:59:59+0000")
FORCING_NWM_RETROSPECTIVE_HAWAII_BMI_DATE_RANGE = DateTimeRange("1994-01-01T00:00:00+0000", "2013-12-31T23:59:59+0000")
FORCING_NWM_RETROSPECTIVE_ALASKA_BMI_DATE_RANGE = DateTimeRange("1981-01-01T00:00:00+0000", "2019-12-31T23:59:59+0000")
FORCING_NWM_RETROSPECTIVE_PUERTO_RICO_BMI_DATE_RANGE = DateTimeRange("2008-01-01T00:00:00+0000", "2023-06-30T23:59:59+0000")

# Location of archive files
NGENCERF_ARCHIVE_S3_PATH = os.getenv('NGENCERF_ARCHIVE_S3_PATH')

# Location of download zip files on S3
NGENCERF_ZIPS_S3_PATH = os.getenv('NGENCERF_ZIPS_S3_PATH')

# AWS Profile to use for r/w buckets (.e.g, for archives and zips)
# Use None for AWS Dev (uses default profile)
NGENCERF_RW_PROFILE = os.getenv('NGENCERF_RW_PROFILE') or None

# Local temp directory for building ZIPs before upload (and for CLI zips)
ZIP_TEMP_DIR = os.path.join('/tmp', 'ngencerf-zips')
os.makedirs(ZIP_TEMP_DIR, exist_ok=True)

# How long a presigned download URL is valid
ZIP_DOWNLOAD_URL_TTL_SECONDS = 300

# How long the ZIP object is kept in S3 (and how long status is cached) before cleanup may delete it
ZIP_RETENTION_SECONDS = 3600

# -----------------------------
# Data / working directories
# -----------------------------
# Must match the repo root used in the docker container.
# It is not necessary for you to have local copies of the ngen and nwm-cal-mgr repos if you are using Docker
# But these directories still need to be set to reflect the directory of the repos in the docker container.
REPO_ROOT = '/ngen-app'
NGEN_REPO_ROOT = os.path.join(REPO_ROOT, 'ngen')

# This must match the shared data mount path expected inside the runtime containers.
# Do not change this location unless all runtime containers and Slurm bindings
# are updated consistently.
#
# You may store the actual data elsewhere on the host filesystem and create
# a symbolic link to /ngencerf/data:
#
# sudo mkdir /ngencerf
# sudo ln -s ~/your/data/dir /ngencerf/data
CONTAINER_DATA_ROOT = '/ngencerf/data'
HOST_DATA_ROOT = os.getenv('HOST_DATA_ROOT', CONTAINER_DATA_ROOT)

# Used only by get_git_info when running in Slurm mode with singularities
SINGULARITY_DIR = '/ngencerf/containers'

# -----------------------------
# Slurm partition / node rules
# -----------------------------
# Format:
#   [[max_catchments, partition], [max_catchments, partition]]
#
# Example:
#   SLURM_NODE_TYPE_RULES='[[500, "c5n-9xlarge"], [-1, "r8a-12xlarge"]]'
#
# - max_catchments is an integer upper bound.
# - partition is the Slurm partition/node type to use.
# - -1 means fallback/default for anything larger.
_SLURM_NODE_TYPE_RULES_STR = os.getenv(
    "SLURM_NODE_TYPE_RULES",
    '[[500, "c5n-9xlarge"], [-1, "r8a-12xlarge"]]',
)


def parse_slurm_node_type_rules(value: str) -> list[tuple[int, str]]:
    """
    Parse SLURM_NODE_TYPE_RULES into ordered catchment-to-partition rules.

    Expected format:

        [[max_catchments, partition], [max_catchments, partition]]

    Example:

        [[500, "c5n-9xlarge"], [-1, "r8a-12xlarge"]]

    The final rule must use -1 as the fallback.

    :param value: JSON-encoded rule string from the environment.
    :return: Ordered list of (max_catchments, partition) tuples.
    :raises RuntimeError: If no rules are configured or the fallback rule is missing.
    :raises ValueError: If the rule string is invalid JSON or contains invalid values.
    """
    try:
        raw_rules = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid SLURM_NODE_TYPE_RULES JSON: {exc}") from exc

    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuntimeError("SLURM_NODE_TYPE_RULES must define at least one rule")

    rules: list[tuple[int, str]] = []

    for index, item in enumerate(raw_rules):
        if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], int)
                or not isinstance(item[1], str)
                or not item[1].strip()
        ):
            raise ValueError(
                f"SLURM_NODE_TYPE_RULES item {index} must be "
                f"[int max_catchments, str partition]; got: {item!r}"
            )

        max_catchments, partition = item
        rules.append((max_catchments, partition.strip()))

    if rules[-1][0] != -1:
        raise RuntimeError("SLURM_NODE_TYPE_RULES must end with a -1 fallback rule")

    return rules


SLURM_NODE_TYPE_RULES = parse_slurm_node_type_rules(_SLURM_NODE_TYPE_RULES_STR)

# Allowed Slurm partitions are derived from the node type rules.
SLURM_PARTITIONS = [
    partition
    for _, partition in SLURM_NODE_TYPE_RULES
]

# -----------------------------
# Slurm REST API (slurmrestd) transport
# -----------------------------
# When JOB_EXECUTION_MODE=SLURM, the server submits/cancels/queries jobs over the
# Slurm REST API (slurmrestd) instead of the sbatch/scancel/squeue CLIs. Used with
# AWS PCS, where the Django task reaches slurmrestd on the controller's private IP.
#
# SLURM_REST_ENDPOINT           base URL of slurmrestd, e.g. http://10.0.0.10:6820
# SLURM_API_VERSION             REST API version (Slurm 25.05 -> v0.0.43)
# SLURM_JWT_SECRET_ARN          Secrets Manager ARN of the PCS-managed JWT signing key
#                               (its SecretString is base64; decoded before signing)
# SLURM_REST_USER/UID/GID       POSIX identity claims AWS PCS requires in the JWT
# SLURM_REST_TOKEN_TTL_SECONDS  lifetime of each signed per-request token
# SLURM_REST_JOB_ENVIRONMENT    environment exported to the batch job (JSON array of
#                               "KEY=VALUE"); slurmrestd requires a non-empty environment
SLURM_REST_ENDPOINT = os.getenv("SLURM_REST_ENDPOINT", "")
SLURM_API_VERSION = os.getenv("SLURM_API_VERSION", "v0.0.43")
SLURM_JWT_SECRET_ARN = os.getenv("SLURM_JWT_SECRET_ARN", "")
SLURM_REST_USER = os.getenv("SLURM_REST_USER", "root")
SLURM_REST_UID = int(os.getenv("SLURM_REST_UID", "0"))
SLURM_REST_GID = int(os.getenv("SLURM_REST_GID", "0"))
SLURM_REST_TOKEN_TTL_SECONDS = int(os.getenv("SLURM_REST_TOKEN_TTL_SECONDS", "600"))
SLURM_REST_JOB_ENVIRONMENT = json.loads(
    os.getenv(
        "SLURM_REST_JOB_ENVIRONMENT",
        '["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root"]',
    )
)

# -----------------------------
# MPI node rules
# -----------------------------
# Format:
#   [[max_catchments, num_nodes], [max_catchments, num_nodes]]
#
# Example:
#   MPI_NODE_RULES='[[15, 1], [50, 2], [250, 4], [500, 6], [1000, 10], [1500, 12], [-1, 18]]'
#
# - max_catchments is an integer upper bound.
# - num_nodes is the number of MPI processes/nodes to use.
# - -1 means fallback/default for anything larger.
_MPI_NODE_RULES_STR = os.getenv(
    "MPI_NODE_RULES",
    "[[15, 1], [50, 2], [250, 4], [500, 6], [1000, 10], [1500, 12], [-1, 18]]",
)


def parse_mpi_node_rules(value: str) -> list[tuple[int, int]]:
    """
    Parse MPI_NODE_RULES into ordered catchment-to-node-count rules.

    Expected format:

        [[max_catchments, num_nodes], [max_catchments, num_nodes]]

    Example:

        [[15, 1], [50, 2], [250, 4], [500, 6], [1000, 10], [1500, 12], [-1, 18]]

    The final rule must use -1 as the fallback.

    :param value: JSON-encoded rule string from the environment.
    :return: Ordered list of (max_catchments, num_nodes) tuples.
    :raises RuntimeError: If no rules are configured or the fallback rule is missing.
    :raises ValueError: If the rule string is invalid JSON or contains invalid values.
    """
    try:
        raw_rules = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid MPI_NODE_RULES JSON: {exc}") from exc

    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuntimeError("MPI_NODE_RULES must define at least one rule")

    rules: list[tuple[int, int]] = []

    for index, item in enumerate(raw_rules):
        if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], int)
                or not isinstance(item[1], int)
        ):
            raise ValueError(
                f"MPI_NODE_RULES item {index} must be "
                f"[int max_catchments, int num_nodes]; got: {item!r}"
            )

        max_catchments, num_nodes = item

        if num_nodes <= 0:
            raise ValueError(
                f"MPI_NODE_RULES item {index} has invalid num_nodes={num_nodes}; "
                "must be greater than 0"
            )

        rules.append((max_catchments, num_nodes))

    if rules[-1][0] != -1:
        raise RuntimeError("MPI_NODE_RULES must end with a -1 fallback rule")

    return rules


MPI_NODE_RULES = parse_mpi_node_rules(_MPI_NODE_RULES_STR)

# -----------------------------
# Execution mode
# -----------------------------
_JOB_EXECUTION_MODE_STR = os.getenv('JOB_EXECUTION_MODE', JobExecutionMode.DOCKER.name)

try:
    # noinspection PyTypeHints
    JOB_EXECUTION_MODE = JobExecutionMode[_JOB_EXECUTION_MODE_STR]
except KeyError:
    # noinspection PyUnresolvedReferences
    raise SystemExit(
        f"Invalid environment value for JOB_EXECUTION_MODE: {_JOB_EXECUTION_MODE_STR}. "
        f"Must be one of {', '.join([e.name for e in JobExecutionMode])}"
    )

if JOB_EXECUTION_MODE == JobExecutionMode.SLURM_MOCK and not DEBUG:
    raise RuntimeError("SLURM_MOCK is only allowed when DJANGO_DEBUG=true")

# ------------------------------------------------------------
# Runtime commands
# ------------------------------------------------------------

# Docker command templates used when JOB_EXECUTION_MODE=DOCKER.
# Use {name} placeholder for the Docker container name.
# --rm ensures containers are auto-removed after exit.

CAL_MGR_DOCKER_CMD = (
    f"docker run --rm --network host --name {{name}} "
    f"-v {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} nwm-cal-mgr"
)

NGEN_FORECAST_DOCKER_CMD = (
    f"docker run --rm --name {{name}} "
    f"-v {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} nwm-fcst-mgr"
)

NWM_EVAL_DOCKER_CMD = (
    f"docker run --rm --name {{name}} "
    f"-v {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} nwm-eval-mgr"
)


DOCKER_RUNTIME_INFO = {
    "calibration": CAL_MGR_DOCKER_CMD,
    "validation": CAL_MGR_DOCKER_CMD,
    "validation_iteration": CAL_MGR_DOCKER_CMD,
    "cold_start": NGEN_FORECAST_DOCKER_CMD,
    "forecast": NGEN_FORECAST_DOCKER_CMD,
    "hindcast": NGEN_FORECAST_DOCKER_CMD,
    "verification": NWM_EVAL_DOCKER_CMD,
}

# Singularity image paths used when JOB_EXECUTION_MODE=SLURM.
NWM_CAL_MGR_SINGULARITY_CONTAINER_PATH = os.getenv(
    "NWM_CAL_MGR_SINGULARITY_CONTAINER_PATH"
)

NWM_FCST_MGR_SINGULARITY_CONTAINER_PATH = os.getenv(
    "NWM_FCST_MGR_SINGULARITY_CONTAINER_PATH"
)

NWM_EVAL_SINGULARITY_CONTAINER_PATH = os.getenv(
    "NWM_EVAL_SINGULARITY_CONTAINER_PATH"
)

CAL_MGR_SINGULARITY_CMD = (
    f"/usr/bin/time -v singularity run "
    f"-B {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} "
    f"{NWM_CAL_MGR_SINGULARITY_CONTAINER_PATH}"
)

NGEN_FORECAST_SINGULARITY_CMD = (
    f"/usr/bin/time -v singularity run "
    f"-B {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} "
    f"{NWM_FCST_MGR_SINGULARITY_CONTAINER_PATH}"
)

NWM_EVAL_SINGULARITY_CMD = (
    f"/usr/bin/time -v singularity run "
    f"-B {HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT} "
    f"{NWM_EVAL_SINGULARITY_CONTAINER_PATH}"
)

SINGULARITY_RUNTIME_INFO = {
    "calibration": CAL_MGR_SINGULARITY_CMD,
    "validation": CAL_MGR_SINGULARITY_CMD,
    "validation_iteration": CAL_MGR_SINGULARITY_CMD,
    "cold_start": NGEN_FORECAST_SINGULARITY_CMD,
    "forecast": NGEN_FORECAST_SINGULARITY_CMD,
    "hindcast": NGEN_FORECAST_SINGULARITY_CMD,
    "verification": NWM_EVAL_SINGULARITY_CMD,
}

# Static and working directories
NGEN_STATIC_DIR = os.path.join(CONTAINER_DATA_ROOT, 'ngen-static-files')
NGEN_CAL_WORK_DIR = os.path.join(CONTAINER_DATA_ROOT, 'ngen-cal-work')
NGEN_VERIFICATION_WORK_DIR = os.path.join(CONTAINER_DATA_ROOT, 'verification_work')

# The NGEN_BMI_FORCING_WORK_DIR directory is owned by ngen-forcing.  It will be responsible for creating it
NGEN_BMI_FORCING_WORK_DIR = os.path.join(CONTAINER_DATA_ROOT, 'bmi_forcing_work')

# Directory where all the output runs are stored
NGEN_CAL_RUN_DIR = os.path.join(NGEN_CAL_WORK_DIR, 'run_calib')


def validate_port(value: str, name: str = "PORT") -> int:
    """
    Validate a port value and return it as an integer.

    Examples of valid values:
        8000
        443
        65535

    Examples of invalid values:
        ""
        abc
        0
        65536
    """
    try:
        port = int(value)
    except ValueError:
        raise SystemExit(
            f"Invalid {name} value: '{value}'"
        )

    if not (1 <= port <= 65535):
        raise SystemExit(
            f"{name} out of range: '{value}'"
        )

    return port


def normalize_base_url(url: str, port: int) -> str:
    """
    Normalize NGENCERF_BASE_URL.

    Rules:
      - Add http:// if the scheme is missing
      - Add PORT if no explicit port is present
      - Preserve an explicitly configured port
      - Strip trailing slash

    Examples:
        localhost                  -> http://localhost:8000
        myserver.com               -> http://myserver.com:8000
        https://myserver.com       -> https://myserver.com:8000
        https://myserver.com:8443  -> https://myserver.com:8443
        http://localhost:9000      -> http://localhost:9000
    """
    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"

    parsed = urlparse(url)

    if not parsed.hostname:
        raise SystemExit(
            f"Invalid NGENCERF_BASE_URL: missing hostname: '{url}'"
        )

    try:
        explicit_port = parsed.port
    except ValueError:
        raise SystemExit(
            f"Invalid NGENCERF_BASE_URL: invalid port in URL: '{url}'"
        )

    if explicit_port is None:
        netloc = f"{parsed.hostname}:{port}"

        # Preserve username/password if ever used
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"

        parsed = parsed._replace(netloc=netloc)

    return str(urlunparse(parsed)).rstrip("/")


def validate_url(url: str, name: str) -> None:
    """
    Validate that a normalized URL contains:
      - an HTTP/HTTPS scheme
      - a hostname

    Examples of valid normalized values:
        http://localhost:8000
        http://myserver.com:8000
        https://myserver.com:8443
        https://myserver.com:8000/api

    Examples of invalid values:
        ""
        http:///api/foo
        ftp://myserver.com
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise SystemExit(
            f"Invalid {name}: unsupported URL scheme: '{url}'. "
            f"Expected http:// or https://"
        )

    if not parsed.hostname:
        raise SystemExit(
            f"Invalid {name}: missing hostname: '{url}'"
        )


# Used for Slurm and cal-mgr callback
PORT = validate_port(os.getenv("PORT", "8000"))

NGENCERF_BASE_URL = normalize_base_url(
    os.getenv("NGENCERF_BASE_URL", "localhost:8000/api"),
    PORT,
)

validate_url(NGENCERF_BASE_URL, "NGENCERF_BASE_URL")

# -----------------------------
# Logging
# -----------------------------
VALID_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}


def get_log_level(env_var_name: str, default: str) -> str:
    value = os.getenv(env_var_name, default).upper().strip()

    if value not in VALID_LOG_LEVELS:
        raise SystemExit(
            f"Invalid log level for {env_var_name}: {value}. "
            f"Must be one of {', '.join(sorted(VALID_LOG_LEVELS))}"
        )

    return value


ROOT_LOG_LEVEL = get_log_level('NGENCERF_ROOT_LOG_LEVEL', 'INFO')
DEFAULT_LOG_LEVEL = get_log_level('NGENCERF_LOG_LEVEL', 'DEBUG')
DJANGO_LOG_LEVEL = get_log_level('NGENCERF_DJANGO_LOG_LEVEL', 'INFO')
DJANGO_REQUEST_LOG_LEVEL = get_log_level('NGENCERF_DJANGO_REQUEST_LOG_LEVEL', DJANGO_LOG_LEVEL)
DATABASE_LOG_LEVEL = get_log_level('NGENCERF_DATABASE_LOG_LEVEL', 'WARNING')
NGENCERF_LOG_LEVEL = get_log_level('NGENCERF_CALIBRATION_LOG_LEVEL', DEFAULT_LOG_LEVEL)

# Controls whether Django also writes local log files.
# Defaults to the value of DEBUG (typically True in development and
# False in production):
#   - Development: console + log files
#   - Production:  console only (CloudWatch collects container stdout/stderr)
# Override by setting NGENCERF_LOG_TO_FILE=true|false.
LOG_TO_FILE = (
        os.getenv("NGENCERF_LOG_TO_FILE", str(DEBUG)).lower() == "true"
)

APP_HANDLERS = ["console"]
DB_HANDLERS = ["db_console"]
NGEN_LOGGING_DIR = os.path.join(BASE_DIR, "logs")

if LOG_TO_FILE:
    print(f"File logging enabled: {NGEN_LOGGING_DIR}")
    os.makedirs(NGEN_LOGGING_DIR, exist_ok=True)

    APP_HANDLERS.append("file_dev")
    DB_HANDLERS.append("file_db")

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,

    'root': {
        'handlers': APP_HANDLERS,
        'level': ROOT_LOG_LEVEL,
    },

    'filters': {
        'suppress_successful_health_check': {
            '()': 'calibration.util.logging_filters.SuppressSuccessfulHealthCheckFilter',
        },
    },

    'formatters': {
        'dev_format': {
            'format': '{asctime}.{msecs:03.0f} {module:15s} {levelname:8s} {funcName} {message}',
            'datefmt': '%Y-%m-%dT%H:%M:%S',
            'style': '{',
        },
        'simple': {
            'format': '{asctime}.{msecs:03.0f} {module:15s} {levelname:8s} {funcName} {message}',
            'datefmt': '%Y-%m-%dT%H:%M:%S',
            'style': '{',
        },
    },

    'handlers': {
        'console': {
            'level': DEFAULT_LOG_LEVEL,
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            'filters': ['suppress_successful_health_check'],
        },
        'db_console': {
            'level': DATABASE_LOG_LEVEL,
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        # Only define file handlers when file logging is enabled.
        # Production logs are written to stdout/stderr and collected by CloudWatch.
        **({
               'file_dev': {
                   'level': DEFAULT_LOG_LEVEL,
                   'class': 'logging.FileHandler',
                   'filename': os.path.join(NGEN_LOGGING_DIR, 'ngencerf.log'),
                   'formatter': 'dev_format',
                   'encoding': 'utf-8',
                   'filters': ['suppress_successful_health_check'],
               },
               'file_db': {
                   'level': DATABASE_LOG_LEVEL,
                   'class': 'logging.FileHandler',
                   'filename': os.path.join(NGEN_LOGGING_DIR, 'ngencerf_db.log'),
                   'formatter': 'dev_format',
                   'encoding': 'utf-8',
               },
           } if LOG_TO_FILE else {}),
    },
    # propagate=False prevents duplicate log messages via the root logger.
    'loggers': {
        'django.db.backends': {
            'handlers': DB_HANDLERS,
            'level': DATABASE_LOG_LEVEL,
            'propagate': False
        },
        'django': {
            'handlers': APP_HANDLERS,
            'level': DJANGO_LOG_LEVEL,
            'propagate': False
        },
        'djoser': {
            'handlers': APP_HANDLERS,
            'level': DJANGO_LOG_LEVEL,
            'propagate': False
        },
        'rest_framework_simplejwt': {
            'handlers': APP_HANDLERS,
            'level': DJANGO_LOG_LEVEL,
            'propagate': False
        },
        'django.request': {
            'handlers': APP_HANDLERS,
            'level': DJANGO_REQUEST_LOG_LEVEL,
            'propagate': False
        },
        'django_dbconn_retry': {
            'handlers': APP_HANDLERS,
            'level': DJANGO_LOG_LEVEL,
            'propagate': False
        },
        # Add these loggers for 'requests' and 'urllib3'
        'requests': {
            'handlers': APP_HANDLERS,
            'level': 'INFO',
            'propagate': False
        },
        'urllib3': {
            'handlers': APP_HANDLERS,
            'level': 'INFO',
            'propagate': False
        },
        'calibration': {
            'handlers': APP_HANDLERS,
            'level': NGENCERF_LOG_LEVEL,
            'propagate': False
        },
        'cerfServer': {
            'handlers': APP_HANDLERS,
            'level': NGENCERF_LOG_LEVEL,
            'propagate': False
        }
    }
}

# -----------------------------
# Database
# -----------------------------

DATABASE_OPTIONS = {
    'connect_timeout': int(os.getenv('CERF_SERVER_DATABASE_CONNECT_TIMEOUT', '10')),
    'options': os.getenv(
        'CERF_SERVER_DATABASE_OPTIONS',
        '-c statement_timeout=10000ms'
    ),
    'sslmode': os.getenv('CERF_SERVER_DATABASE_SSLMODE', 'require'),
}

sslrootcert = os.getenv('CERF_SERVER_DATABASE_SSLROOTCERT')

if sslrootcert:
    DATABASE_OPTIONS['sslrootcert'] = sslrootcert

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('CERF_SERVER_DATABASE_NAME', 'postgres'),
        'USER': os.getenv('CERF_SERVER_DATABASE_USER', 'postgres'),
        'PASSWORD': os.getenv('CERF_SERVER_DATABASE_PASSWORD', 'postgres'),
        'HOST': os.getenv('CERF_SERVER_DATABASE_HOST', 'localhost'),
        'PORT': int(os.getenv('CERF_SERVER_DATABASE_PORT', '5432')),
        'CONN_MAX_AGE': int(os.getenv('CERF_SERVER_DATABASE_CONN_MAX_AGE', '60')),
        'OPTIONS': DATABASE_OPTIONS,
    }
}
