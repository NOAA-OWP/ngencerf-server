import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def _require_setting(name: str) -> str:
    value = getattr(settings, name, None)

    if value is None:
        raise ImproperlyConfigured(f"{name} is required when ACTIVE_DIRECTORY_ENABLED=True")

    if isinstance(value, str) and not value.strip():
        raise ImproperlyConfigured(f"{name} is required when ACTIVE_DIRECTORY_ENABLED=True")

    return str(value).strip()


def validate_active_directory_settings() -> None:
    """
    Validate Active Directory settings at startup.

    This should only be called from startup code when validation is actually desired.
    It does not decide whether the current process should run validation.
    """

    if not getattr(settings, "ACTIVE_DIRECTORY_ENABLED", False):
        logger.info("Active Directory authentication is disabled")
        return

    ldap_server_uri = _require_setting("LDAP_SERVER_URI")
    ldap_bind_dn = _require_setting("LDAP_BIND_DN")
    _require_setting("LDAP_BIND_PASSWORD")
    ldap_user_search_base_dn = _require_setting("LDAP_USER_SEARCH_BASE_DN")
    ldap_system_name = _require_setting("LDAP_SYSTEM_NAME")
    ldap_required_group_users = _require_setting("LDAP_REQUIRED_GROUP_USERS")
    ldap_admin_group = _require_setting("LDAP_ADMIN_GROUP")

    ldap_timeout = getattr(settings, "LDAP_TIMEOUT", None)
    if not isinstance(ldap_timeout, int) or ldap_timeout <= 0:
        raise ImproperlyConfigured("LDAP_TIMEOUT must be a positive integer")

    ldap_use_ssl = getattr(settings, "LDAP_USE_SSL", None)
    if not isinstance(ldap_use_ssl, bool):
        raise ImproperlyConfigured("LDAP_USE_SSL must be True or False")

    if ldap_use_ssl and not ldap_server_uri.lower().startswith("ldaps://"):
        logger.warning(
            "LDAP_USE_SSL=True but LDAP_SERVER_URI does not start with ldaps://: %s",
            ldap_server_uri,
        )

    if not ldap_use_ssl and ldap_server_uri.lower().startswith("ldaps://"):
        logger.warning(
            "LDAP_USE_SSL=False but LDAP_SERVER_URI starts with ldaps://: %s",
            ldap_server_uri,
        )

    logger.info("Active Directory authentication is enabled")
    logger.info("LDAP_SERVER_URI: %s", ldap_server_uri)
    logger.info("LDAP_BIND_DN: %s", ldap_bind_dn)
    logger.info("LDAP_USER_SEARCH_BASE_DN: %s", ldap_user_search_base_dn)
    logger.info("LDAP_SYSTEM_NAME: %s", ldap_system_name)
    logger.info("LDAP_REQUIRED_GROUP_USERS: %s", ldap_required_group_users)
    logger.info("LDAP_ADMIN_GROUP: %s", ldap_admin_group)
    logger.info("LDAP_USE_SSL: %s", ldap_use_ssl)
    logger.info("LDAP_TIMEOUT: %s", ldap_timeout)
