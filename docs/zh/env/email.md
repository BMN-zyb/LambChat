# 邮件配置

使用 Resend 发送事务性邮件（验证、密码重置）的邮件服务设置。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `EMAIL_ENABLED` | `false` | 否 | 启用邮件服务。 |
| `RESEND_ACCOUNTS` | `[]` | 是 | Resend 账户配置的 JSON 数组。见下方格式。 |
| `PASSWORD_RESET_EXPIRE_HOURS` | `24` | 否 | 密码重置链接过期时间（小时）。 |
| `REQUIRE_EMAIL_VERIFICATION` | `false` | 否 | 注册时要求邮箱验证。 |

## RESEND_ACCOUNTS 格式

```json
[
  {
    "api_key": "re_xxxxxxxx",
    "email_from": "noreply@example.com",
    "email_from_name": "LambChat"
  }
]
```

## 示例

```bash
EMAIL_ENABLED=true
RESEND_ACCOUNTS=[{"api_key":"re_xxxxxxxx","email_from":"noreply@example.com","email_from_name":"LambChat"}]
PASSWORD_RESET_EXPIRE_HOURS=24
REQUIRE_EMAIL_VERIFICATION=true
```

::: warning
如果启用了 `REQUIRE_EMAIL_VERIFICATION` 但没有正确配置 `RESEND_ACCOUNTS`，用户将无法完成注册。
:::
