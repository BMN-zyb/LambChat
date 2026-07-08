"""GitHub client for fetching release information."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx

# 目标仓库与「最新 release」API 地址；缓存有效期 1 小时(减少对 GitHub API 的请求频率)。
GITHUB_REPO = "Yanyutin753/LambChat"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CACHE_TTL_SECONDS = 3600  # 1 hour


@dataclass
class GitHubRelease:
    """GitHub release information"""

    # tag_name: 版本标签; html_url: release 页面链接; published_at: 发布时间(ISO 字符串)。
    tag_name: str
    html_url: str
    published_at: str
    # body: release 说明正文; assets: 附件列表(名称/下载地址/大小/类型)。
    body: str = ""
    assets: list[dict] = field(default_factory=list)


class GitHubClient:
    """Client for fetching GitHub release information with simple in-memory cache."""

    def __init__(self):
        # 进程内内存缓存:缓存的 release 及其写入时间,用于按 TTL 判断是否复用。
        self._cache: Optional[GitHubRelease] = None
        self._cache_time: Optional[datetime] = None

    async def get_latest_release(self, force_refresh: bool = False) -> Optional[GitHubRelease]:
        """Get latest release from GitHub, using cache if available"""
        # 非强制刷新且缓存有效时直接返回缓存;否则请求 API,成功才更新缓存。
        if not force_refresh and self._is_cache_valid():
            return self._cache

        release = await self._fetch_release()
        if release:
            self._cache = release
            self._cache_time = datetime.now(UTC)
        return release

    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid"""
        # 缓存有效的条件:已有缓存且距写入时间未超过 TTL。
        if self._cache is None or self._cache_time is None:
            return False
        elapsed = datetime.now(UTC) - self._cache_time
        return elapsed < timedelta(seconds=CACHE_TTL_SECONDS)

    async def _fetch_release(self) -> Optional[GitHubRelease]:
        """Fetch latest release from GitHub API"""
        # 请求 GitHub API(10s 超时);仅 200 才解析返回,其余状态码或任何异常都返回 None(静默降级)。
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    GITHUB_API_URL, headers={"Accept": "application/vnd.github+json"}
                )
                if response.status_code == 200:
                    data = response.json()
                    return self._parse_release(data)
                return None
        except Exception:
            return None

    def _parse_release(self, data: dict) -> GitHubRelease:
        """Parse GitHub API response"""
        # 把 API 原始 JSON 收敛成内部 GitHubRelease;各字段用 .get 兜底,缺失时给安全默认值。
        assets = []
        for asset in data.get("assets", []):
            assets.append(
                {
                    "name": asset.get("name", ""),
                    "url": asset.get("browser_download_url", ""),
                    "size": asset.get("size"),
                    "content_type": asset.get("content_type", "application/octet-stream"),
                }
            )
        return GitHubRelease(
            tag_name=data.get("tag_name", ""),
            html_url=data.get("html_url", ""),
            published_at=data.get("published_at", ""),
            body=data.get("body", ""),
            assets=assets,
        )


# Singleton instance
# 模块级单例:全局共享同一个客户端及其缓存。
github_client = GitHubClient()
