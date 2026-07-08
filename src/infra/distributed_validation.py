"""Startup validation for distributed runtime safety."""

from __future__ import annotations

import os
from typing import Any

from src.infra.local_filesystem import should_prepare_local_filesystem


class DistributedRuntimeConfigError(RuntimeError):
    """Raised when settings are unsafe for multi-replica deployments."""


def _parse_bool(value: str | None) -> bool | None:
    # 把环境变量字符串解析为三态布尔：可识别的真/假值分别返回 True/False，
    # None 或无法识别时返回 None(表示「未显式配置」，交由上层选择默认策略)。
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def is_distributed_runtime(environ: dict[str, str] | None = None) -> bool:
    """Return whether this process should enforce multi-replica safety checks."""
    # 判定当前进程是否处于「分布式(多副本)运行」，从而决定是否强制执行多副本安全校验。
    # 优先级：显式的 LAMBCHAT_DISTRIBUTED_MODE > 副本数 LAMBCHAT_REPLICA_COUNT>1 > 默认 False。
    env = environ if environ is not None else os.environ
    explicit = _parse_bool(env.get("LAMBCHAT_DISTRIBUTED_MODE"))
    if explicit is not None:
        return explicit

    replica_count = env.get("LAMBCHAT_REPLICA_COUNT")
    if replica_count:
        try:
            return int(replica_count) > 1
        except ValueError:
            # 副本数非法(无法解析为整数)时保守地视为非分布式。
            return False

    return False


def _was_generated(settings: Any, attr_name: str) -> bool:
    # 判断某项密钥类配置是否为「运行时自动生成」(而非显式设置)。
    # settings 上形如 _xxx_generated 的标志位为 True 表示是随机生成的，在多副本下各副本值会不一致。
    return bool(getattr(settings, attr_name, False))


def validate_distributed_runtime_settings(
    settings: Any,
    *,
    distributed_mode: bool | None = None,
) -> None:
    """Fail fast when process-local defaults would break distributed deployments."""
    # 分布式部署下,进程级默认值会导致副本间不一致或数据不可共享,此处提前(fail fast)校验并报错。
    # distributed_mode 显式给定则以之为准,否则由 is_distributed_runtime() 自动判定。
    enabled = is_distributed_runtime() if distributed_mode is None else distributed_mode
    if not enabled:
        return

    # 收集所有不安全项,最后一次性抛出,便于运维一次看到全部问题。
    errors: list[str] = []
    # JWT 密钥若是自动生成,各副本各不相同,会导致 token 跨副本失效,必须显式统一配置。
    if _was_generated(settings, "_jwt_secret_key_generated"):
        errors.append("JWT_SECRET_KEY must be explicitly set and identical across replicas")
    # MCP 加密盐同理,自动生成会造成副本间无法互相解密。
    if _was_generated(settings, "_mcp_encryption_salt_generated"):
        errors.append("MCP_ENCRYPTION_SALT must be explicitly set and identical across replicas")
    # 若还需准备「本地文件系统」,说明未启用共享对象存储;多副本下上传文件无法互通,必须启用 S3。
    if should_prepare_local_filesystem(settings):
        errors.append("S3_ENABLED=true with shared object storage is required for uploads")

    if errors:
        raise DistributedRuntimeConfigError("; ".join(errors))
