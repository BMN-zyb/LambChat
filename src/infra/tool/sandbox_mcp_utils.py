"""Shared utilities for sandbox MCP operations.

Provides common helpers used by both the LLM tools (sandbox_mcp_tool.py)
and the sandbox session manager (session_manager.py) to avoid code
duplication.
"""
# 中文说明：本模块被 sandbox_mcp_tool.py（LLM 工具实现）与
# session_manager.py（沙箱会话/重建时自动恢复 MCP 服务器）共同依赖，
# 抽出来避免两处重复实现同一段"拼接 mcporter --env 参数"的逻辑。

import shlex


async def build_env_flags(user_id: str, env_key_names: list[str]) -> str:
    """Build --env KEY=VALUE flags for mcporter commands.

    Resolves actual values from the user's encrypted env var storage.

    Args:
        user_id: User ID to look up env vars for.
        env_key_names: List of env var key names to include.

    Returns:
        A string of "--env KEY=VALUE" flags, or empty string if no keys.
    """
    # 没有需要注入的环境变量名时直接短路返回空字符串
    if not env_key_names:
        return ""
    # 延迟导入，避免模块级循环依赖；EnvVarStorage 负责加密存储/解密读取用户环境变量
    from src.infra.envvar.storage import EnvVarStorage

    storage = EnvVarStorage()
    # 一次性取出该用户所有环境变量的解密结果，再按需挑选本次要用到的 key
    env_vars = await storage.get_decrypted_vars(user_id)
    parts = []
    for key in env_key_names:
        # 找不到对应 key 时用空字符串兜底，而不是抛异常中断整个命令拼接
        val = env_vars.get(key, "")
        # 使用 shlex.quote 对 key、value 做 shell 转义，防止用户自定义的变量值
        # 中包含空格、引号等特殊字符时破坏 mcporter 命令行结构，甚至引发命令注入
        parts.append(f" --env {shlex.quote(key)}={shlex.quote(val)}")
    return "".join(parts)
