# OAuth 配置

Google、GitHub 和 Apple 第三方认证设置。

## Google OAuth

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `OAUTH_GOOGLE_ENABLED` | `false` | 否 | 启用 Google OAuth。 |
| `OAUTH_GOOGLE_CLIENT_ID` | _(空)_ | 否 | Google OAuth 客户端 ID。 |
| `OAUTH_GOOGLE_CLIENT_SECRET` | _(空)_ | 是 | Google OAuth 客户端密钥。 |

## GitHub OAuth

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `OAUTH_GITHUB_ENABLED` | `false` | 否 | 启用 GitHub OAuth。 |
| `OAUTH_GITHUB_CLIENT_ID` | _(空)_ | 否 | GitHub OAuth 客户端 ID。 |
| `OAUTH_GITHUB_CLIENT_SECRET` | _(空)_ | 是 | GitHub OAuth 客户端密钥。 |

## Apple OAuth

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `OAUTH_APPLE_ENABLED` | `false` | 否 | 启用 Apple OAuth。 |
| `OAUTH_APPLE_CLIENT_ID` | _(空)_ | 否 | Apple OAuth 客户端 ID（Service ID）。 |
| `OAUTH_APPLE_CLIENT_SECRET` | _(空)_ | 是 | Apple OAuth 客户端密钥。 |
| `OAUTH_APPLE_TEAM_ID` | _(空)_ | 否 | Apple Team ID。 |
| `OAUTH_APPLE_KEY_ID` | _(空)_ | 否 | Apple Key ID。 |

## 设置指南

### Google OAuth

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 创建新项目或选择现有项目
3. 导航到 **APIs & Services** > **Credentials**
4. 创建 **OAuth 2.0 Client ID**（Web 应用）
5. 添加回调 URI：`https://your-domain.com/api/auth/oauth/google/callback`

### GitHub OAuth

1. 前往 [GitHub Developer Settings](https://github.com/settings/developers)
2. 点击 **New OAuth App**
3. 设置 **Authorization callback URL**：`https://your-domain.com/api/auth/oauth/github/callback`

### Apple OAuth

1. 前往 [Apple Developer](https://developer.apple.com/)
2. 在 Certificates, Identifiers & Profiles 中注册 **Services ID**
3. 为你的域名配置 **Sign in with Apple**

## 示例

```bash
# Google
OAUTH_GOOGLE_ENABLED=true
OAUTH_GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
OAUTH_GOOGLE_CLIENT_SECRET=GOCSPX-your-secret

# GitHub
OAUTH_GITHUB_ENABLED=true
OAUTH_GITHUB_CLIENT_ID=Ov23lixxxxxxxxx
OAUTH_GITHUB_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
