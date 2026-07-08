// 【当前 agent 选择的解析工具】在可用 agent 列表变化时，决定应选中的 agent，
// 保证已选 agent 失效时能优雅回退到默认/首个可用 agent（含「团队」特殊 ID 的处理）。

import type { AgentInfo } from "../../types";

// 「团队」这一特殊 agent 的 ID（表示多 agent 团队模式，而非某个具体 agent）
const TEAM_AGENT_ID = "team";

// 从可用 agent 中解析出应选中的 agent ID：
// 优先保留当前选中项（若仍可用），其次用首选默认项（若可用），再退化为列表首个，最后空串。
export function resolveAvailableAgentId(
  currentAgentId: string,
  preferredDefaultAgentId: string | undefined,
  agents: AgentInfo[],
): string {
  const availableIds = new Set(agents.map((agent) => agent.id));

  if (currentAgentId && availableIds.has(currentAgentId)) {
    return currentAgentId;
  }

  if (preferredDefaultAgentId && availableIds.has(preferredDefaultAgentId)) {
    return preferredDefaultAgentId;
  }

  return agents[0]?.id || "";
}

// 解析用于「人设」场景的 agent ID：排除「团队」这一特殊项——
// 当前若已是具体 agent 则沿用解析结果；若当前为团队或为空，则在非团队 agent 中选取。
export function resolvePersonaAgentId(
  currentAgentId: string,
  preferredDefaultAgentId: string | undefined,
  agents: AgentInfo[],
): string {
  if (currentAgentId && currentAgentId !== TEAM_AGENT_ID) {
    return resolveAvailableAgentId(
      currentAgentId,
      preferredDefaultAgentId,
      agents,
    );
  }

  const nonTeamAgents = agents.filter((agent) => agent.id !== TEAM_AGENT_ID);
  return resolveAvailableAgentId("", preferredDefaultAgentId, nonTeamAgents);
}
