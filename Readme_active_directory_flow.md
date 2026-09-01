# Authentication / Active Directory UI Notes

## 1. Auth Configuration Endpoint

Before displaying the login screen, call:

`GET /auth/config/`

The UI should call this at least:

* on application startup
* before each login attempt

This allows the UI to adapt if server authentication settings changed after a restart/config update.

Example response:

```json
{
    "active_directory_enabled": true,
    "allow_self_registration": false,
    "allow_password_change": false
}
```

UI behavior:

* `active_directory_enabled`

  * Indicates whether Active Directory authentication is enabled

* `allow_self_registration`

  * If `false`, hide registration UI
  * If `true`, allow local self-registration

* `allow_password_change`

  * If `false`, hide normal password change UI
  * If `true`, allow password change UI

# 2. Login Flow

Use:

`POST /auth/login/`

Do NOT use:

* `/auth/jwt/create/`
* `/auth/token/login/`

Possible responses:

## Standard login success

```json
{
    "access": "...",
    "refresh": "...",
    "first_name": "Peter",
    "last_name": "K",
    "message": "Login successful"
}
```

## MFA setup required

```json
{
    "mfa_setup_required": true,
    "mfa_token": "...",
    "message": "MFA setup required before login."
}
```

UI should redirect to MFA setup flow.

## MFA verification required

```json
{
    "mfa_required": true,
    "mfa_token": "...",
    "message": "MFA verification required."
}
```

UI should prompt for MFA code.

Note:

* The email address returned by the server after login may differ from the email entered by the user.
* Active Directory may normalize or map the login identity to a different canonical email address.
* The UI should treat the server-returned user identity as authoritative.

# 3. Important Error Responses

## Invalid credentials

```json
{
    "response_type": "error",
    "error_code": "INVALID_CREDENTIALS",
    "message": "Invalid credentials"
}
```

Show generic login failure.

## User not authorized for this system

```json
{
    "response_type": "error",
    "error_code": "USER_NOT_AUTHORIZED",
    "message": "User is not authorized for this system."
}
```

This means:

* AD authentication succeeded
* but the user is not in the required AD group(s)

## Active Directory unavailable

```json
{
    "response_type": "error",
    "error_code": "ACTIVE_DIRECTORY_UNAVAILABLE",
    "message": "Active Directory authentication service is currently unavailable."
}
```

This is a server/infrastructure issue.
UI should display a system error message.

# 4. Disabled Endpoints When AD Is Enabled

The following Djoser endpoints are disabled when AD is enabled:

* `/auth/users/`
* `/auth/users/set_password/`
* `/auth/users/reset_password/`
* `/auth/users/reset_password_confirm/`

The UI should not call them.

For password changes (when AD is disabled), use:

`POST /auth/change_password/`

# 5. Local-Only Users

Local-only users:

* are admin-created
* bypass AD authentication
* still authenticate through `/auth/login/`

Password changes use:

`POST /auth/change_password/`

However:

* when Active Directory is enabled, the normal UI should hide password change functionality
* password changes for local-only users are primarily intended for admin/CLI workflows
