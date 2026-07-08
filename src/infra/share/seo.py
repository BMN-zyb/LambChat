from __future__ import annotations

# 本模块用于为"分享会话"页面与公开落地页生成 SEO/社交分享所需的元信息（标题、描述、robots、
# Open Graph/Twitter Card 字段等），并把这些信息以字符串替换的方式注入到前端构建产物的 index.html
# 中，实现服务端渲染（SSR-lite）级别的 SEO 优化：搜索引擎/社交平台抓取的是注入后的静态 HTML，
# 无需执行前端 JS 即可看到正确的标题、描述与预览内容。
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

# 分享页默认使用 noindex（不希望被搜索引擎收录，因为分享内容通常是私有/临时性质），
# 但仍允许 follow（爬虫可以顺着链接继续抓取站内其他页面）。
DEFAULT_SHARE_ROBOTS = "noindex, follow, max-image-preview:large"
# 公开落地页（首页、功能页等）则允许被索引。
INDEX_ROBOTS = "index, follow, max-image-preview:large"
NOINDEX_ROBOTS = "noindex, follow, max-image-preview:large"
DEFAULT_SHARE_PREVIEW_LABEL = "Shared session preview"
DEFAULT_SHARE_DESCRIPTION = "View this shared session on LambChat."
PUBLIC_HOME_PATH = "/"
# 常见搜索引擎/社交平台爬虫使用的 meta robots name，需要逐一写入对应的 <meta name="..." > 标签，
# 因为部分爬虫只识别自己专属的 robots meta 而不看通用的 "robots"。
CRAWLER_ROBOTS_META_NAMES = (
    "googlebot",
    "bingbot",
    "Baiduspider",
    "360Spider",
    "Sogou web spider",
    "YisouSpider",
    "Bytespider",
)

# 合并连续空白字符（含换行）为单个空格，用于规整从消息内容里提取出的预览文本。
_WHITESPACE_RE = re.compile(r"\s+")
# 匹配前端构建产物中挂载 React 根节点的占位 div。
_ROOT_DIV_RE = re.compile(r'<div id="root"></div>')
# 匹配"已经注入过一次的服务端预览块 + 根节点"整体，用于重复注入时先整体替换掉旧的预览块。
_SHARED_PREVIEW_RE = re.compile(
    r'(<div id="shared-server-preview">.*?</div>\s*)?<div id="root"></div>', re.S
)


@dataclass(frozen=True)
class SharedPageSeo:
    # 分享会话页面的 SEO 元信息载体：标题/描述用于 <title>/<meta description>，
    # preview_* 字段用于服务端渲染的可见预览卡片，author_name/published_date 用于展示署名与时间。
    title: str
    description: str
    canonical_url: str
    robots: str
    og_type: str
    preview_label: str
    preview_title: str
    preview_summary: str
    author_name: str
    published_date: str


@dataclass(frozen=True)
class PublicRouteSeo:
    # 公开落地页（如首页、/features 等）的 SEO 元信息载体，preview_html 是可直接注入到根节点内的静态预览内容。
    title: str
    description: str
    canonical_url: str
    robots: str
    og_type: str
    preview_html: str


@dataclass(frozen=True)
class PublicLandingRoute:
    # 公开落地页的静态文案配置：标题/描述用于 SEO，eyebrow/heading/bullets 用于渲染预览卡片正文。
    title: str
    description: str
    eyebrow: str
    heading: str
    bullets: tuple[str, ...]


# 站点各公开路由对应的 SEO 文案与预览内容配置表；只有出现在这里的路径才会被视为"可索引的公开落地页"，
# 其余路径（如聊天界面内部路由）默认走 noindex。
PUBLIC_LANDING_ROUTES: dict[str, PublicLandingRoute] = {
    "/": PublicLandingRoute(
        title="LambChat - AI Agent Platform | Multi-Model Chat & Skill Engine",
        description="LambChat is a pluggable, multi-tenant AI conversation platform for teams building multi-model agents with Skills, MCP tools, streaming chat, document processing, and role-based access.",
        eyebrow="AI agent workspace",
        heading="LambChat AI Agent Platform",
        bullets=(
            "Multi-model AI chat for Claude, GPT, Gemini, and other LLM providers.",
            "Skills engine and Model Context Protocol integrations for extensible agent workflows.",
            "Real-time streaming conversations, document processing, and team-ready access control.",
        ),
    ),
    "/interface": PublicLandingRoute(
        title="LambChat Interface - AI Agent Workspace",
        description="Explore the LambChat interface for streaming chat, file-aware conversations, shared sessions, and team-ready AI agent workflows.",
        eyebrow="Main interface",
        heading="LambChat AI Agent Workspace",
        bullets=(
            "Streaming conversations with multi-model AI agents.",
            "Document and image workflows built into the chat surface.",
            "Shareable sessions and responsive workspace layouts.",
        ),
    ),
    "/features": PublicLandingRoute(
        title="LambChat Core Features - AI Agent Platform",
        description="LambChat core features include Skills, MCP tools, role-based access, multi-agent orchestration, document processing, feedback, and observability.",
        eyebrow="Core features",
        heading="LambChat Core Features",
        bullets=(
            "Skills engine for reusable AI capabilities and prompt workflows.",
            "MCP tools for connecting external systems, services, and data.",
            "Role-based access, teams, feedback, notifications, and observability.",
        ),
    ),
    "/architecture": PublicLandingRoute(
        title="LambChat Architecture - Skills and MCP Dual Engine",
        description="LambChat architecture combines a Skills engine, MCP integrations, streaming sessions, sandboxed tools, and multi-tenant access control.",
        eyebrow="Architecture",
        heading="Skills and MCP Dual Engine",
        bullets=(
            "Composable agent workflows backed by Skills and MCP servers.",
            "Real-time SSE streaming with durable session and task handling.",
            "Sandboxed tool execution, storage services, and access boundaries.",
        ),
    ),
    "/dashboard": PublicLandingRoute(
        title="LambChat Management Panels - Agents, Skills, Models and Teams",
        description="Manage LambChat agents, models, Skills, MCP servers, channels, files, teams, users, roles, memory, and notifications from one workspace.",
        eyebrow="Management panels",
        heading="Agents, Skills, Models and Teams",
        bullets=(
            "Configure agents, model providers, MCP servers, and channels.",
            "Manage Skills, marketplace content, files, memory, and personas.",
            "Administer users, roles, teams, notifications, and system settings.",
        ),
    ),
    "/responsive": PublicLandingRoute(
        title="LambChat Responsive Design - Desktop, Tablet and Mobile AI Workspace",
        description="LambChat provides a responsive AI workspace for desktop, tablet, and mobile usage with adaptive chat, panels, and navigation.",
        eyebrow="Responsive design",
        heading="Desktop, Tablet and Mobile Workspace",
        bullets=(
            "Adaptive navigation and panels across desktop and mobile screens.",
            "Touch-friendly chat, file, and management workflows.",
            "PWA-ready shell with offline status and install metadata.",
        ),
    ),
    "/github": PublicLandingRoute(
        title="LambChat GitHub - Open Source AI Agent Platform",
        description="Explore LambChat on GitHub, the open source AI agent platform for multi-model chat, Skills, MCP integrations, and team workflows.",
        eyebrow="Open source",
        heading="LambChat on GitHub",
        bullets=(
            "Review the open source codebase and deployment assets.",
            "Track LambChat features, issues, and release updates.",
            "Contribute to the AI agent platform and integration ecosystem.",
        ),
    ),
}

PUBLIC_NAVIGATION_ITEMS: tuple[tuple[str, str], ...] = (
    ("Features", "/features"),
    ("Architecture", "/architecture"),
    ("Management Panels", "/dashboard"),
    ("Responsive Design", "/responsive"),
    ("GitHub", "/github"),
)


def _normalize_text(value: Any) -> str:
    # 非字符串输入统一视为空文本；字符串则压缩内部空白并去掉首尾空格，便于后续拼接/截断处理。
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def _truncate_text(value: str, max_length: int) -> str:
    # 按最大长度截断文本，尽量在单词边界处断开（避免截断到单词中间），并去掉断点处残留的标点，
    # 最后补上省略号；若按空格切分后为空（如长单词或 CJK 无空格文本），则直接硬截断。
    if len(value) <= max_length:
        return value

    clipped = value[: max_length + 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    if not clipped:
        clipped = value[:max_length]
    return f"{clipped}..."


def _extract_preview_lines(events: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    # 从会话事件流中提取"第一条用户消息"与"第一条助手回复"，作为分享页预览摘要的原始素材。
    # 一旦两者都拿到就提前结束遍历，避免扫描整段可能很长的会话历史。
    first_user = ""
    first_assistant = ""

    for event in events:
        event_type = event.get("event_type", "")
        data = event.get("data") or {}
        content = _normalize_text(data.get("content"))
        if not content:
            continue

        if event_type == "user:message" and not first_user:
            first_user = content
            continue

        if event_type in {"message:chunk", "assistant:message"} and not first_assistant:
            first_assistant = content

        if first_user and first_assistant:
            break

    return first_user, first_assistant


def _format_date(value: Any) -> str:
    # 尝试把任意时间表示解析为 ISO 日期（仅年月日）；解析失败则退化为截取原始文本的前 10 个字符
    # （多数 ISO 时间字符串的前 10 位恰好就是日期部分），保证该函数不会因格式异常而抛出异常。
    text = _normalize_text(value)
    if not text:
        return ""

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]


def build_shared_page_seo(
    *,
    base_url: str,
    share_id: str,
    session: Mapping[str, Any],
    owner: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
    app_name: str = "LambChat",
    indexable: bool = False,
) -> SharedPageSeo:
    # 组装分享会话页面的完整 SEO 信息：标题优先取会话名称，否则回退为用户首条消息的截断文本。
    first_user, first_assistant = _extract_preview_lines(events)

    raw_title = _normalize_text(session.get("name")) or _truncate_text(
        first_user or "Shared session",
        60,
    )
    preview_title = raw_title or "Shared session"
    title = f"{preview_title} - {app_name} Shared Session"

    # 描述由用户首条消息 + 助手首条回复各截断 140 字符拼接而成，两者都缺失时用默认文案兜底。
    description_parts = [
        _truncate_text(first_user, 140) if first_user else "",
        _truncate_text(first_assistant, 140) if first_assistant else "",
    ]
    description = " ".join(part for part in description_parts if part).strip()
    if not description:
        description = DEFAULT_SHARE_DESCRIPTION

    summary = description
    if summary == DEFAULT_SHARE_DESCRIPTION and _normalize_text(session.get("agent_name")):
        summary = f"{summary} Agent: {_normalize_text(session.get('agent_name'))}."

    # 仅当调用方显式声明该分享是"可被索引"的公开内容时才放开 robots，否则始终 noindex。
    robots = "index, follow, max-image-preview:large" if indexable else DEFAULT_SHARE_ROBOTS
    canonical_url = f"{base_url.rstrip('/')}/shared/{share_id}"

    return SharedPageSeo(
        title=title,
        description=description,
        canonical_url=canonical_url,
        robots=robots,
        og_type="article",
        preview_label=DEFAULT_SHARE_PREVIEW_LABEL,
        preview_title=preview_title,
        preview_summary=summary,
        author_name=_normalize_text((owner or {}).get("username")),
        published_date=_format_date(session.get("created_at")),
    )


def build_shared_page_error_seo(
    *,
    base_url: str,
    share_id: str,
    app_name: str = "LambChat",
    reason: str,
) -> SharedPageSeo:
    # 分享链接不可用（不存在/需要登录/其它原因）时，同样生成一份 SEO 结构，
    # 保证错误态页面依然有合理的标题与描述，而不是留空或报错。
    messages = {
        "not_found": (
            "Shared session not found",
            "This shared session was not found or is no longer available.",
        ),
        "auth_required": (
            "Login required for shared session",
            "Sign in to view this shared session.",
        ),
    }
    preview_title, description = messages.get(
        reason,
        ("Shared session unavailable", "This shared session is currently unavailable."),
    )

    return SharedPageSeo(
        title=f"{preview_title} - {app_name}",
        description=description,
        canonical_url=f"{base_url.rstrip('/')}/shared/{share_id}",
        robots=DEFAULT_SHARE_ROBOTS,
        og_type="article",
        preview_label=DEFAULT_SHARE_PREVIEW_LABEL,
        preview_title=preview_title,
        preview_summary=description,
        author_name="",
        published_date="",
    )


def _canonical_url(base_url: str, path: str) -> str:
    # 拼接规范化 URL：确保路径部分以 "/" 开头，且根路径不会被裁剪成缺少末尾斜杠的 base_url。
    normalized_path = path if path.startswith("/") else f"/{path}"
    if normalized_path == "/":
        return f"{base_url.rstrip('/')}/"
    return f"{base_url.rstrip('/')}{normalized_path}"


def _is_public_indexable_path(path: str) -> bool:
    # 判断某路径是否属于配置表中登记的"可索引公开落地页"。
    normalized_path = PUBLIC_HOME_PATH if path == "" else path
    return normalized_path in PUBLIC_LANDING_ROUTES


def _build_public_preview_html(route: PublicLandingRoute) -> str:
    # 为公开落地页生成一段纯静态的预览 HTML（内联样式，不依赖前端构建产物的 CSS），
    # 直接塞进 <div id="root"> 里，让爬虫/未执行 JS 的客户端也能看到有意义的内容。
    # 所有动态文案都经过 html.escape 转义，防止配置内容中出现特殊字符破坏 HTML 结构。
    feature_items = "\n".join(f"      <li>{html.escape(feature)}</li>" for feature in route.bullets)
    nav_items = "\n".join(
        f'      <li><a href="{path}">{html.escape(label)}</a></li>'
        for label, path in PUBLIC_NAVIGATION_ITEMS
    )
    return f"""<main data-public-preview="server" style="max-width: 920px; margin: 0 auto; padding: 48px 24px; font-family: 'Source Sans 3', Arial, sans-serif; color: #1c1917; background: #faf9f7;">
    <p style="margin: 0 0 12px; font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; color: #78716c;">{html.escape(route.eyebrow)}</p>
    <h1>{html.escape(route.heading)}</h1>
    <p style="font-size: 18px; line-height: 1.7; color: #44403c;">{html.escape(route.description)}</p>
    <ul style="font-size: 16px; line-height: 1.8; color: #44403c;">
{feature_items}
    </ul>
    <nav aria-label="LambChat public sections">
      <ul style="font-size: 15px; line-height: 1.8; color: #44403c;">
{nav_items}
      </ul>
    </nav>
  </main>"""


def _public_route_structured_data(seo: PublicRouteSeo) -> str:
    # 生成 schema.org 结构化数据（JSON-LD），帮助搜索引擎理解站点结构：
    # WebSite（含站内导航）、SoftwareApplication（应用本身的描述信息）、BreadcrumbList（面包屑导航）。
    origin_match = re.match(r"^(https?://[^/]+)", seo.canonical_url)
    origin = origin_match.group(1) if origin_match else seo.canonical_url.rstrip("/")

    navigation = [
        {
            "@type": "SiteNavigationElement",
            "name": label,
            "url": _canonical_url(origin, path),
        }
        for label, path in PUBLIC_NAVIGATION_ITEMS
    ]
    breadcrumb_items = [
        {
            "@type": "ListItem",
            "position": 1,
            "name": "Home",
            "item": _canonical_url(origin, "/"),
        }
    ]
    if seo.canonical_url.rstrip("/") != _canonical_url(origin, "/").rstrip("/"):
        breadcrumb_items.append(
            {
                "@type": "ListItem",
                "position": 2,
                "name": seo.title.split(" - ", 1)[0].removeprefix("LambChat "),
                "item": seo.canonical_url,
            }
        )

    data = [
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "LambChat",
            "url": _canonical_url(origin, "/"),
            "hasPart": navigation,
        },
        {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "LambChat",
            "applicationCategory": "ChatApplication",
            "operatingSystem": "Web",
            "url": _canonical_url(origin, "/"),
            "description": PUBLIC_LANDING_ROUTES[PUBLIC_HOME_PATH].description,
            "codeRepository": "https://github.com/Yanyutin753/LambChat",
        },
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": breadcrumb_items,
        },
    ]
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_public_route_seo(*, base_url: str, path: str) -> PublicRouteSeo:
    # 为公开路由生成 SEO 信息：命中配置表则使用对应文案并允许索引，否则退化为通用兜底文案并禁止索引。
    normalized_path = path if path.startswith("/") else f"/{path}"
    if normalized_path == "":
        normalized_path = PUBLIC_HOME_PATH

    landing_route = PUBLIC_LANDING_ROUTES.get(normalized_path)
    is_public_landing = landing_route is not None
    title = landing_route.title if landing_route else "LambChat"
    description = landing_route.description if landing_route else "LambChat application route."

    return PublicRouteSeo(
        title=title,
        description=description,
        canonical_url=_canonical_url(base_url, normalized_path),
        robots=INDEX_ROBOTS if is_public_landing else NOINDEX_ROBOTS,
        og_type="website",
        preview_html=_build_public_preview_html(landing_route) if landing_route else "",
    )


def _replace_title(html_doc: str, value: str) -> str:
    # 用正则替换 <title> 标签内容；HTML 构建产物里始终存在唯一的 <title>，因此不需要"插入"分支。
    replacement = f"<title>{html.escape(value, quote=True)}</title>"
    return re.sub(r"<title>.*?</title>", replacement, html_doc, count=1, flags=re.S)


def _replace_link(html_doc: str, rel: str, href: str) -> str:
    # 替换指定 rel 的 <link> 标签（如 canonical）；若原文档中不存在该标签，则退化为在 </head> 前插入一条新标签。
    escaped_href = html.escape(href, quote=True)
    replacement = f'<link rel="{rel}" href="{escaped_href}" />'
    pattern = re.compile(rf'<link\s+rel="{re.escape(rel)}"\s+href="[^"]*"\s*/?>', re.I)
    if pattern.search(html_doc):
        return pattern.sub(replacement, html_doc, count=1)
    return html_doc.replace("</head>", f"    {replacement}\n  </head>", 1)


def _replace_meta(html_doc: str, attr: str, name: str, content: str) -> str:
    # 通用的 <meta> 标签替换/插入辅助函数：attr 是标识用的属性名（"name" 或 "property"，
    # 分别对应普通 meta 与 Open Graph meta），若匹配不到已有标签则同样退化为插入到 </head> 前。
    escaped = html.escape(content, quote=True)
    replacement = f'<meta {attr}="{name}" content="{escaped}" />'
    pattern = re.compile(
        rf'<meta\s+[^>]*{attr}="{re.escape(name)}"[^>]*content="[^"]*"[^>]*>',
        re.I,
    )
    if pattern.search(html_doc):
        return pattern.sub(replacement, html_doc, count=1)
    return html_doc.replace("</head>", f"    {replacement}\n  </head>", 1)


def _replace_crawler_robots_meta(html_doc: str, robots: str) -> str:
    # 为每一个已知爬虫名单逐个写入专属的 robots meta，确保各家爬虫都能读到一致的收录策略。
    rendered = html_doc
    for crawler in CRAWLER_ROBOTS_META_NAMES:
        rendered = _replace_meta(rendered, "name", crawler, robots)
    return rendered


def inject_share_seo_into_html(html_doc: str, seo: SharedPageSeo) -> str:
    # 把分享页 SEO 信息整体注入到前端构建产物的 HTML 中：依次替换 title/canonical/description/
    # robots（含各爬虫专属 meta）/Open Graph/Twitter Card 字段，最后再插入一段服务端渲染的可见预览卡片。
    rendered = html_doc
    rendered = _replace_title(rendered, seo.title)
    rendered = _replace_link(rendered, "canonical", seo.canonical_url)
    rendered = _replace_meta(rendered, "name", "description", seo.description)
    rendered = _replace_meta(rendered, "name", "robots", seo.robots)
    rendered = _replace_crawler_robots_meta(rendered, seo.robots)
    rendered = _replace_meta(rendered, "property", "og:type", seo.og_type)
    rendered = _replace_meta(rendered, "property", "og:title", seo.title)
    rendered = _replace_meta(rendered, "property", "og:description", seo.description)
    rendered = _replace_meta(rendered, "property", "og:url", seo.canonical_url)
    rendered = _replace_meta(rendered, "name", "twitter:title", seo.title)
    rendered = _replace_meta(rendered, "name", "twitter:description", seo.description)
    rendered = _replace_meta(rendered, "property", "og:image:alt", seo.preview_title)
    rendered = _replace_meta(rendered, "name", "twitter:image:alt", seo.preview_title)

    byline_parts = [part for part in [seo.author_name, seo.published_date] if part]
    byline = " · ".join(byline_parts)
    summary_html = html.escape(seo.preview_summary, quote=True)
    byline_html = html.escape(byline, quote=True)

    preview_markup = f"""<div id="shared-server-preview">
  <main data-shared-preview="server" style="max-width: 760px; margin: 0 auto; padding: 48px 24px; font-family: 'Source Sans 3', sans-serif; color: #1c1917; background: #faf9f7; min-height: 100vh;">
    <p style="margin: 0 0 12px; font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; color: #78716c;">{html.escape(seo.preview_label, quote=True)}</p>
    <h1 style="margin: 0 0 16px; font-size: 40px; line-height: 1.15; color: #1c1917;">{html.escape(seo.preview_title, quote=True)}</h1>
    <p style="margin: 0 0 16px; font-size: 18px; line-height: 1.7; color: #44403c;">{summary_html}</p>
    <p style="margin: 0; font-size: 14px; color: #78716c;">{byline_html}</p>
  </main>
</div>"""

    # 若文档中已存在旧的预览块（例如同一进程内被处理过一次），先整体替换掉旧块，
    # 否则按普通的根节点占位符替换，插入新的预览块 + 根节点。
    replacement = f'{preview_markup}\n<div id="root"></div>'
    if _SHARED_PREVIEW_RE.search(rendered):
        return _SHARED_PREVIEW_RE.sub(replacement, rendered, count=1)
    if _ROOT_DIV_RE.search(rendered):
        return _ROOT_DIV_RE.sub(replacement, rendered, count=1)
    return rendered


def inject_public_route_seo_into_html(html_doc: str, seo: PublicRouteSeo) -> str:
    # 与分享页注入逻辑类似，但额外为"可索引"的公开落地页追加 JSON-LD 结构化数据脚本，
    # 并在存在预览 HTML 时把它直接注入根节点（保留原根节点包裹，而非替换整个 div）。
    rendered = html_doc
    rendered = _replace_title(rendered, seo.title)
    rendered = _replace_link(rendered, "canonical", seo.canonical_url)
    rendered = _replace_meta(rendered, "name", "description", seo.description)
    rendered = _replace_meta(rendered, "name", "robots", seo.robots)
    rendered = _replace_crawler_robots_meta(rendered, seo.robots)
    rendered = _replace_meta(rendered, "property", "og:type", seo.og_type)
    rendered = _replace_meta(rendered, "property", "og:title", seo.title)
    rendered = _replace_meta(rendered, "property", "og:description", seo.description)
    rendered = _replace_meta(rendered, "property", "og:url", seo.canonical_url)
    rendered = _replace_meta(rendered, "name", "twitter:title", seo.title)
    rendered = _replace_meta(rendered, "name", "twitter:description", seo.description)
    if seo.robots == INDEX_ROBOTS:
        structured_data = _public_route_structured_data(seo)
        script = f'<script type="application/ld+json">\n{structured_data}\n</script>'
        rendered = rendered.replace("</head>", f"    {script}\n  </head>", 1)

    if not seo.preview_html:
        return rendered

    replacement = f'<div id="root">\n  {seo.preview_html}\n</div>'
    if _ROOT_DIV_RE.search(rendered):
        return _ROOT_DIV_RE.sub(replacement, rendered, count=1)
    return rendered
