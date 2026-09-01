"""
Django authentication backends for Active Directory integration.

This module connects Django's authentication system to Active Directory.

Backends included:

1. ActiveDirectoryBackend
   - Used when ACTIVE_DIRECTORY_ENABLED=True
   - Authenticates users against Active Directory
   - Skips authentication for explicitly local-only users
   - Links existing Django users by ad_guid or email
   - Auto-creates Django users on first successful AD login
   - Synchronizes selected profile fields from AD

2. LocalUserBackend
   - Allows local Django password authentication for:
       - local-only users
       - fallback local superusers
   - Blocks ordinary local-user password login while AD is enabled
   - Blocks password login for AD-managed users

Design goals:
    - Active Directory remains the source of truth for AD-managed credentials
    - Django remains the source of truth for application data
    - AD-managed users authenticate only through Active Directory
    - Local-only users authenticate only with Django passwords
    - One local superuser can remain available for recovery/admin access
"""

import logging
from typing import cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from calibration.auth.active_directory_service import (
    ActiveDirectoryAuthenticationError,
    ActiveDirectoryAuthorizationError,
    ActiveDirectoryUserNotFoundError,
    authenticate_active_directory_user, ActiveDirectoryServiceBindError,
)
from calibration.models import CustomUser

logger = logging.getLogger(__name__)


class ActiveDirectoryBackend(ModelBackend):
    """
    Authenticate normal users against Active Directory when AD authentication is enabled.

    This backend does not authenticate local Django users. Local fallback authentication
    is handled separately by LocalUserBackend.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """
        Authenticate a user using Active Directory credentials.

        Django passes the login identifier as either username or USERNAME_FIELD.
        In this project, the login identifier is email.
        """
        if not settings.ACTIVE_DIRECTORY_ENABLED:
            return None

        email = username or kwargs.get("email")
        if not email or not password:
            return None

        User = get_user_model()
        local_user = User.objects.filter(email__iexact=email).first()
        local_user = cast(CustomUser | None, local_user)

        if local_user and (local_user.is_local_only or local_user.is_superuser):
            logger.info(
                "Skipping AD auth for local Django user: email=%s is_local_only=%s is_superuser=%s",
                email,
                local_user.is_local_only,
                local_user.is_superuser,
            )
            return None

        try:
            ad_user = authenticate_active_directory_user(email=email, password=password)

        except ActiveDirectoryUserNotFoundError:
            logger.warning("AD login failed: user not found for email=%s", email)
            return None

        except ActiveDirectoryAuthorizationError as e:
            logger.warning(
                "AD authorization failed: user lacks required group for system '%s': "
                "email=%s, required_group=%s, ad_groups=%s",
                e.system_name,
                email,
                e.required_group,
                e.user_groups
            )
            raise

        except ActiveDirectoryServiceBindError:
            logger.exception(
                "AD login failed due to LDAP service bind failure: email=%s",
                email,
            )
            raise

        except ActiveDirectoryAuthenticationError:
            logger.warning("AD login failed: invalid credentials email=%s", email)
            return None

        user = self._get_or_link_django_user(ad_user)
        logger.info("AD login succeeded: user_id=%s email=%s", user.id, user.email)
        return user

    @staticmethod
    def _get_or_link_django_user(ad_user) -> CustomUser:
        """
        Find, link, or create the Django user for this AD identity.

        Lookup order:
            1. ad_guid
            2. email, for first-time linking only
            3. create a new Django user if no match exists

        A new user is only created after AD authentication and authorization have
        already succeeded.
        """
        User = get_user_model()

        # Existing AD-linked user
        user = User.objects.filter(ad_guid=ad_user.ad_guid).first()
        user = cast(CustomUser | None, user)

        if user is None:

            # Existing local Django user with same email -> link to AD
            user = User.objects.filter(email__iexact=ad_user.email).first()
            user = cast(CustomUser | None, user)

            if user is None:
                # First login: create Django user automatically
                logger.info(
                    "Creating Django user from AD identity: email=%s, ad_guid=%s, admin=%s",
                    ad_user.email,
                    ad_user.ad_guid,
                    ad_user.is_admin
                )

                user = User.objects.create_user(
                    email=ad_user.email,
                    username=ad_user.email,
                    password=None,
                    ad_guid=ad_user.ad_guid,
                    first_name=ad_user.first_name,
                    last_name=ad_user.last_name,
                    last_ldap_sync=timezone.now(),
                    is_active=True,

                    # LDAP admins become Django staff users
                    is_staff=ad_user.is_admin,
                )
                user = cast(CustomUser, user)

                # Prevent local password login for AD-managed users
                user.set_unusable_password()
                user.save(update_fields=["password"])

                return user

            # Local user now becomes AD-linked
            logger.info(
                "Linking existing Django user to AD identity: user_id=%s email=%s ad_guid=%s, admin=%s",
                user.id,
                user.email,
                ad_user.ad_guid,
                ad_user.is_admin
            )
            user.ad_guid = ad_user.ad_guid

        # Refresh fields from AD on every login
        user.email = ad_user.email
        user.username = ad_user.email
        user.first_name = ad_user.first_name
        user.last_name = ad_user.last_name
        user.last_ldap_sync = timezone.now()

        # Keep Django staff status synced to LDAP admin group
        user.is_staff = ad_user.is_admin

        # Prevent local password auth
        user.set_unusable_password()

        user.save(
            update_fields=[
                "ad_guid",
                "email",
                "username",
                "first_name",
                "last_name",
                "last_ldap_sync",
                "is_staff",
                "password",
            ]
        )

        return user


class LocalUserBackend(ModelBackend):
    """
    Allow local Django password authentication for local-only users and
    fallback superusers.

    When AD is enabled, this backend blocks ordinary local users and blocks
    AD-managed users from authenticating with their Django password.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """
        Authenticate local users according to the current AD mode.
        """
        user = super().authenticate(
            request,
            username=username,
            password=password,
            **kwargs,
        )
        user = cast(CustomUser | None, user)

        if user is None:
            return None

        if not settings.ACTIVE_DIRECTORY_ENABLED:
            return user

        if user.ad_guid:
            logger.warning(
                "Blocked local password login for AD-managed user: user_id=%s email=%s",
                user.id,
                user.email,
            )
            return None

        if user.is_local_only:
            logger.info(
                "Allowed local-only password login: user_id=%s email=%s",
                user.id,
                user.email,
            )
            return user

        if user.is_superuser:
            logger.info(
                "Allowed fallback superuser password login: user_id=%s email=%s",
                user.id,
                user.email,
            )
            return user

        logger.warning(
            "Blocked local password login while AD is enabled: user_id=%s email=%s",
            user.id,
            user.email,
        )
        return None
