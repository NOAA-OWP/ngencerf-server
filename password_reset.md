# How to Reset a User Password (API / Console Email Flow)

This project uses **Djoser** for password reset. In this environment, emails are sent to the **server log** (console email backend), not to the user. 
You must manually extract the reset token from the logs and complete the flow via API.

---

## Prerequisites

- The server must be running.
- `DJOSER["PASSWORD_RESET_CONFIRM_URL"]` must be configured.
- You have access to the server logs.
- You know the user’s email address.

---

## Step 1 — Request a Password Reset

Send a reset request for the user’s email:

```bash
curl -X POST http://localhost:8000/auth/users/reset_password/ \
  -H "Content-Type: application/json" \
  -d '{"email":"brian.cosgrove@noaa.gov"}'
```

If successful, this will return `204 No Content`.

---

## Step 2 — Find the Reset Link in the Server Log

Check the server log for the “email” that was generated. It will look like this:

```html
<p>Please go to the following page and choose a new password:</p>
<a href="http://127.0.0.1:8000/reset-password-confirm/Mg/d2rjy9-801c0c4220946a96c15daa2024ac8a0e">
  http://127.0.0.1:8000/reset-password-confirm/Mg/d2rjy9-801c0c4220946a96c15daa2024ac8a0e
</a>
<p>Your username, in case you've forgotten: <b>peter@nextgenwaterprediction.com</b></p>
```

---

## Step 3 — Extract UID and Token

From the link:

```
http://127.0.0.1:8000/reset-password-confirm/<UID>/<TOKEN>
```

Example:

- `UID` = `Mg`
- `TOKEN` = `d2rjy9-801c0c4220946a96c15daa2024ac8a0e`

> Note: The UID is a base64-encoded version of the user’s database ID.

---

## Step 4 — Set a Temporary Password

Call the confirm endpoint with the extracted values:

```bash
curl -X POST http://127.0.0.1:8000/auth/users/reset_password_confirm/ \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "Mg",
    "token": "d2rjy9-801c0c4220946a96c15daa2024ac8a0e",
    "new_password": "changeme!",
    "re_new_password": "changeme!"
  }'
```

**Expected response:**  
- `204 No Content` on success

> Note: The new password must satisfy Django’s configured password validators (minimum length, common-password check, numeric-only check, etc.).

---

## Step 5 — Verify Login

Test the new password:

```bash
curl -X POST http://127.0.0.1:8000/auth/jwt/create/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "peter@nextgenwaterprediction.com",
    "password": "changeme!"
  }'
```

A successful response will return a JWT access/refresh token pair.

---

## Security Notes

- This flow is intended for development and operations use only.
- Do not use console email in production.
- Reset tokens should not be exposed in shared logs.
- Add rate limiting on `/auth/users/reset_password/` to prevent abuse.
