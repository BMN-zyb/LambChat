# 安全配置

认证和验证码设置。

## JWT 认证

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `JWT_SECRET_KEY` | _(自动生成)_ | 是 | JWT 签名密钥。未设置时自动生成。**生产环境务必设置固定值。** |
| `JWT_ALGORITHM` | `HS256` | 否 | JWT 签名算法。 |
| `ACCESS_TOKEN_EXPIRE_HOURS` | `24` | 否 | 访问令牌过期时间（小时）。 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | 否 | 刷新令牌过期时间（天）。 |

::: warning
如果不设置 `JWT_SECRET_KEY`，每次启动时会生成随机密钥。这意味着每次重启后所有活跃会话都将失效。**生产环境务必设置固定的密钥。**
:::

## Cloudflare Turnstile（验证码）

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `TURNSTILE_ENABLED` | `false` | 否 | 启用 Cloudflare Turnstile 验证码。 |
| `TURNSTILE_SITE_KEY` | _(空)_ | 否 | Turnstile 站点密钥（前端使用）。 |
| `TURNSTILE_SECRET_KEY` | _(空)_ | 是 | Turnstile 密钥（后端使用）。 |
| `TURNSTILE_REQUIRE_ON_LOGIN` | `false` | 否 | 登录时要求验证码。 |
| `TURNSTILE_REQUIRE_ON_REGISTER` | `true` | 否 | 注册时要求验证码。 |
| `TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE` | `true` | 否 | 修改密码时要求验证码。 |

## 用户管理

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DEFAULT_USER_ROLE` | `user` | 新用户默认角色。 |
| `ENABLE_REGISTRATION` | `true` | 启用用户注册。 |
| `ADMIN_CONTACT_EMAIL` | _(空)_ | 管理员联系邮箱，在 UI 中显示。 |
| `ADMIN_CONTACT_URL` | _(空)_ | 管理员联系 URL，在 UI 中显示。 |

## 示例

```bash
# JWT
JWT_SECRET_KEY=your-stable-secret-key-at-least-32-chars
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_HOURS=24
REFRESH_TOKEN_EXPIRE_DAYS=7

# Turnstile 验证码
TURNSTILE_ENABLED=true
TURNSTILE_SITE_KEY=0x4AAAAAAA
TURNSTILE_SECRET_KEY=0x4AAAAAAA
TURNSTILE_REQUIRE_ON_REGISTER=true

# 用户管理
ENABLE_REGISTRATION=true
DEFAULT_USER_ROLE=user
```
