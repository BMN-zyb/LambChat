"""Helpers for local filesystem preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.infra.logging import get_logger

logger = get_logger(__name__)


def should_prepare_local_filesystem(settings: Any) -> bool:
    """Whether this process should rely on local filesystem storage paths."""
    # 是否应使用本地文件系统作为存储：未启用 S3 则一定用本地；
    # 即便启用了 S3,若其 provider 为 "local"(本地模拟)也仍走本地目录。
    if not getattr(settings, "S3_ENABLED", False):
        return True
    return str(getattr(settings, "S3_PROVIDER", "") or "").lower() == "local"


def ensure_local_filesystem_dirs(
    settings: Any,
    *,
    default_upload_dir: str | Path = "./uploads",
) -> None:
    """Create local directories that the app expects to exist at startup."""
    # 启动时预建应用所需的本地目录;若使用对象存储(非本地)则直接跳过。
    if not should_prepare_local_filesystem(settings):
        logger.info("Skipping local filesystem directory preparation for object storage mode")
        return

    # 上传根目录:优先取配置的 LOCAL_STORAGE_PATH,为空则用传入的默认值。
    upload_path = Path(getattr(settings, "LOCAL_STORAGE_PATH", "") or default_upload_dir)

    # 需要确保存在的目录:上传根目录 + 用于「已揭示文件/项目」的两个子目录。
    required_paths: list[Path] = [
        upload_path,
        upload_path / "revealed_files",
        upload_path / "revealed_projects",
    ]

    # 逐个创建(parents=True 递归建父目录,exist_ok=True 已存在不报错)。
    for path in required_paths:
        path.mkdir(parents=True, exist_ok=True)
        logger.info("Ensured local directory exists: %s", path.resolve())
