"""
Skill storage constants
"""

# MongoDB collection names
# 技能子系统三张集合：用户私有文件、市场元数据、市场文件
SKILL_FILES_COLLECTION = "skill_files"  # 用户文件
SKILL_MARKETPLACE_COLLECTION = "skill_marketplace"  # 商城 Skill 元数据
SKILL_MARKETPLACE_FILES_COLLECTION = "skill_marketplace_files"  # 商城 Skill 文件

# Redis cache TTL (seconds), default 30 minutes
# 用户技能列表与 MCP 工具元数据的缓存过期时间（秒），默认 30 分钟
SKILLS_CACHE_TTL = 1800
MCP_TOOLS_METADATA_CACHE_TTL = 1800

# Redis cache key prefixes
# 缓存 key 前缀，实际 key 通常再拼接 user_id 等标识
SKILLS_CACHE_KEY_PREFIX = "user_skills:"
MCP_TOOLS_METADATA_KEY_PREFIX = "mcp_tools_metadata:"
