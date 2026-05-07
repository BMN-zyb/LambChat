# Email Configuration

Email service settings using Resend for transactional emails (verification, password reset).

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `EMAIL_ENABLED` | `false` | No | Enable email service. |
| `RESEND_ACCOUNTS` | `[]` | Yes | JSON array of Resend account configs. See format below. |
| `PASSWORD_RESET_EXPIRE_HOURS` | `24` | No | Password reset link expiration in hours. |
| `REQUIRE_EMAIL_VERIFICATION` | `false` | No | Require email verification for signup. |

## RESEND_ACCOUNTS Format

```json
[
  {
    "api_key": "re_xxxxxxxx",
    "email_from": "noreply@example.com",
    "email_from_name": "LambChat"
  }
]
```

## Example

```bash
EMAIL_ENABLED=true
RESEND_ACCOUNTS=[{"api_key":"re_xxxxxxxx","email_from":"noreply@example.com","email_from_name":"LambChat"}]
PASSWORD_RESET_EXPIRE_HOURS=24
REQUIRE_EMAIL_VERIFICATION=true
```

::: warning
If you enable `REQUIRE_EMAIL_VERIFICATION` without properly configuring `RESEND_ACCOUNTS`, users will not be able to complete registration.
:::
