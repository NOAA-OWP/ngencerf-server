"""
Active Directory / LDAP service layer.

This module contains the low-level logic for communicating with Active
Directory using ldap3.

Responsibilities:
    - connect to the configured LDAP server
    - bind with the service account
    - locate users by email / userPrincipalName
    - validate user credentials
    - retrieve user attributes such as GUID, name, and groups
    - normalize LDAP data into ActiveDirectoryUser objects

This module is intentionally independent of Django authentication flow.
It should not create Django users, manage sessions, issue JWT tokens,
or apply application authorization rules beyond basic group checks.

Primary caller:
    calibration.auth.active_directory_backend
"""

import logging
import uuid
from dataclasses import dataclass
from typing import cast

from django.conf import settings
from ldap3 import Server, Connection, SUBTREE, ALL
from ldap3.core.exceptions import LDAPBindError, LDAPException

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActiveDirectoryUser:
    """
    Normalized Active Directory user data returned after successful authentication.
    """
    ad_guid: uuid.UUID
    email: str
    first_name: str
    last_name: str
    groups: list[str]
    is_admin: bool


class ActiveDirectoryAuthenticationError(Exception):
    """Raised when LDAP authentication fails or returned data is invalid."""
    pass


class ActiveDirectoryServiceBindError(Exception):
    """
    Raised when the LDAP service account cannot bind to Active Directory.
    """
    pass


class ActiveDirectoryAuthorizationError(Exception):
    """Raised when the user authenticated successfully but lacks access."""

    def __init__(
            self,
            message: str,
            *,
            required_group: str | None = None,
            user_groups: list[str] | None = None,
            system_name: str | None = None,
    ):
        super().__init__(message)
        self.required_group = required_group
        self.user_groups = user_groups or []
        self.system_name = system_name


class ActiveDirectoryUserNotFoundError(Exception):
    """Raised when no AD user matches the supplied email."""
    pass


def authenticate_active_directory_user(email: str, password: str) -> ActiveDirectoryUser:
    """
    Authenticate a user against Active Directory and return normalized user data.

    Flow:
        1. Bind with service account.
        2. Search for the user by email / UPN.
        3. Bind as the user to verify credentials.
        4. Verify required group membership.
        5. Return normalized AD user details.
    """

    email = email.strip().lower()

    if not email:
        raise ActiveDirectoryAuthenticationError("Email is required")

    if not password:
        raise ActiveDirectoryAuthenticationError("Password is required")

    with _get_service_connection() as service_conn:
        user_entry = _find_user_by_email(service_conn, email)

    user_dn = user_entry.entry_dn
    ad_user = _build_ad_user(user_entry)

    _authenticate_user_dn(user_dn, password)

    # TODO:
    # Current logic checks only direct group names returned from memberOf.
    # This is sufficient for many environments, but nested AD groups may not
    # appear as inherited parent memberships.
    #
    # Future improvement:
    # Replace this with explicit recursive / nested membership checks using
    # Active Directory's LDAP_MATCHING_RULE_IN_CHAIN so parent group
    # membership is resolved accurately.
    if (
            settings.LDAP_REQUIRED_GROUP_USERS not in ad_user.groups
            and settings.LDAP_ADMIN_GROUP not in ad_user.groups
    ):
        raise ActiveDirectoryAuthorizationError(
            f"User is not a member of required group {settings.LDAP_REQUIRED_GROUP_USERS}",
            required_group=settings.LDAP_REQUIRED_GROUP_USERS,
            user_groups=ad_user.groups,
            system_name=settings.LDAP_SYSTEM_NAME
        )

    return ad_user


def _get_server() -> Server:
    """
    Build the ldap3 Server object using configured host and timeout settings.
    """
    return Server(
        settings.LDAP_SERVER_URI,
        get_info=ALL,
        connect_timeout=settings.LDAP_TIMEOUT,
    )


def _get_service_connection() -> Connection:
    """
    Create and bind an LDAP connection using the service account.

    This account is used for searching users and reading attributes.
    """
    server = _get_server()

    try:
        return Connection(
            server,
            user=settings.LDAP_BIND_DN,
            password=settings.LDAP_BIND_PASSWORD,
            auto_bind=True,
            receive_timeout=settings.LDAP_TIMEOUT,
        )

    except LDAPBindError as exc:
        logger.exception(
            "LDAP service bind failed: invalid bind credentials or bind DN. "
            "bind_dn=%s server=%s",
            settings.LDAP_BIND_DN,
            settings.LDAP_SERVER_URI,
        )

        raise ActiveDirectoryServiceBindError(
            "Active Directory service account bind failed"
        ) from exc

    except LDAPException as exc:
        logger.exception(
            "LDAP service connection failed: bind_dn=%s server=%s",
            settings.LDAP_BIND_DN,
            settings.LDAP_SERVER_URI,
        )

        raise ActiveDirectoryServiceBindError(
            "Active Directory service connection failed"
        ) from exc


def _authenticate_user_dn(user_dn: str, password: str) -> None:
    """
    Attempt an LDAP bind using the user's DN and submitted password.

    A successful bind confirms valid credentials.
    """
    server = _get_server()

    try:
        with Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=True,
                receive_timeout=settings.LDAP_TIMEOUT,
        ):
            return
    except Exception as exc:
        logger.warning("Active Directory authentication failed for user DN %s", user_dn)
        raise ActiveDirectoryAuthenticationError("Invalid Active Directory credentials") from exc


def _find_user_by_email(conn: Connection, email: str):
    """
    Search Active Directory for exactly one user matching the supplied email.

    Searches both:
        - mail
        - userPrincipalName
    """
    escaped_email = _escape_ldap_filter_value(email)

    search_filter = (
        "(&"
        "(objectClass=user)"
        "(objectCategory=person)"
        f"(|(mail={escaped_email})(userPrincipalName={escaped_email}))"
        ")"
    )

    conn.search(
        search_base=settings.LDAP_USER_SEARCH_BASE_DN,
        search_filter=search_filter,
        search_scope=SUBTREE,
        attributes=[
            "objectGUID",
            "mail",
            "userPrincipalName",
            "givenName",
            "sn",
            "memberOf",
        ],
    )

    if not conn.entries:
        raise ActiveDirectoryUserNotFoundError(f"No Active Directory user found for {email}")

    if len(conn.entries) > 1:
        raise ActiveDirectoryAuthenticationError(
            f"Multiple Active Directory users found for {email}"
        )

    return conn.entries[0]


def _build_ad_user(user_entry) -> ActiveDirectoryUser:
    """
    Convert an ldap3 user entry into the normalized ActiveDirectoryUser model.
    """
    ad_guid = _get_object_guid(user_entry)

    email = _get_first_attr_value(user_entry, "mail")
    if not email:
        email = _get_first_attr_value(user_entry, "userPrincipalName")

    if not email:
        raise ActiveDirectoryAuthenticationError("Active Directory user has no email address")

    email = cast(str, email)

    first_name = _get_first_attr_value(user_entry, "givenName") or ""
    last_name = _get_first_attr_value(user_entry, "sn") or ""

    groups = _get_group_common_names(user_entry)

    return ActiveDirectoryUser(
        ad_guid=ad_guid,
        email=email.lower(),
        first_name=first_name,
        last_name=last_name,
        groups=groups,
        is_admin=settings.LDAP_ADMIN_GROUP in groups,
    )


def _get_object_guid(user_entry) -> uuid.UUID:
    """
    Convert the AD objectGUID attribute into a Python UUID.
    """
    value = _get_first_attr_value(user_entry, "objectGUID")

    if isinstance(value, uuid.UUID):
        return value

    if isinstance(value, bytes):
        return uuid.UUID(bytes_le=value)

    if isinstance(value, str):
        return uuid.UUID(value)

    raise ActiveDirectoryAuthenticationError("Active Directory user has no valid objectGUID")


def _get_group_common_names(user_entry) -> list[str]:
    """
    Return direct group common names (CN values) from memberOf.
    """
    group_dns = _get_attr_values(user_entry, "memberOf")
    groups: list[str] = []

    for group_dn in group_dns:
        group_name = _extract_cn(str(group_dn))
        if group_name:
            groups.append(group_name)

    return groups


def _get_first_attr_value(user_entry, attr_name: str):
    """
    Return the first attribute value or None if missing.
    """
    values = _get_attr_values(user_entry, attr_name)
    return values[0] if values else None


def _get_attr_values(user_entry, attr_name: str) -> list:
    """
    Return attribute values as a normalized list.
    """
    if attr_name not in user_entry:
        return []

    value = user_entry[attr_name].value

    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


def _extract_cn(dn: str) -> str | None:
    """
    Extract the CN component from a distinguished name.

    Example:
        CN=my-group,OU=Groups,DC=corp,DC=com -> my-group
    """
    for part in dn.split(","):
        part = part.strip()
        if part.lower().startswith("cn="):
            return part[3:]

    return None


def _escape_ldap_filter_value(value: str) -> str:
    """
    Escape LDAP filter special characters for safe search filters.
    """
    return (
        value
        .replace("\\", r"\5c")
        .replace("*", r"\2a")
        .replace("(", r"\28")
        .replace(")", r"\29")
        .replace("\x00", r"\00")
    )
