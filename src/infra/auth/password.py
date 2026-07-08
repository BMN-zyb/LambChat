"""
密码处理

提供密码哈希和验证功能。
"""

import bcrypt


def _truncate_password(password: str, max_bytes: int = 72) -> bytes:
    """
    安全截断密码到指定字节数，确保不在多字节字符中间截断

    Args:
        password: 明文密码
        max_bytes: 最大字节数（bcrypt 限制为 72 字节）

    Returns:
        截断后的密码字节
    """
    # 以 UTF-8 编码为字节；bcrypt 只接受字节且最多处理 72 字节
    password_bytes = password.encode("utf-8")
    # 未超限时直接返回，无需截断
    if len(password_bytes) <= max_bytes:
        return password_bytes

    # 安全截断：从 max_bytes 位置向前查找有效的 UTF-8 边界
    # UTF-8 多字节字符的第一个字节最高位为 11xxxxxx（不是 10xxxxxx）
    # 若正好截在多字节字符中间，会破坏字符导致乱码，因此向前回退到字符边界
    truncate_pos = max_bytes
    while truncate_pos > 0:
        byte = password_bytes[truncate_pos - 1]
        # 检查是否是 UTF-8 连续字节（10xxxxxx）
        # 是续接字节则说明仍在字符内部，继续向前回退
        if (byte & 0xC0) != 0x80:
            break
        truncate_pos -= 1

    return password_bytes[:truncate_pos]


def hash_password(password: str) -> str:
    """
    生成密码哈希

    Args:
        password: 明文密码

    Returns:
        哈希后的密码
    """
    # bcrypt has a 72 byte limit, truncate safely if necessary
    # 先按字节安全截断，再生成随机盐，最后计算带盐哈希
    password_bytes = _truncate_password(password, 72)
    # gensalt 每次生成不同的随机盐，因此同一密码每次哈希结果都不同
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    # 存储时统一用字符串形式（盐已内嵌在哈希结果中）
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码

    Args:
        plain_password: 明文密码
        hashed_password: 哈希密码

    Returns:
        是否匹配
    """
    # Truncate safely to match hashing behavior
    # 校验时必须复用与哈希时相同的截断规则，否则超长密码会比对失败
    password_bytes = _truncate_password(plain_password, 72)
    hashed_bytes = hashed_password.encode("utf-8")
    # checkpw 会从哈希串中解析出盐并重新计算比对，返回是否匹配
    return bcrypt.checkpw(password_bytes, hashed_bytes)
