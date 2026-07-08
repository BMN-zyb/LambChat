"""Localized email text content for all supported languages."""

# 多语言邮件文案表，三层结构：语言码 -> 邮件类型 -> 文案字段
# 支持语言：zh/en/ja/ko/ru；邮件类型：password_reset/verify_email/welcome
# 文案字段含占位符（如 {from_name}/{username}/{hours}），发送时用 str.format 填充
EMAIL_TEXTS: dict[str, dict[str, dict[str, str]]] = {
    "zh": {
        "password_reset": {
            "subject": "{from_name} - 重置密码",
            "heading": "重置您的密码",
            "greeting": "您好，<strong>{username}</strong>！",
            "content": "我们收到了重置您密码的请求。请点击下方按钮重置密码：",
            "button_text": "重置密码",
            "footer": "此链接将在 {hours} 小时后失效。<br>如果您没有请求重置密码，请忽略此邮件。",
        },
        "verify_email": {
            "subject": "{from_name} - 验证您的邮箱",
            "heading": "验证您的邮箱",
            "greeting": "您好，<strong>{username}</strong>！",
            "content": "感谢您注册 {from_name}！请点击下方按钮验证您的邮箱地址：",
            "button_text": "验证邮箱",
            "footer": "如果您没有注册 {from_name}，请忽略此邮件。",
        },
        "welcome": {
            "subject": "欢迎加入 {from_name}！",
            "heading": "欢迎加入！",
            "greeting": "您好，<strong>{username}</strong>！",
            "content": "欢迎加入 {from_name}！立即登录开始使用吧。",
            "button_text": "开始使用",
            "footer": "",
        },
    },
    "en": {
        "password_reset": {
            "subject": "{from_name} - Password Reset",
            "heading": "Reset Your Password",
            "greeting": "Hello <strong>{username}</strong>!",
            "content": "We received a request to reset your password. Please click the button below to reset it:",
            "button_text": "Reset Password",
            "footer": "This link will expire in {hours} hours. If you didn't request this, you can safely ignore this email.",
        },
        "verify_email": {
            "subject": "{from_name} - Verify Your Email",
            "heading": "Verify Your Email",
            "greeting": "Hello <strong>{username}</strong>!",
            "content": "Thank you for registering with {from_name}! Please click the button below to verify your email address:",
            "button_text": "Verify Email",
            "footer": "If you didn't register with {from_name}, you can safely ignore this email.",
        },
        "welcome": {
            "subject": "Welcome to {from_name}!",
            "heading": "Welcome!",
            "greeting": "Hello <strong>{username}</strong>!",
            "content": "Welcome to {from_name}! Start using it now.",
            "button_text": "Get Started",
            "footer": "",
        },
    },
    "ja": {
        "password_reset": {
            "subject": "{from_name} - パスワードリセット",
            "heading": "パスワードのリセット",
            "greeting": "<strong>{username}</strong> 様",
            "content": "パスワードのリセットリクエストを受け取りました。下のボタンをクリックしてリセットしてください：",
            "button_text": "パスワードをリセット",
            "footer": "このリンクは {hours} 時間後に失效します。",
        },
        "verify_email": {
            "subject": "{from_name} - メール認証",
            "heading": "メールを認証する",
            "greeting": "<strong>{username}</strong> 様",
            "content": "{from_name} へのご登録ありがとうございます！下のボタンをクリックしてメールを認証してください：",
            "button_text": "メールを認証",
            "footer": "",
        },
        "welcome": {
            "subject": "{from_name} へようこそ！",
            "heading": "ようこそ！",
            "greeting": "<strong>{username}</strong> 様",
            "content": "{from_name} へようこそ！今すぐ始めましょう。",
            "button_text": "始める",
            "footer": "",
        },
    },
    "ko": {
        "password_reset": {
            "subject": "{from_name} - 비밀번호 재설정",
            "heading": "비밀번호 재설정",
            "greeting": "<strong>{username}</strong>님 안녕하세요!",
            "content": "비밀번호 재설정 요청을 받았습니다. 아래 버튼을 클릭하여 재설정하세요:",
            "button_text": "비밀번호 재설정",
            "footer": "이 링크는 {hours} 시간後に失效합니다.",
        },
        "verify_email": {
            "subject": "{from_name} - 이메일 인증",
            "heading": "이메일 인증",
            "greeting": "<strong>{username}</strong>님 안녕하세요!",
            "content": "{from_name}에 가입해 주셔서 감사합니다! 아래 버튼을 클릭하여 이메일 주소를 인증하세요:",
            "button_text": "이메일 인증",
            "footer": "",
        },
        "welcome": {
            "subject": "{from_name}에 오신 것을 환영합니다!",
            "heading": "환영합니다!",
            "greeting": "<strong>{username}</strong>님 안녕하세요!",
            "content": "{from_name}에 오신 것을 환영합니다! 지금 바로 시작하세요.",
            "button_text": "시작하기",
            "footer": "",
        },
    },
    "ru": {
        "password_reset": {
            "subject": "{from_name} - Сброс пароля",
            "heading": "Сброс пароля",
            "greeting": "Привет, <strong>{username}</strong>!",
            "content": "Мы получили запрос на сброс пароля. Нажмите кнопку ниже, чтобы сбросить пароль:",
            "button_text": "Сбросить пароль",
            "footer": "Ссылка действительна в течение {hours} часов.",
        },
        "verify_email": {
            "subject": "{from_name} - Подтверждение email",
            "heading": "Подтвердите ваш email",
            "greeting": "Привет, <strong>{username}</strong>!",
            "content": "Спасибо за регистрацию в {from_name}! Нажмите кнопку ниже, чтобы подтвердить ваш email:",
            "button_text": "Подтвердить email",
            "footer": "",
        },
        "welcome": {
            "subject": "Добро пожаловать в {from_name}!",
            "heading": "Добро пожаловать!",
            "greeting": "Привет, <strong>{username}</strong>!",
            "content": "Добро пожаловать в {from_name}! Начните использовать прямо сейчас.",
            "button_text": "Начать",
            "footer": "",
        },
    },
}

# Fallback default
# 默认回退语言：当请求的语言或邮件类型不存在时使用英文
_DEFAULT_LANG = "en"


def get_texts(lang: str, email_type: str) -> dict[str, str]:
    """Get localized text for an email type.

    Args:
        lang: 2-letter language code (en, zh, ja, ko, ru)
        email_type: one of 'password_reset', 'verify_email', 'welcome'

    Returns:
        Dict with keys: subject, heading, greeting, content, button_text, footer
    """
    # 语言未命中回退英文；再按邮件类型取；类型仍未命中则回退英文对应类型，最终兜底为空字典
    return EMAIL_TEXTS.get(lang, EMAIL_TEXTS[_DEFAULT_LANG]).get(
        email_type, EMAIL_TEXTS[_DEFAULT_LANG].get(email_type, {})
    )
