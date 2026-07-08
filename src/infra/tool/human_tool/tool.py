"""
Human Tool 实现

支持多字段表单的 ask_human 工具的 LangChain 工具实现。
"""

import json
from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import BaseTool

from src.api.routes.human import create_approval, wait_for_response
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.human_tool.models import AskHumanInput, FieldType, FormField

logger = get_logger(__name__)


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 统一走线程池执行 json.dumps，避免（理论上）较大的响应结构在序列化时阻塞事件循环
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


class AskHumanTool(BaseTool):
    """
    请求人工输入的工具（支持多字段表单）

    当 Agent 遇到不确定的情况时，可以调用此工具请求人工输入。
    工具会阻塞直到用户响应或超时。

    支持多种字段类型：
    - text: 单行文本输入
    - textarea: 多行文本输入
    - number: 数字输入
    - checkbox: 复选框（布尔值）
    - select: 下拉单选
    - multi_select: 下拉多选

    使用场景：
    - 需要用户确认敏感操作
    - 需要用户提供额外信息（如表单）
    - 遇到多种可能的方案需要用户选择
    - 不确定用户意图时请求澄清
    """

    # name/description 会被 LangChain 直接拼进 LLM 的工具调用 schema，
    # 因此下面这段中文说明既是给人看的文档，也是模型据以判断"何时调用/如何传参"的依据，
    # 内容需要尽量详尽、贴合实际参数结构
    name: str = "ask_human"
    description: str = """向用户提问并等待响应，支持多字段表单。

使用场景：
- 需要用户确认敏感操作（如删除文件、执行危险命令）
- 需要用户提供额外信息才能继续
- 需要用户填写表单（如数据库连接信息、配置参数等）
- 遇到多种可能的方案需要用户选择
- 不确定用户意图时请求澄清

参数：
- message: 向用户展示的提示消息，说明需要用户提供什么信息
- choices: 简写选项列表；设置后会自动生成单选字段
- multiple: 配合 choices 使用，true 时生成多选字段
- fields: 表单字段列表，每个字段包含：
  - name: 字段名称（用于标识返回值）
  - label: 显示给用户的标签
  - type: 字段类型 - text（单行文本）、textarea（多行文本）、number（数字）、checkbox（复选框）、select（下拉单选）、radio（平铺单选）、multi_select（多选）
  - placeholder: 输入框占位符文本（可选）
  - default: 默认值（可选）
  - required: 是否必填（默认 true）
  - options: 选项列表（仅 select 和 multi_select 类型使用）
- timeout: 等待响应的超时时间（秒），范围 10-3600，默认 300
- allow_other: 是否额外提供「其他意见」文本输入框（默认 false），启用后返回值中会包含 other 字段

返回值：
- 成功时返回 JSON 字符串，包含各字段的值
- 超时时返回超时消息
- 用户拒绝时返回拒绝消息

示例：
1. 简单确认：
   ask_human(message="确定要删除这个文件吗？", fields=[{"name": "confirm", "label": "确认", "type": "checkbox", "default": false}])

2. 获取文本输入：
   ask_human(message="请输入数据库连接信息", fields=[
     {"name": "host", "label": "主机地址", "type": "text", "required": true},
     {"name": "port", "label": "端口", "type": "number", "default": 5432},
     {"name": "password", "label": "密码", "type": "text", "required": true}
   ])

3. 多选一：
   ask_human(message="选择部署环境", fields=[
     {"name": "env", "label": "环境", "type": "select", "options": ["development", "staging", "production"], "default": "development"}
   ])

4. 多行文本：
   ask_human(message="请描述问题详情", fields=[
     {"name": "description", "label": "描述", "type": "textarea", "placeholder": "请详细描述您遇到的问题..."}
   ])
"""
    args_schema: Type[AskHumanInput] = AskHumanInput
    return_direct: bool = False

    # 从 context 注入（可选，优先使用 TraceContext）
    session_id: str = ""

    def _run(
        self,
        message: str,
        fields: Optional[List[FormField]] = None,
        timeout: int = 300,
    ) -> str:
        """同步执行（不支持，返回错误）"""
        # 本工具的核心是"阻塞等待人工响应"，天然需要 await，因此只提供异步实现；
        # 同步入口保留仅为满足 BaseTool 的抽象方法要求，调用即报错提示改用 ainvoke
        return "Error: ask_human only supports async execution. Use ainvoke instead."

    async def _arun(
        self,
        message: str,
        fields: Optional[List[FormField]] = None,
        choices: Optional[List[str]] = None,
        multiple: bool = False,
        timeout: int = 300,
        allow_other: bool = False,
    ) -> str:
        """
        异步执行：创建审批请求并等待响应

        Args:
            message: 向用户展示的提示消息
            fields: 表单字段列表
            timeout: 超时时间（秒），范围 10-3600

        Returns:
            JSON 字符串，包含状态和字段值或错误消息
        """
        # 设置默认值
        # choices/multiple 是免手写 fields 的简写路径，二者互斥：
        # 提供了 fields 就忽略 choices（见 _expand_short_choices 实现）
        fields = self._expand_short_choices(fields, choices, multiple)

        # 解析字段并设置默认值
        # 字段可能来自 LLM 生成的多种不规范形态（dict/JSON 字符串/字段别名等），
        # 丢进线程池里做规整解析，避免这部分兼容性处理占用事件循环
        parsed_fields = await run_blocking_io(self._parse_fields, fields)

        # 如果启用了 allow_other，追加一个独立的「其他意见」文本字段
        # 使用 _ 前缀命名空间，避免与用户字段冲突
        if allow_other:
            parsed_fields.append(
                FormField(
                    name="_other",
                    label="其他意见",
                    type=FieldType.TEXTAREA,
                    placeholder="除上述选项外，您还有其他想法或建议吗？",
                    required=False,
                )
            )

        # 获取当前请求上下文
        from src.infra.logging.context import TraceContext

        # session_id 优先使用实例属性（get_human_tool 显式传入的），
        # 缺省时回退到当前请求的 TraceContext，两者都取不到就无法推送实时事件
        ctx = TraceContext.get_request_context()
        session_id = self.session_id or ctx.session_id
        run_id = ctx.run_id
        user_id = ctx.user_id

        # 构建审批类型和字段列表
        # approval_type 固定为 "form"：即便只有一个字段（如简单确认），
        # 也统一走表单审批流程，避免维护"单字段/多字段"两套不同的响应结构
        approval_type = "form"

        # 将字段序列化为 dict 列表
        field_dicts = [f.model_dump() for f in parsed_fields] if parsed_fields else []

        # 创建审批请求
        # create_approval 只是把这次请求记录下来（持久化 + 生成 approval_id），
        # 真正通知用户靠下面的 SSE 事件；wait_for_response 才会真正阻塞等待
        approval = await create_approval(
            message=message,
            approval_type=approval_type,
            fields=field_dicts,
            session_id=session_id or None,
            user_id=user_id,
        )

        # 通过 SSE 流发送 approval_required 事件
        await self._send_approval_event(
            approval, session_id, run_id, parsed_fields, timeout=timeout
        )

        # 等待用户响应
        # 核心阻塞点：这里会挂起当前协程，直到用户在前端提交/拒绝，
        # 或者等待时间超过 timeout 秒后由 wait_for_response 自行返回 None（超时）
        response = await wait_for_response(approval.id, timeout=timeout)

        if response is None:
            # 超时：构建超时响应
            # 超时场景下没有用户输入，退化为返回各字段的默认值，
            # 保证下游解析响应结构时始终能拿到与"成功"一致的 values 形状
            result = {
                "status": "timeout",
                "message": f"等待用户响应超时（{timeout}秒）",
                "values": self._get_default_values(parsed_fields),
            }
            return await _json_dumps_result(result)

        if not response.approved:
            # 用户拒绝
            result = {
                "status": "rejected",
                "message": "用户拒绝了此请求",
                "values": {},
            }
            return await _json_dumps_result(result)

        # 成功：解析用户响应
        # response.response 现在是 dict 类型
        if response.response and isinstance(response.response, dict):
            values = response.response
        else:
            # 响应存在但不是预期的 dict 结构（异常兜底），仍然退化为默认值
            values = self._get_default_values(parsed_fields)

        result = {
            "status": "success",
            "message": "用户已响应",
            "values": values,
        }
        return await _json_dumps_result(result)

    def _expand_short_choices(
        self,
        fields: Optional[List[FormField]],
        choices: Optional[List[str]],
        multiple: bool,
    ) -> list[Any]:
        # fields 优先级更高：只有在完全没有提供 fields 时才尝试用 choices 简写展开
        if fields:
            return fields
        if not choices:
            return []
        # 把一组选项字符串展开成单个名为 "choice" 的字段：
        # multiple=True 时用 multi_select（下拉多选），否则用 radio（平铺单选）
        return [
            {
                "name": "choice",
                "label": "请选择",
                "type": "multi_select" if multiple else "radio",
                "options": choices,
                "multiple": multiple,
                "required": True,
            }
        ]

    def _parse_fields(self, fields: Any) -> List[FormField]:
        """
        解析字段列表并设置默认值

        Args:
            fields: 字段列表（可能是 FormField 对象、字典或 JSON 字符串）

        Returns:
            解析后的 FormField 列表
        """
        # 处理 fields 是 JSON 字符串的情况（LLM 有时会这样传参）
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse fields as JSON: {fields[:100]}...")
                fields = []

        # 确保 fields 是列表
        if not isinstance(fields, list):
            logger.warning(f"fields is not a list: {type(fields)}")
            fields = []

        parsed = []
        for field in fields:
            if isinstance(field, FormField):
                # 兼容历史数据/调用方遗留的写法：给了 options 却仍标记为 TEXT 类型，
                # 视为"忘记设置正确类型"，按 multiple 自动纠正为 multi_select 或 radio
                if field.options and field.type == FieldType.TEXT:
                    field = field.model_copy(
                        update={
                            "type": FieldType.MULTI_SELECT if field.multiple else FieldType.RADIO,
                            "multiple": field.multiple,
                        }
                    )
                parsed.append(field)
            elif isinstance(field, dict):
                # 从字典创建 FormField。带 options 的字段可省略 type。
                field_multiple = bool(field.get("multiple", False))
                field_type = field.get("type")
                if not field_type:
                    # 未显式指定类型时，按是否有 options/是否多选推断出合理的默认类型
                    field_type = (
                        "multi_select"
                        if field.get("options") and field_multiple
                        else "radio"
                        if field.get("options")
                        else "text"
                    )
                if isinstance(field_type, str):
                    # LLM 可能用一些近义词表达类型，这里做一层别名归一化，
                    # 提升对不规范/自然语言化输出的容错能力
                    type_aliases = {
                        "choice": "radio",
                        "single": "radio",
                        "single_select": "radio",
                        "multiple_choice": "multi_select",
                        "checkbox_group": "multi_select",
                    }
                    field_type = FieldType(type_aliases.get(field_type, field_type))

                # 兼容 LLM 可能使用 "id" 而不是 "name" 的情况
                field_name = field.get("name") or field.get("id") or "choice"

                form_field = FormField(
                    name=field_name,
                    label=field.get("label")
                    or field.get("title")
                    or ("请选择" if field.get("options") else field_name),
                    type=field_type,
                    placeholder=field.get("placeholder"),
                    default=field.get("default", self._get_type_default(field_type)),
                    required=field.get("required", True),
                    options=field.get("options"),
                    multiple=field_multiple or field_type == FieldType.MULTI_SELECT,
                )
                parsed.append(form_field)
            else:
                logger.warning(f"Unknown field type: {type(field)}")

        # 如果没有字段，添加一个默认的文本字段
        # 保证 ask_human 永远至少有一个可填写的字段，避免出现"空表单"的边界情况
        if not parsed:
            parsed.append(
                FormField(
                    name="response",
                    label="响应",
                    type=FieldType.TEXT,
                    required=True,
                )
            )

        return parsed

    def _get_type_default(self, field_type: FieldType) -> Any:
        """
        获取字段类型的默认值

        Args:
            field_type: 字段类型

        Returns:
            该类型的默认值
        """
        # select/radio 类型语义上是"用户必须选择一项"，没有天然的空值表示，
        # 因此默认值用 None（而不是空字符串）表示"尚未选择"
        defaults = {
            FieldType.TEXT: "",
            FieldType.TEXTAREA: "",
            FieldType.NUMBER: 0,
            FieldType.CHECKBOX: False,
            FieldType.SELECT: None,
            FieldType.RADIO: None,
            FieldType.MULTI_SELECT: [],
        }
        return defaults.get(field_type, None)

    def _get_default_values(self, fields: List[FormField]) -> Dict[str, Any]:
        """
        获取所有字段的默认值

        Args:
            fields: 字段列表

        Returns:
            字段名到默认值的映射
        """
        # 用于超时场景：字段自身声明的 default 优先，没有声明则退回该类型的通用默认值
        values = {}
        for field in fields:
            if field.default is not None:
                values[field.name] = field.default
            else:
                values[field.name] = self._get_type_default(field.type)
        return values

    async def _send_approval_event(
        self,
        approval,
        session_id: Optional[str],
        run_id: Optional[str],
        fields: List[FormField],
        timeout: int = 300,
    ) -> None:
        """
        发送 approval_required 事件到 SSE 流

        Args:
            approval: 审批对象
            session_id: 会话 ID
            run_id: 运行 ID
            fields: 表单字段列表
        """
        logger.info(
            f"[AskHuman] _send_approval_event called: session_id={session_id}, "
            f"run_id={run_id}, approval_id={approval.id}"
        )

        # 没有 session_id 就没有可推送的 SSE 通道（例如非会话场景下的后台调用），
        # 此时用户不会在界面上看到审批弹窗，只能依赖轮询或其他方式获知
        if not session_id:
            logger.warning("[AskHuman] Cannot send approval event: no session_id")
            return

        try:
            from src.infra.session.dual_writer import get_dual_writer

            dual_writer = get_dual_writer()
            logger.info(
                f"[AskHuman] Writing approval_required event to Redis: "
                f"session={session_id}, run_id={run_id}"
            )

            # 构建事件数据
            event_data = {
                "id": approval.id,
                "message": approval.message,
                "type": approval.type,
                "fields": [f.model_dump() for f in fields],
                "timeout": timeout,
            }

            await dual_writer.write_event(
                session_id=session_id,
                event_type="approval_required",
                data=event_data,
                run_id=run_id,
            )
            logger.info(
                f"[AskHuman] Sent approval_required event: approval_id={approval.id}, "
                f"session={session_id}, run_id={run_id}"
            )
        except Exception as e:
            # 事件推送失败不影响审批本身已创建的事实，只是前端可能收不到实时提示；
            # 用户仍可能通过刷新或其他渠道看到待处理的审批
            logger.error(f"[AskHuman] Failed to send approval event: {e}", exc_info=True)


def get_human_tool(session_id: str = "") -> AskHumanTool:
    """
    获取 ask_human 工具实例

    Args:
        session_id: 会话 ID，用于关联审批请求（可选，优先使用 TraceContext）

    Returns:
        配置好的 AskHumanTool 实例
    """
    # 每次调用都返回一个新实例（而非单例）：session_id 与具体会话绑定，
    # 不同会话/请求需要各自独立的工具实例来携带各自的 session_id
    return AskHumanTool(session_id=session_id)
