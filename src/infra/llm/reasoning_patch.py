"""Monkey-patch langchain-openai to preserve provider reasoning content safely.

Some OpenAI-compatible providers return ``reasoning_content`` in streaming
deltas, but ``langchain-openai`` does not preserve this field by default.
These patches bridge the gap by:

1. **Inbound** — copying ``reasoning_content`` from the raw delta dict into
   ``AIMessageChunk.additional_kwargs`` so that ``langchain_core`` can surface
   it via ``content_blocks``.
2. **Outbound** — only re-sending ``reasoning_content`` when continuing a
   DeepSeek tool-call turn, which matches the provider contract without leaking
   the field into ordinary assistant turns or other OpenAI-compatible backends.
"""


# 判断消息是否来自 DeepSeek：读 response_metadata 里的 model_name/model，小写后看
# 是否以 "deepseek" 开头。用于把 reasoning_content 回传限定在 DeepSeek 上。
def _is_deepseek_message(message) -> bool:
    response_metadata = getattr(message, "response_metadata", {})
    if not isinstance(response_metadata, dict):
        return False

    model_name = str(
        response_metadata.get("model_name") or response_metadata.get("model") or ""
    ).lower()
    return model_name.startswith("deepseek")


# 判断这条 AI 消息是否处于"工具调用续写"回合：带 tool_calls / invalid_tool_calls，
# 或 additional_kwargs 里有 tool_calls / function_call。
def _has_tool_continuation(message) -> bool:
    if getattr(message, "tool_calls", None) or getattr(message, "invalid_tool_calls", None):
        return True

    additional_kwargs = getattr(message, "additional_kwargs", {})
    return bool(additional_kwargs.get("tool_calls") or additional_kwargs.get("function_call"))


def apply_reasoning_patches() -> None:
    """给 langchain-openai 打补丁，安全保留各 provider 的推理内容（reasoning_content）。

    详见模块 docstring：入站把流式 delta 里的 reasoning_content 拷进 additional_kwargs；
    出站仅在"DeepSeek 工具调用续写"时才回传该字段，避免污染普通回合或其他后端。
    补丁幂等（靠模块级标记 _lambchat_reasoning_patch_applied）。
    """
    import langchain_openai.chat_models.base as _base

    # 幂等保护：已打过补丁则直接返回
    if getattr(_base, "_lambchat_reasoning_patch_applied", False):
        return

    _orig_convert_delta = _base._convert_delta_to_message_chunk
    _orig_convert_msg = _base._convert_message_to_dict

    def _patched_convert_delta(_dict, default_class):
        # 入站：把原始 delta dict 里的 reasoning_content 塞进 chunk 的 additional_kwargs，
        # 使 langchain_core 能通过 content_blocks 暴露推理内容。
        result = _orig_convert_delta(_dict, default_class)
        rc = _dict.get("reasoning_content") if isinstance(_dict, dict) else None
        if rc:
            result.additional_kwargs["reasoning_content"] = rc
        return result

    def _patched_convert_msg(message, api="chat/completions"):
        from langchain_core.messages import AIMessage

        # 出站：仅当是 DeepSeek 消息且处于工具调用续写回合时，才把 reasoning_content
        # 写回请求体（符合 DeepSeek 契约），普通回合/其他后端不携带以免报错。
        result = _orig_convert_msg(message, api=api)
        if isinstance(message, AIMessage):
            rc = message.additional_kwargs.get("reasoning_content")
            if rc and _is_deepseek_message(message) and _has_tool_continuation(message):
                result["reasoning_content"] = rc
        return result

    # 替换库内部的两个转换函数，并打上幂等标记
    _base._convert_delta_to_message_chunk = _patched_convert_delta
    _base._convert_message_to_dict = _patched_convert_msg
    setattr(_base, "_lambchat_reasoning_patch_applied", True)  # type: ignore[attr-defined]
