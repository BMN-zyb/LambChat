"""Validation helpers for chat routes."""

from src.kernel.schemas.agent import AgentRequest


# 团队 agent 请求校验：在真正分发（dispatch）之前，针对 team 类型 agent 的特殊约束对请求体做预处理，
# 清理那些在团队模式下不适用的字段。返回 None，直接就地修改传入的 _request。
def validate_team_agent_request(_agent_id: str, _request: AgentRequest) -> None:
    """Validate team-agent-specific request requirements before dispatch."""
    # 仅当目标 agent 为 "team"（团队聚合 agent）且请求指定了 team_id 时，才需要执行下面的清理逻辑
    if _agent_id == "team" and _request.team_id:
        # 团队模式下技能由团队内各子 agent 自行决定，故清空请求级的 enabled_skills
        _request.enabled_skills = None
        # 若请求携带了人格预设（persona），团队模式下同样不适用，需一并清空相关人格字段（下面三行）
        if _request.persona_preset_id:
            _request.persona_preset_id = None
            _request.persona_snapshot = None
            _request.persona_system_prompt = None
    return None
