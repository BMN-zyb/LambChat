# Security Configuration

Authentication and CAPTCHA settings.

## JWT Authentication

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `JWT_SECRET_KEY` | _(auto-generated)_ | Yes | JWT signing key. Auto-generated if not set. **Set a stable value in production.** |
| `JWT_ALGORITHM` | `HS256` | No | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_HOURS` | `24` | No | Access token expiration in hours. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | No | Refresh token expiration in days. |

::: warning
If `JWT_SECRET_KEY` is not set, a random key is generated at startup. This means all active sessions will be invalidated on every restart. **Always set a stable key in production.**
:::

## Cloudflare Turnstile (CAPTCHA)

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `TURNSTILE_ENABLED` | `false` | No | Enable Cloudflare Turnstile CAPTCHA. |
| `TURNSTILE_SITE_KEY` | _(empty)_ | No | Turnstile site key (used in frontend). |
| `TURNSTILE_SECRET_KEY` | _(empty)_ | Yes | Turnstile secret key (used in backend). |
| `TURNSTILE_REQUIRE_ON_LOGIN` | `false` | No | Require CAPTCHA on login. |
| `TURNSTILE_REQUIRE_ON_REGISTER` | `true` | No | Require CAPTCHA on registration. |
| `TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE` | `true` | No | Require CAPTCHA on password change. |

## User Management

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_USER_ROLE` | `user` | Default role for new users. |
| `ENABLE_REGISTRATION` | `true` | Enable user registration. |
| `ADMIN_CONTACT_EMAIL` | _(empty)_ | Admin contact email displayed in the UI. |
| `ADMIN_CONTACT_URL` | _(empty)_ | Admin contact URL displayed in the UI. |

## Example

```bash
# JWT
JWT_SECRET_KEY=your-stable-secret-key-at-least-32-chars
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_HOURS=24
REFRESH_TOKEN_EXPIRE_DAYS=7

# Turnstile CAPTCHA
TURNSTILE_ENABLED=true
TURNSTILE_SITE_KEY=0x4AAAAAAA
TURNSTILE_SECRET_KEY=0x4AAAAAAA
TURNSTILE_REQUIRE_ON_REGISTER=true

# User Management
ENABLE_REGISTRATION=true
DEFAULT_USER_ROLE=user
```
