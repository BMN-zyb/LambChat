"""HTML email template renderer with cross-client compatibility."""

# ---------------------------------------------------------------------------
# 模块说明：跨邮件客户端兼容的 HTML 邮件模板渲染器
#
# 本模块提供 EmailTemplate 类，负责把「标题/图标/正文/按钮/页脚」等字段
# 套版渲染成一封完整的 HTML 邮件。设计要点：
#   1. 安全：所有纯文本字段统一做 HTML 转义，URL 字段做协议白名单校验，
#      防止用户内容注入正文导致 XSS，或注入 javascript:/data: 危险链接。
#   2. 兼容：邮件客户端（尤其是 Outlook）对 CSS 支持极不统一，因此大量采用
#      table 布局 + 内联样式，并通过 MSO 条件注释 + VML 为 Outlook 单独渲染
#      圆角按钮，保证各客户端下视觉尽量一致。
#   3. 无状态：全部方法为 staticmethod，不持有实例状态，可直接以类名调用。
# 具体的多语言文案由同目录的 texts.py 提供，本模块只负责套版渲染。
# ---------------------------------------------------------------------------

# 启用未来注解语义，支持延迟求值的类型注解
from __future__ import annotations

# html：提供 HTML 转义能力，防止用户内容注入邮件正文造成 XSS
import html
from typing import Optional


class EmailTemplate:
    """Email template renderer with consistent styling.

    All plain-text fields are HTML-escaped to prevent XSS.
    HTML fields (greeting, content, footer) are passed through as-is;
    callers must escape any user-provided values before inserting.
    """

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters to prevent XSS attacks."""
        # quote=True 同时转义引号，确保放入属性值时也安全
        return html.escape(str(text), quote=True)

    @staticmethod
    def _escape_url(url: str) -> str:
        """Validate and escape URL to prevent javascript: and data: URL attacks."""
        url = str(url).strip()
        # 只放行 http/https 协议，拦截 javascript:/data: 等危险协议
        if url.startswith(("http://", "https://")):
            return html.escape(url, quote=True)
        # 非法协议返回空串，等价于「无链接」
        return ""

    @staticmethod
    def render(
        title: str,
        icon_url: str,
        heading: str,
        greeting: str,
        content: str,
        button_url: str,
        button_text: str,
        footer: Optional[str] = None,
    ) -> str:
        """Render HTML email template with XSS protection.

        Args:
            title: Email title in header (plain text, will be escaped)
            icon_url: URL to the brand icon image
            heading: Main heading (plain text, will be escaped)
            greeting: Greeting HTML (may contain <strong>, <br>, etc.)
            content: Content HTML (may contain <br>, etc.)
            button_url: Button link URL (validated to only allow http/https)
            button_text: Button text (plain text, will be escaped)
            footer: Optional footer HTML (may contain <br>, etc.)

        Returns:
            Complete HTML email content.
        """
        # 纯文本字段统一转义后再插入 HTML，杜绝 XSS
        safe_title = EmailTemplate._escape_html(title)
        safe_heading = EmailTemplate._escape_html(heading)
        # 按钮/图标 URL 需经协议校验，非法则变为空串
        safe_button_url = EmailTemplate._escape_url(button_url)
        safe_button_text = EmailTemplate._escape_html(button_text)
        safe_icon_url = EmailTemplate._escape_url(icon_url)

        # 图标块：仅在图标 URL 合法时渲染，否则留空
        icon_html = (
            f'<div style="width: 72px; height: 72px; margin: 0 auto 20px auto; '
            f"background: rgba(255,255,255,0.1); border-radius: 20px; "
            f'text-align: center; line-height: 72px;">'
            f'<img src="{safe_icon_url}" alt="{safe_title}" width="44" height="44" '
            f'style="display: inline-block; border: 0; width: 44px; height: 44px; '
            f'vertical-align: middle;" class="mobile-full"></div>'
            if safe_icon_url
            else ""
        )

        # 页脚块：仅在传入 footer 时渲染（footer 为可含 HTML 的富文本）
        footer_html = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
            f'<tr><td style="padding-bottom: 8px; font-size: 13px; line-height: 1.5; '
            f'color: #78716c; text-align: center;">'
            f"{footer}"
            f"</td></tr></table>"
            if footer
            else ""
        )

        # 按钮 URL 合法时渲染可点击按钮
        if safe_button_url:
            # 使用 MSO 条件注释 + VML roundrect 为 Outlook 渲染圆角按钮（Outlook 不支持 CSS 圆角）
            button_html = (
                f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" '
                f'xmlns:w="urn:schemas-microsoft-com:office:word" href="{safe_button_url}" '
                f'style="height:50px;v-text-anchor:middle;width:260px;" arcsize="25%" '
                f'strokecolor="#292524" fillcolor="#292524"><w:anchorlock/>'
                f'<center style="color:#ffffff;font-family:Helvetica,Arial,sans-serif;'
                f'font-size:15px;font-weight:600;">{safe_button_text}</center></v:roundrect><![endif]-->'
                f'<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
                f'align="center" style="margin: 0 auto;" class="mobile-button-container">'
                f'<tr><td align="center" style="border-radius: 12px; '
                f'background-color: #292524;" class="mobile-button-bg">'
                f'<a href="{safe_button_url}" target="_blank" style="font-size: 15px; font-family: '
                f"Helvetica, Arial, sans-serif; font-weight: 600; color: #ffffff; text-decoration: none; "
                f"border-radius: 12px; padding: 16px 44px; border: 1px solid #292524; display: inline-block; "
                f'mso-padding-alt: 0; text-align: center; letter-spacing: 0.2px;">{safe_button_text}</a>'
                f"</td></tr></table>"
            )
        else:
            # URL 非法时降级为不可点击的纯样式按钮（避免渲染出危险链接）
            button_html = (
                f'<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
                f'align="center" style="margin: 0 auto;">'
                f'<tr><td align="center" style="border-radius: 12px; '
                f'background-color: #292524;">'
                f'<span style="font-size: 15px; font-family: Helvetica, Arial, sans-serif; '
                f"font-weight: 600; color: #ffffff; border-radius: 12px; padding: 16px 44px; "
                f'display: inline-block; text-align: center; letter-spacing: 0.2px;">'
                f"{safe_button_text}</span>"
                f"</td></tr></table>"
            )

        # 预览文案（preheader）：收件箱列表中标题旁的摘要文字，取转义后的标题
        preheader = EmailTemplate._escape_html(heading)

        # 返回完整 HTML；注意 f-string 中 CSS 的花括号需写成 {{ }} 转义
        return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="x-apple-disable-message-reformatting">
  <title>{safe_title} - {safe_heading}</title>
  <!--[if mso]>
  <noscript>
    <xml>
      <o:OfficeDocumentSettings>
        <o:AllowPNG/>
        <o:PixelsPerInch>96</o:PixelsPerInch>
      </o:OfficeDocumentSettings>
    </xml>
  </noscript>
  <![endif]-->
  <style type="text/css">
    body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    img {{ -ms-interpolation-mode: bicubic; border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; }}
    body {{ margin: 0; padding: 0; width: 100% !important; height: 100% !important; }}
    .mobile-full {{ width: 100% !important; max-width: 100% !important; }}
    .mobile-padding {{ padding-left: 20px !important; padding-right: 20px !important; }}
    @media only screen and (max-width: 620px) {{
      .mobile-full {{ width: 100% !important; max-width: 100% !important; }}
      .mobile-padding {{ padding-left: 20px !important; padding-right: 20px !important; }}
      .mobile-stack {{ display: block !important; width: 100% !important; }}
      .mobile-text-center {{ text-align: center !important; }}
      .mobile-button-bg {{ width: 100% !important; text-align: center !important; }}
      .mobile-button-bg a {{ width: 100% !important; display: block !important; box-sizing: border-box !important; }}
    }}
  </style>
</head>
<body style="margin: 0; padding: 0; background-color: #f5f5f4; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #1c1917; -webkit-font-smoothing: antialiased;">

  <!-- Preheader -->
  <div style="display: none; font-size: 1px; color: #f5f5f4; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden;">
    {preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>

  <!-- Wrapper -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color: #f5f5f4;">
    <tr>
      <td align="center" style="padding: 48px 16px;">

        <!-- Main container 600px -->
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" class="mobile-full" style="max-width: 600px; width: 100%; border-radius: 16px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.06);">

          <!-- Header -->
          <tr>
            <td style="background-color: #292524; border-radius: 16px 16px 0 0; padding: 48px 40px 40px 40px; text-align: center;" class="mobile-padding">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td>
                    {icon_html}
                  </td>
                </tr>
                <tr>
                  <td>
                    <h1 style="margin: 0; padding: 0; font-size: 24px; font-weight: 700; line-height: 1.3; color: #fafaf9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; letter-spacing: -0.3px;">{safe_title}</h1>
                  </td>
                </tr>
                <tr>
                  <td style="padding-top: 10px;">
                    <p style="margin: 0; font-size: 14px; line-height: 1.5; color: #a8a29e; font-weight: 400;">{safe_heading}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background-color: #ffffff; padding: 40px; border-left: 1px solid #e7e5e4; border-right: 1px solid #e7e5e4;" class="mobile-padding">

              <!-- Heading -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom: 8px;">
                    <h2 style="margin: 0; padding: 0; font-size: 20px; font-weight: 600; line-height: 1.4; color: #1c1917; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">{safe_heading}</h2>
                  </td>
                </tr>
                <tr>
                  <td style="padding-bottom: 24px;">
                    <div style="height: 3px; width: 36px; background-color: #78716c; border-radius: 2px;"></div>
                  </td>
                </tr>
              </table>

              <!-- Greeting -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom: 20px; font-size: 15px; line-height: 1.7; color: #44403c;">
                    {greeting}
                  </td>
                </tr>
              </table>

              <!-- Content -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom: 36px; font-size: 15px; line-height: 1.7; color: #44403c;">
                    {content}
                  </td>
                </tr>
              </table>

              <!-- CTA Button -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom: 8px; text-align: center;">
                    {button_html}
                  </td>
                </tr>
              </table>

              <!-- Fallback link -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom: 32px; text-align: center; font-size: 13px; line-height: 1.5; color: #a8a29e;">
                    {safe_button_url}
                  </td>
                </tr>
              </table>

              {footer_html}

            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="background-color: #ffffff; padding: 0 40px; border-left: 1px solid #e7e5e4; border-right: 1px solid #e7e5e4;" class="mobile-padding">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="border-top: 1px solid #f5f5f4; font-size: 1px; line-height: 1px;">&nbsp;</td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color: #ffffff; border-radius: 0 0 16px 16px; padding: 24px 40px 32px 40px; text-align: center; border-left: 1px solid #e7e5e4; border-right: 1px solid #e7e5e4; border-bottom: 1px solid #e7e5e4;" class="mobile-padding">
              <p style="margin: 0 0 8px 0; font-size: 13px; line-height: 1.5; color: #78716c; font-weight: 500;">
                {safe_title}
              </p>
              <p style="margin: 0; font-size: 12px; line-height: 1.5; color: #a8a29e;">
                This is an automated email. Please do not reply.
              </p>
            </td>
          </tr>

        </table>
        <!-- /Main container -->

      </td>
    </tr>
  </table>
  <!-- /Wrapper -->

</body>
</html>"""
