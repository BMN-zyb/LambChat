# OAuth Configuration

Third-party authentication settings for Google, GitHub, and Apple.

## Google OAuth

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `OAUTH_GOOGLE_ENABLED` | `false` | No | Enable Google OAuth. |
| `OAUTH_GOOGLE_CLIENT_ID` | _(empty)_ | No | Google OAuth client ID. |
| `OAUTH_GOOGLE_CLIENT_SECRET` | _(empty)_ | Yes | Google OAuth client secret. |

## GitHub OAuth

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `OAUTH_GITHUB_ENABLED` | `false` | No | Enable GitHub OAuth. |
| `OAUTH_GITHUB_CLIENT_ID` | _(empty)_ | No | GitHub OAuth client ID. |
| `OAUTH_GITHUB_CLIENT_SECRET` | _(empty)_ | Yes | GitHub OAuth client secret. |

## Apple OAuth

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `OAUTH_APPLE_ENABLED` | `false` | No | Enable Apple OAuth. |
| `OAUTH_APPLE_CLIENT_ID` | _(empty)_ | No | Apple OAuth client ID (Service ID). |
| `OAUTH_APPLE_CLIENT_SECRET` | _(empty)_ | Yes | Apple OAuth client secret. |
| `OAUTH_APPLE_TEAM_ID` | _(empty)_ | No | Apple Team ID. |
| `OAUTH_APPLE_KEY_ID` | _(empty)_ | No | Apple Key ID. |

## Setup Guides

### Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Navigate to **APIs & Services** > **Credentials**
4. Create an **OAuth 2.0 Client ID** (Web application)
5. Add your redirect URI: `https://your-domain.com/api/auth/oauth/google/callback`

### GitHub OAuth

1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
2. Click **New OAuth App**
3. Set **Authorization callback URL** to: `https://your-domain.com/api/auth/oauth/github/callback`

### Apple OAuth

1. Go to [Apple Developer](https://developer.apple.com/)
2. Register a **Services ID** in Certificates, Identifiers & Profiles
3. Configure **Sign in with Apple** for your domain

## Example

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
