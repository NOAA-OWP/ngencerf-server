# Active Directory Authentication Implementation

## Overview

The system supports optional Active Directory (AD) authentication while continuing to use Django as the source of application-specific user data.

When Active Directory is enabled:

* credentials are validated against AD
* Django users are automatically linked or created
* authorization is controlled through AD group membership
* local self-registration is disabled
* standard password management is disabled
* MFA continues to function normally

When Active Directory is disabled:

* authentication uses standard Django credentials
* Djoser self-registration remains available
* normal password changes are allowed

---

## Intended Deployment Model

Active Directory enablement is primarily intended to be a stable deployment-time configuration.

The system supports enabling or disabling AD dynamically for:

* development
* testing
* troubleshooting
* migration scenarios

However, repeatedly switching AD on and off in a production environment is not recommended.

Reasons include:

* AD-linked users may not have usable Django passwords
* existing `ad_guid` links remain stored
* local authentication behavior changes depending on configuration
* users may require administrative password resets after AD is disabled
* stale or partially linked accounts can create confusing authentication behavior

In normal production usage:

* AD-enabled systems are expected to remain AD-enabled
* non-AD systems are expected to remain non-AD

The ability to toggle AD exists mainly to support development and operational recovery workflows.

---

## Authentication Flow

User login always occurs through:

`POST /auth/login/`

Flow:

1. User submits email and password
2. Local-only users are checked first
3. If not local-only:

   * Active Directory authenticates credentials
   * required AD group membership is verified
4. Existing Django users are linked automatically
5. New Django users are created automatically on first login
6. MFA flow is applied if enabled
7. JWT tokens are issued after successful authentication

---

## Active Directory Configuration

Example `.env` settings:

```text
LDAP_DOMAIN=nextgenwaterprediction.com
LDAP_SYSTEM_NAME=local
LDAP_USER_SEARCH_BASE_DN=DC=nextgenwaterprediction,DC=com
```

The system derives authorization groups dynamically:

```python
LDAP_REQUIRED_GROUP_USERS = f"ngencerf-{LDAP_SYSTEM_NAME}-users"
LDAP_ADMIN_GROUP = f"ngencerf-{LDAP_SYSTEM_NAME}-admins"
```

Examples:

For:

```text
LDAP_SYSTEM_NAME=local
```

Expected groups:

```text
ngencerf-local-users
ngencerf-local-admins
```

For:

```text
LDAP_SYSTEM_NAME=dev
```

Expected groups:

```text
ngencerf-dev-users
ngencerf-dev-admins
```

For:

```text
LDAP_SYSTEM_NAME=oe
```

Expected groups:

```text
ngencerf-oe-users
ngencerf-oe-admins
```

Authentication requires membership in the `*-users` group.

Administrative privileges require membership in the `*-admins` group.

Users in the admin group automatically receive:

* `is_staff=True`

The current deployment uses:

```text
LDAP_SYSTEM_NAME=local
```

which expects:

```text
ngencerf-local-users
ngencerf-local-admins
```

The backend currently checks direct memberships returned from `memberOf`.

Nested group traversal is not currently implemented.

---

## Django User Linking

Users are linked in the following order:

1. Existing `ad_guid`
2. Existing email match
3. Create new Django user

Linked AD users store:

* `ad_guid`
* email
* first name
* last name
* `last_ldap_sync`

AD profile values refresh during every login.

The email entered during login may differ from the final stored email.

Example:

User logs in with:

```text
peter.a.kronenberg@nextgenwaterprediction.com
```

AD may normalize this to:

```text
peter.a.kronenberg@rtx.com
```

The AD-returned email becomes the canonical Django identity.

---

## Local-Only Users

Local-only users are special Django users that bypass Active Directory.

Properties:

* created only by administrators
* authenticate using Django passwords
* are never linked to AD
* use `is_local_only=True`

Authentication checks local-only users before querying Active Directory.

Local-only users are intended for:

* emergency access
* testing
* isolated administrative workflows
* non-AD users

---

## Default Superuser / Bootstrap Admin

The server startup script ensures that one Django superuser exists.

The account is configured in `cerfserver.env`:

```text
DJANGO_SUPERUSER_EMAIL=admin@nextgenwaterprediction.com
DJANGO_SUPERUSER_PASSWORD=...
```

During startup, `runCerf.sh` checks whether a superuser with that email already exists. If it does not exist, the script creates it using:

```bash
python manage.py createsuperuser --noinput
```

This account is important because it provides a stable administrative fallback.

It can be used for admin-only CLI operations, including:

* creating local-only users
* resetting passwords
* recovering access when AD configuration is incorrect
* recovering access when AD is disabled after users were previously linked

When Active Directory is enabled, this default superuser remains a local Django account and is not AD-managed.

When Active Directory is disabled, this account can still be used to perform CLI administrative operations.

Currently, there is no normal UI or CLI workflow for promoting another user to admin/staff status. To make another user an admin, the `is_staff` flag must be set manually in the database.

Example:

```sql
-- Promote an existing Django user to staff/admin access.
UPDATE custom_user
SET is_staff = TRUE
WHERE email = 'user@example.com';
```

Use this carefully. Staff/admin users can perform privileged CLI operations.

If the bootstrap admin password is lost or becomes invalid, it can be reset directly through Django using:

```bash
python manage.py changepassword admin@nextgenwaterprediction.com
```

This provides a recovery mechanism even if:

* Active Directory configuration is broken
* all AD admin access is lost
* the stored bootstrap password in `cerfserver.env` is outdated
* normal UI authentication is unavailable

The command must be run on the server with access to the Django environment and database.

---

## Password Behavior

When AD is enabled:

* user self-registration is disabled
* normal password management UI is hidden
* AD-managed users cannot change passwords through Django
* local-only users still authenticate using Django passwords

Password changes use:

`POST /auth/change_password/`

The UI should not expose password changes when:

```text
allow_password_change=false
```

Administrative password resets are performed through CLI workflows.

---

## Behavior When AD Is Disabled After Users Were Linked

When a user authenticates through AD, their Django account becomes linked using `ad_guid`.

If AD is later disabled:

* linked users remain linked
* `ad_guid` values remain stored
* authentication no longer checks Active Directory
* most AD-created users do not have usable Django passwords

Disabling AD alone is therefore usually not enough to allow login.

An administrator typically needs to assign a new Django password:

```bash
ngencerf change-password --email user@example.com
```

Clearing `ad_guid` is optional while AD is disabled.

However, clearing `ad_guid` may still be useful when:

* permanently converting a user into a local-only account
* testing relink behavior
* intentionally removing stale AD linkage
* forcing a future AD relink by email

Example:

```sql
-- Disconnect user from Active Directory
UPDATE custom_user
SET ad_guid = NULL
WHERE email='user@example.com';
```

Without resetting the password, login will still fail.

---

## Configuration Endpoint

UI clients should query:

`GET /auth/config/`

Example:

```json
{
    "active_directory_enabled": true,
    "allow_self_registration": false,
    "allow_password_change": false
}
```

The UI should call this:

* during application startup
* before login attempts

This allows the UI to adapt if server configuration changes after restart or deployment updates.

---

## Admin CLI Commands

Administrative operations are intended to be performed through the CLI rather than through the normal UI.

Create local-only user:

```bash
ngencerf create-local-user \
    --email user@example.com \
    --first-name John \
    --last-name Smith
```

Change your own password:

```bash
ngencerf change-password
```

Administrative password reset:

```bash
ngencerf change-password --email user@example.com
```

Rules:

* `create-local-user` requires an authenticated admin account

* local-only users cannot be created through the UI

* `change-password` with no `--email` changes the authenticated user's password

* `change-password --email user@example.com` changes another user's password

* changing another user's password requires admin privileges

* when AD is enabled:

  * AD-managed users cannot change their own passwords
  * administrators may reset passwords only for local-only users

* when AD is disabled:

  * administrators may reset passwords for any user

* password validation rules still apply

* all CLI commands call authenticated server APIs and enforce server-side authorization

---

## Disabled Djoser Endpoints

When Active Directory is enabled, the following endpoints are disabled:

* `/auth/jwt/create/`
* `/auth/token/*`
* `/auth/users/`
* `/auth/users/set_password/`
* `/auth/users/reset_password/`
* `/auth/users/reset_password_confirm/`

Clients should use the custom authentication endpoints instead.
