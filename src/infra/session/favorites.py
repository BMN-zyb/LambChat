"""Helpers for decoupled session favorites."""

from typing import Any, Mapping


def is_session_favorite(
    metadata: Mapping[str, Any] | None,
    favorites_project_id: str | None = None,
) -> bool:
    """Return whether a session should be treated as favorited.

    New data uses ``metadata.is_favorite``.
    Legacy data stored favorites by moving the session into the special
    favorites project, so we still recognize that shape while migrating.
    """

    # 判定会话是否被收藏，兼容新旧两种数据形态
    data = metadata or {}
    # 新数据：直接读取显式布尔标记 is_favorite
    explicit = data.get("is_favorite")
    if isinstance(explicit, bool):
        return explicit

    # 旧数据：把会话移入"收藏夹"特殊项目来表示收藏，迁移期间仍需识别这种形态
    return bool(
        favorites_project_id
        and isinstance(data.get("project_id"), str)
        and data.get("project_id") == favorites_project_id
    )


def normalize_session_metadata(
    metadata: Mapping[str, Any] | None,
    favorites_project_id: str | None = None,
) -> dict[str, Any]:
    """Return session metadata with a normalized favorite flag."""

    # 拷贝一份 metadata，避免就地修改调用方数据
    normalized = dict(metadata or {})
    # 无论新旧形态，只要判定为收藏就补齐统一的 is_favorite=True 字段供上层使用
    if is_session_favorite(normalized, favorites_project_id):
        normalized["is_favorite"] = True
    return normalized
