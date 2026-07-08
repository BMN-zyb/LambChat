"""
MCP敏感字段加密模块

提供env和headers字段的加密/解密功能，使用Fernet对称加密。
"""

import base64
import hashlib
import json
from typing import Any, Optional

# Fernet：对称加密算法（AES-128-CBC + HMAC），保证密文机密性与完整性
from cryptography.fernet import Fernet

from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# 加密字段标识（用于区分加密和未加密的数据）
# 约定：加密后的值形如 {"__encrypted__": "<base64密文>"}，据此判断是否需要解密
ENCRYPTED_MARKER = "__encrypted__"

# 密钥派生参数
# PBKDF2 迭代次数越高，暴力破解成本越高，但派生耗时也越长（10 万次约 100ms CPU）
_KDF_ITERATIONS = 100000  # PBKDF2 迭代次数


# 从配置读取 PBKDF2 盐值并转为 bytes；未配置时直接抛错，避免用空盐降低安全性
def _get_kdf_salt() -> bytes:
    """获取PBKDF2盐值，从配置读取并转换为bytes"""
    salt = settings.MCP_ENCRYPTION_SALT
    if not salt:
        raise RuntimeError("MCP_ENCRYPTION_SALT is not configured")
    return salt.encode("utf-8")


class DecryptionError(Exception):
    """解密失败异常"""

    pass


# 旧版密钥派生方式（向后兼容）：直接对 JWT_SECRET_KEY 做单次 SHA256
# 仅用于解密 PR #52 之前写入的历史密文，不再用于新数据加密
def _get_fernet_legacy() -> Fernet:
    """
    获取旧版Fernet加密实例（向后兼容）

    使用单次 SHA256 哈希派生密钥（PR #52 之前的方式）
    仅用于解密旧数据，不用于新加密。
    结果会被缓存。
    """
    # 命中缓存则直接返回，避免重复派生
    global _fernet_legacy_cache
    if _fernet_legacy_cache is not None:
        return _fernet_legacy_cache

    # SHA256 摘要得到 32 字节密钥，再 base64 编码为 Fernet 要求的 key 格式
    key = hashlib.sha256(settings.JWT_SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key)
    _fernet_legacy_cache = Fernet(fernet_key)
    return _fernet_legacy_cache


# 缓存 Fernet 实例，避免每次加解密都执行 PBKDF2（100K 迭次约 100ms CPU）
# 模块级单例：新旧两套密钥各缓存一份
_fernet_cache: Optional[Fernet] = None
_fernet_legacy_cache: Optional[Fernet] = None


# 新版密钥派生方式：PBKDF2-HMAC-SHA256 + 盐 + 高迭代次数，安全性优于单次 SHA256
# 新数据统一用此实例加密，解密时也优先尝试它
def _get_fernet() -> Fernet:
    """
    获取Fernet加密实例，使用PBKDF2从JWT_SECRET_KEY派生密钥

    使用 PBKDF2-HMAC-SHA256 进行密钥派生，比单次 SHA256 更安全。
    结果会被缓存，避免重复执行昂贵的 KDF 计算。
    """
    # 命中缓存则直接返回
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache

    # 使用 PBKDF2 派生 32 字节密钥
    # 以 JWT_SECRET_KEY 为口令、MCP_ENCRYPTION_SALT 为盐派生，dklen=32 对齐 Fernet 需求
    key = hashlib.pbkdf2_hmac(
        "sha256",
        settings.JWT_SECRET_KEY.encode("utf-8"),
        _get_kdf_salt(),
        _KDF_ITERATIONS,
        dklen=32,
    )
    fernet_key = base64.urlsafe_b64encode(key)
    _fernet_cache = Fernet(fernet_key)
    return _fernet_cache


def encrypt_value(value: Any) -> Any:
    """
    加密敏感字段值

    Args:
        value: 要加密的值（通常是dict）

    Returns:
        加密后的值，如果是None则返回None

    Raises:
        RuntimeError: 加密失败时抛出异常
    """
    # None 原样返回
    if value is None:
        return None

    # 只对 dict 类型加密（env/headers 均为键值字典），其他类型原样返回
    if not isinstance(value, dict):
        return value

    if not value:  # 空字典
        return value

    try:
        fernet = _get_fernet()
        # 将dict序列化为JSON字符串
        import json

        json_str = json.dumps(value, ensure_ascii=False)
        # 加密
        # Fernet 加密得到含时间戳与 HMAC 的密文字节
        encrypted_bytes = fernet.encrypt(json_str.encode("utf-8"))
        # 添加加密标识并编码为字符串
        # 再 base64 成可存 JSON 的字符串，并包一层 ENCRYPTED_MARKER 便于识别
        return {ENCRYPTED_MARKER: base64.b64encode(encrypted_bytes).decode("utf-8")}
    except Exception as e:
        logger.error(f"加密失败: {e}")
        # 加密失败时抛出异常，避免敏感数据以明文形式存储
        raise RuntimeError(f"加密失败: {e}") from e


def decrypt_value(value: Any) -> Any:
    """
    解密敏感字段值

    支持两种格式：
    1. 加密格式: {"__encrypted__": "base64_encoded_data"}
    2. 明文格式: {"key": "value"}

    支持向后兼容：
    - 先尝试新密钥（PBKDF2）解密
    - 失败后尝试旧密钥（SHA256）解密

    Args:
        value: 要解密的值

    Returns:
        解密后的值，如果是None则返回None
    """
    # None 原样返回
    if value is None:
        return None

    # 非 dict、空 dict 无需解密，原样返回
    if not isinstance(value, dict):
        return value

    if not value:  # 空字典
        return value

    # 检查是否是加密格式
    # 含 ENCRYPTED_MARKER 才是加密数据，否则视为历史明文
    if ENCRYPTED_MARKER in value:
        encrypted_str = value.get(ENCRYPTED_MARKER)
        if not encrypted_str:
            return value

        # 还原 base64 得到密文字节
        encrypted_bytes = base64.b64decode(encrypted_str.encode("utf-8"))

        # 先尝试新密钥（PBKDF2）
        try:
            fernet = _get_fernet()
            decrypted_bytes = fernet.decrypt(encrypted_bytes)
            return json.loads(decrypted_bytes.decode("utf-8"))
        except Exception:
            # 新密钥失败，尝试旧密钥（SHA256）向后兼容
            # 兼容 PR #52 之前用旧密钥加密的历史数据
            try:
                fernet_legacy = _get_fernet_legacy()
                decrypted_bytes = fernet_legacy.decrypt(encrypted_bytes)
                logger.info("使用旧版密钥解密成功，建议重新保存配置以使用新密钥")
                return json.loads(decrypted_bytes.decode("utf-8"))
            except Exception as e:
                # 两种密钥都失败
                # 无法解密视为严重错误，抛出 DecryptionError 由上层处理
                logger.error(f"解密失败（尝试了新旧密钥）: {e}")
                raise DecryptionError(f"解密失败: {e}") from e

    # 明文格式（向后兼容）
    return value


def encrypt_server_secrets(server: dict[str, Any]) -> dict[str, Any]:
    """
    加密MCP服务器配置中的敏感字段

    Args:
        server: MCP服务器配置dict

    Returns:
        加密后的配置dict
    """
    # 拷贝一份，避免就地修改调用方传入的原始配置
    result = server.copy()

    # 加密env
    # env 为进程环境变量（可能含密钥/令牌），非空才加密
    if "env" in result and result["env"]:
        result["env"] = encrypt_value(result["env"])

    # 加密headers
    # headers 为远程 MCP 的请求头（可能含 Authorization），非空才加密
    if "headers" in result and result["headers"]:
        result["headers"] = encrypt_value(result["headers"])

    return result


def decrypt_server_secrets(server: dict[str, Any]) -> dict[str, Any]:
    """
    解密MCP服务器配置中的敏感字段

    Args:
        server: MCP服务器配置dict

    Returns:
        解密后的配置dict
    """
    # 拷贝一份，避免污染调用方数据
    result = server.copy()

    # 解密env
    # 与加密相反：读出时把密文还原为明文供实际使用
    if "env" in result:
        result["env"] = decrypt_value(result.get("env"))

    # 解密headers
    if "headers" in result:
        result["headers"] = decrypt_value(result.get("headers"))

    return result
