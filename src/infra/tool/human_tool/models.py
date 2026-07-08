"""
Human Tool 模型定义

支持多字段表单的 ask_human 工具的输入模型。
"""

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class FieldType(str, Enum):
    """表单字段类型枚举"""

    # 继承 str 使得该枚举既能参与序列化（json_schema/model_dump 里直接是字符串），
    # 也能直接用字符串比较，前端根据这个值决定渲染哪种输入控件

    TEXT = "text"
    """单行文本输入"""

    TEXTAREA = "textarea"
    """多行文本输入"""

    NUMBER = "number"
    """数字输入"""

    CHECKBOX = "checkbox"
    """复选框（布尔值）"""

    SELECT = "select"
    """下拉单选"""

    RADIO = "radio"
    """平铺单选"""

    MULTI_SELECT = "multi_select"
    """下拉多选"""

    def __str__(self) -> str:
        return self.value


class FormField(BaseModel):
    """表单字段定义"""

    # 每个 FormField 描述表单里的一个输入项；AskHumanInput.fields 是这些字段的列表，
    # 前端据此动态渲染出一张表单，人工填写后连同 name 一起作为响应结构的 key 返回
    name: str = Field(
        default="choice",
        description="字段名称，用于标识返回值中的字段",
    )
    label: str = Field(
        default="请选择",
        description="字段标签，显示给用户看的名称",
    )
    type: FieldType = Field(
        default=FieldType.TEXT,
        description="字段类型：text、textarea、number、checkbox、select、radio、multi_select",
    )
    placeholder: Optional[str] = Field(
        default=None,
        description="输入框占位符文本",
    )
    default: Optional[Any] = Field(
        default=None,
        description="字段默认值",
    )
    required: bool = Field(
        default=True,
        description="是否必填",
    )
    options: Optional[List[str]] = Field(
        default=None,
        description="选项列表（仅 select、radio 和 multi_select 类型使用）",
    )
    multiple: bool = Field(
        default=False,
        description="是否允许多选。设置 options 时可用它自动推断单选或多选",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "username",
                    "label": "用户名",
                    "type": "text",
                    "placeholder": "请输入用户名",
                    "required": True,
                },
                {
                    "name": "description",
                    "label": "描述",
                    "type": "textarea",
                    "placeholder": "请输入详细描述",
                    "required": False,
                },
                {
                    "name": "environment",
                    "label": "部署环境",
                    "type": "select",
                    "options": ["development", "staging", "production"],
                    "default": "development",
                    "required": True,
                },
            ]
        }
    }


class AskHumanInput(BaseModel):
    """ask_human 工具的输入参数（支持多字段表单）"""

    # 本模型同时承担两个角色：
    # 1) 作为 AskHumanTool 的 args_schema，供 LLM function calling 生成结构化参数；
    # 2) 提供 choices/multiple 简写路径——LLM 只需给出一组选项字符串，无需手写完整的
    #    fields 结构；具体如何把 choices 展开成单个 FormField 由 tool.py 里的实现完成
    message: str = Field(
        ...,
        description="向用户展示的提示消息，说明需要用户提供什么信息",
    )
    fields: List[FormField] = Field(
        default_factory=list,
        description="表单字段列表，定义需要用户填写的各个字段",
    )
    choices: Optional[List[str]] = Field(
        default=None,
        description="简写选项列表。设置后无需手写 fields，会自动生成一个 choice 字段",
    )
    multiple: bool = Field(
        default=False,
        description="配合 choices 使用：false 为单选，true 为多选",
    )
    timeout: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="等待响应的超时时间（秒），范围 10-3600",
    )
    allow_other: bool = Field(
        default=True,
        description="是否额外提供一个「其他意见」文本输入框，让用户可以填写选项中没有的建议，返回值中会包含 _other 字段",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "message": "请填写数据库连接信息",
                    "fields": [
                        {
                            "name": "host",
                            "label": "主机地址",
                            "type": "text",
                            "required": True,
                        },
                        {
                            "name": "port",
                            "label": "端口",
                            "type": "number",
                            "default": 5432,
                            "required": True,
                        },
                        {
                            "name": "password",
                            "label": "密码",
                            "type": "text",
                            "required": True,
                        },
                    ],
                    "timeout": 300,
                }
            ]
        }
    }
