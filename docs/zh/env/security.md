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

## Web Push（VAPID）

VAPID 密钥用于认证浏览器 Web Push 推送通知。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `VAPID_PUBLIC_KEY` | _(自动生成)_ | 否 | 发送给浏览器、用于创建推送订阅的 VAPID 公钥。 |
| `VAPID_PRIVATE_KEY` | _(自动生成)_ | 是 | 服务端发送 Web Push 时用于签名请求的 VAPID 私钥。 |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | 否 | VAPID claims 中的联系主体。建议使用你控制的 `mailto:` 邮箱或 HTTPS URL。 |

::: warning
生产环境和多实例部署必须使用固定的 VAPID 密钥对。如果密钥对变化，已有浏览器推送订阅可能会失效，用户需要重新订阅。
:::

在项目根目录执行下面的命令生成密钥：

```bash
uv run python - <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

private_key = ec.generate_private_key(ec.SECP256R1())
private_der = private_key.private_bytes(
    Encoding.DER,
    PrivateFormat.PKCS8,
    NoEncryption(),
)
public_raw = private_key.public_key().public_bytes(
    Encoding.X962,
    PublicFormat.UncompressedPoint,
)

print("VAPID_PUBLIC_KEY=" + base64.urlsafe_b64encode(public_raw).decode())
print("VAPID_PRIVATE_KEY=" + base64.urlsafe_b64encode(private_der).decode())
print("VAPID_SUBJECT=mailto:admin@example.com")
PY
```

把输出复制到 `.env` 或生产环境的密钥管理系统里。记得把 `VAPID_SUBJECT` 改成真实管理员邮箱，例如 `mailto:ops@example.com`。

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

# Web Push（VAPID）
VAPID_PUBLIC_KEY=your-generated-vapid-public-key
VAPID_PRIVATE_KEY=your-generated-vapid-private-key
VAPID_SUBJECT=mailto:ops@example.com

# Turnstile 验证码
TURNSTILE_ENABLED=true
TURNSTILE_SITE_KEY=0x4AAAAAAA
TURNSTILE_SECRET_KEY=0x4AAAAAAA
TURNSTILE_REQUIRE_ON_REGISTER=true

# 用户管理
ENABLE_REGISTRATION=true
DEFAULT_USER_ROLE=user
```
