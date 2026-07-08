"""
Environment Variable schemas for API request/response
"""

# 模块说明：定义"用户级环境变量"相关的请求/响应模型。
# 用户可以配置一批 KEY=VALUE 形式的环境变量（如第三方服务的 API Key），
# 供沙箱/工具执行时注入使用；变量值在数据库中是加密存储的，
# 本模块只负责 API 层的数据结构定义，不涉及加解密逻辑
# （加解密见 src/infra/mcp/encryption.py，存取见 src/infra/envvar/storage.py）。
# key 命名需符合 Shell 环境变量命名规范：以字母或下划线开头，只能包含字母、数字、下划线。
from typing import Optional

from pydantic import BaseModel, Field


class EnvVarCreate(BaseModel):
    """Schema for creating a new environment variable"""

    # 变量名：必须以字母或下划线开头，只能包含字母、数字、下划线，长度 1~128
    key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Environment variable name (must start with letter or underscore)",
    )
    # 变量值：长度 1~4096，创建时必须提供且不能为空
    value: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Environment variable value",
    )


# 更新环境变量的请求体：只能修改变量值，变量名（key）不可变更，
# 通常通过路径参数指定要更新哪个 key。
class EnvVarUpdate(BaseModel):
    """Schema for updating an environment variable"""

    value: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Environment variable value",
    )


# 单个环境变量的响应结构（value 通常已在返回前解密为明文）。
class EnvVarResponse(BaseModel):
    """Single environment variable response"""

    key: str
    value: str
    # 创建时间（字符串格式），历史数据或部分场景下可能缺失
    created_at: Optional[str] = None
    # 最近更新时间（字符串格式）
    updated_at: Optional[str] = None


# 环境变量列表的响应结构。
class EnvVarListResponse(BaseModel):
    """List of environment variables"""

    # 当前用户的全部环境变量
    variables: list[EnvVarResponse] = Field(default_factory=list)
    # 变量个数，等于 variables 长度，便于前端直接读取无需再计数
    count: int = 0


# 批量创建/更新环境变量的请求体（upsert 语义：已存在的 key 会被覆盖，不存在的会被新建）。
class EnvVarBulkUpdateRequest(BaseModel):
    """Bulk upsert environment variables"""

    # 待写入的键值对集合，key 命名规则同 EnvVarCreate.key
    variables: dict[str, str] = Field(
        ...,
        description="Key-value pairs to upsert (key must match ^[A-Za-z_][A-Za-z0-9_]*$)",
    )


# 批量更新操作的响应结构。
class EnvVarBulkUpdateResponse(BaseModel):
    """Response after bulk updating"""

    # 本次成功创建/更新的变量个数
    updated_count: int
    # 操作结果提示信息，可直接展示给用户
    message: str
