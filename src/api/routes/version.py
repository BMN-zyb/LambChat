"""Version info route."""

# 版本信息路由模块（挂载于 /api）
# 职责：返回当前应用版本、git 信息、构建时间，并对比 GitHub 最新发布判断是否有更新
# GitHub 查询结果带缓存，force_refresh 可强制刷新
from fastapi import APIRouter, Query

from src.infra.github_client import github_client
from src.kernel.config import settings
from src.kernel.schemas.agent import ReleaseAsset, VersionResponse
from src.kernel.version_utils import has_new_version, normalize_version

router = APIRouter()


# GET /api/version —— 获取版本信息，无需鉴权
# 查询参数 force_refresh：强制刷新对 GitHub 最新发布的缓存
# 返回当前版本/git tag/commit/构建时间，以及 GitHub 最新版本、更新提示、发布说明与下载资源
@router.get("/version", response_model=VersionResponse)
async def get_version(
    force_refresh: bool = Query(False, description="Force refresh GitHub cache"),
) -> VersionResponse:
    """Get application version info including git tag and build time."""
    # 从 GitHub 拉取最新 release（带缓存，force_refresh 时跳过缓存）
    # Fetch latest from GitHub
    latest_release = await github_client.get_latest_release(force_refresh=force_refresh)

    # 比较当前版本与 GitHub 最新 tag，判断是否有新版本可用
    # Determine if update available
    has_update = False
    if latest_release:
        has_update = has_new_version(settings.APP_VERSION, latest_release.tag_name)

    # 把最新 release 附带的下载资源(assets) 转换为 ReleaseAsset 列表
    release_assets = None
    if latest_release:
        release_assets = [ReleaseAsset(**asset) for asset in latest_release.assets]

    return VersionResponse(
        app_version=settings.APP_VERSION,
        git_tag=settings.GIT_TAG,
        commit_hash=settings.COMMIT_HASH,
        build_time=settings.BUILD_TIME,
        latest_version=normalize_version(latest_release.tag_name) if latest_release else None,
        release_url=latest_release.html_url if latest_release else None,
        github_url=settings.GITHUB_URL,
        has_update=has_update,
        published_at=latest_release.published_at if latest_release else None,
        release_notes=latest_release.body if latest_release else None,
        release_assets=release_assets,
    )
