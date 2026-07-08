/**
 * Session API - 会话管理
 *
 * 会话领域客户端：增删改查、事件/运行记录、生成标题、收藏/已读、消息分叉与检查点等。
 * 所有请求走 authFetch（自动鉴权+刷新）。部分 URL 抽成独立 build* 函数便于复用与测试。
 */

import type {
  SessionEventsResponse,
  RunSummary,
  MessageAttachment,
} from "../../types";
import { API_BASE } from "./config";
import { authFetch } from "./fetch";

// Backend Session type (matches backend Session schema)
export interface BackendSession {
  id: string;
  user_id?: string;
  agent_id: string;
  created_at: string;
  updated_at: string;
  is_active: boolean;
  name?: string;
  metadata: Record<string, unknown>;
  unread_count?: number;
}

// Session list response type
export interface SessionListResponse {
  sessions: BackendSession[];
  total: number;
  skip: number;
  limit: number;
  has_more: boolean;
}

export interface SessionRunsQuery {
  limit?: number;
  trace_id?: string;
}

export interface RunGoalSpec {
  objective: string;
  rubric?: string;
  max_iterations?: number;
}

// 以下三个 URL 构造函数对应「消息分叉 / 从消息建检查点 / 从检查点分叉」，
// 用于对话树的分支与回溯能力。
export function buildMessageForkUrl(
  sessionId: string,
  messageId: string,
): string {
  return `${API_BASE}/api/sessions/${sessionId}/messages/${messageId}/fork`;
}

export function buildMessageCheckpointUrl(
  sessionId: string,
  messageId: string,
): string {
  return `${API_BASE}/api/sessions/${sessionId}/messages/${messageId}/checkpoints`;
}

export function buildCheckpointForkUrl(
  sessionId: string,
  checkpointId: string,
): string {
  return `${API_BASE}/api/sessions/${sessionId}/checkpoints/${checkpointId}/fork`;
}

// 读取浏览器时区（如 "Asia/Shanghai"）；无法解析时返回 undefined。
// 随聊天请求上报，供后端做时间相关的本地化处理。
function getBrowserTimezone(): string | undefined {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return typeof timezone === "string" && timezone.trim() ? timezone : undefined;
}

// 组装「提交聊天」的请求体：把前端各种可选参数（附件、启用/禁用的技能、禁用的 MCP
// 工具、人设预设、项目/团队、目标 goal 等）映射为后端字段名(snake_case)。
// 仅在有值时才写入可选字段，避免给后端传一堆 undefined/空值。
export function buildSubmitChatBody({
  message,
  sessionId,
  agentOptions,
  attachments,
  projectId,
  disabledSkills,
  enabledSkills,
  personaPresetId,
  disabledMcpTools,
  userTimezone,
  teamId,
  goal,
}: {
  message: string;
  sessionId?: string;
  agentOptions?: Record<string, boolean | string | number>;
  attachments?: MessageAttachment[];
  projectId?: string;
  disabledSkills?: string[];
  enabledSkills?: string[];
  personaPresetId?: string | null;
  disabledMcpTools?: string[];
  userTimezone?: string;
  teamId?: string | null;
  goal?: RunGoalSpec | null;
}): Record<string, unknown> {
  const body: Record<string, unknown> = {
    message,
    session_id: sessionId,
    agent_options: agentOptions,
    attachments,
    disabled_skills: disabledSkills,
    enabled_skills: enabledSkills,
    persona_preset_id: personaPresetId || undefined,
    disabled_mcp_tools: disabledMcpTools,
  };

  if (userTimezone) {
    body.user_timezone = userTimezone;
  }
  if (projectId) {
    body.project_id = projectId;
  }
  if (teamId) {
    body.team_id = teamId;
  }
  if (goal) {
    body.goal = goal;
  }
  return body;
}

export function buildSessionRunsUrl(
  sessionId: string,
  options?: SessionRunsQuery,
): string {
  const searchParams = new URLSearchParams();
  if (options?.limit) {
    searchParams.set("limit", String(options.limit));
  }
  if (options?.trace_id) {
    searchParams.set("trace_id", options.trace_id);
  }

  const queryString = searchParams.toString();
  return `${API_BASE}/api/sessions/${sessionId}/runs${
    queryString ? `?${queryString}` : ""
  }`;
}

export const sessionApi = {
  /**
   * List all sessions with pagination
   */
  async list(params?: {
    status?: string;
    limit?: number;
    skip?: number;
    project_id?: string;
    search?: string;
    favorites_only?: boolean;
  }): Promise<SessionListResponse | BackendSession[]> {
    const searchParams = new URLSearchParams();
    if (params?.status) searchParams.set("status", params.status);
    if (params?.limit) searchParams.set("limit", params.limit.toString());
    if (params?.skip) searchParams.set("skip", params.skip.toString());
    if (params?.project_id) searchParams.set("project_id", params.project_id);
    if (params?.search) searchParams.set("search", params.search);
    if (params?.favorites_only) searchParams.set("favorites_only", "true");

    const url = `${API_BASE}/api/sessions${
      searchParams.toString() ? `?${searchParams}` : ""
    }`;
    return authFetch<SessionListResponse | BackendSession[]>(url);
  },

  /**
   * Get a session
   * 获取单个会话；后端 404 时吞掉并返回 null（会话可能已删除），其它错误继续抛出。
   */
  async get(sessionId: string): Promise<BackendSession | null> {
    try {
      return await authFetch<BackendSession>(
        `${API_BASE}/api/sessions/${sessionId}`,
      );
    } catch (error) {
      if ((error as Error).message.includes("404")) {
        return null;
      }
      throw error;
    }
  },

  /**
   * Get all session events
   */
  async getEvents(
    sessionId: string,
    options?: {
      event_types?: string[];
      run_id?: string;
      exclude_run_id?: string;
    },
  ): Promise<SessionEventsResponse & { run_id?: string }> {
    const searchParams = new URLSearchParams();
    if (options?.event_types && options.event_types.length > 0) {
      searchParams.set("event_types", options.event_types.join(","));
    }
    if (options?.run_id) {
      searchParams.set("run_id", options.run_id);
    }
    if (options?.exclude_run_id) {
      searchParams.set("exclude_run_id", options.exclude_run_id);
    }

    const url = `${API_BASE}/api/sessions/${sessionId}/events${
      searchParams.toString() ? `?${searchParams}` : ""
    }`;
    return authFetch<SessionEventsResponse & { run_id?: string }>(url);
  },

  /**
   * Get all runs for a session
   */
  async getRuns(
    sessionId: string,
    options?: SessionRunsQuery,
  ): Promise<{ session_id: string; runs: RunSummary[]; count: number }> {
    return authFetch(buildSessionRunsUrl(sessionId, options));
  },

  /**
   * Delete a session
   */
  async delete(sessionId: string) {
    return authFetch(`${API_BASE}/api/sessions/${sessionId}`, {
      method: "DELETE",
    });
  },

  /**
   * Update session status
   */
  async updateStatus(sessionId: string, status: "active" | "archived") {
    return authFetch(
      `${API_BASE}/api/sessions/${sessionId}/status?status=${status}`,
      {
        method: "PATCH",
      },
    );
  },

  /**
   * Clear messages for a session
   */
  async clearMessages(sessionId: string) {
    return authFetch(`${API_BASE}/api/sessions/${sessionId}/clear-messages`, {
      method: "POST",
    });
  },

  /**
   * Generate title for session using LLM
   */
  async generateTitle(
    sessionId: string,
    message: string,
    lang: string = "en",
  ): Promise<{ title: string; session_id: string }> {
    return authFetch(
      `${API_BASE}/api/sessions/${sessionId}/generate-title?message=${encodeURIComponent(
        message,
      )}&lang=${encodeURIComponent(lang)}`,
      {
        method: "POST",
      },
    );
  },

  /**
   * Get session task status
   */
  async getStatus(
    sessionId: string,
    runId?: string,
  ): Promise<{
    session_id: string;
    run_id?: string;
    status: string;
    error?: string;
  }> {
    const params = runId ? `?run_id=${runId}` : "";
    return authFetch(
      `${API_BASE}/api/chat/sessions/${sessionId}/status${params}`,
    );
  },

  /**
   * Cancel running task for a session
   */
  async cancel(sessionId: string): Promise<{
    success: boolean;
    message: string;
  }> {
    return authFetch(`${API_BASE}/api/chat/sessions/${sessionId}/cancel`, {
      method: "POST",
    });
  },

  /**
   * Submit a chat message (returns immediately)
   * 提交一条聊天消息：POST /api/chat/stream。注意这里是「立即返回」run_id/trace_id 等，
   * 真正的流式增量输出由另外的 SSE/WS 连接消费；自动附带浏览器时区。
   */
  async submitChat(
    agentId: string,
    message: string,
    sessionId?: string,
    agentOptions?: Record<string, boolean | string | number>,
    attachments?: MessageAttachment[],
    projectId?: string,
    disabledSkills?: string[],
    disabledMcpTools?: string[],
    personaPresetId?: string | null,
    enabledSkills?: string[],
    teamId?: string | null,
    goal?: RunGoalSpec | null,
  ): Promise<{
    session_id: string;
    run_id: string;
    trace_id: string;
    status: string;
  }> {
    const body = buildSubmitChatBody({
      message,
      sessionId,
      agentOptions,
      attachments,
      projectId,
      disabledSkills,
      enabledSkills,
      personaPresetId,
      disabledMcpTools,
      userTimezone: getBrowserTimezone(),
      teamId,
      goal,
    });
    return authFetch(`${API_BASE}/api/chat/stream?agent_id=${agentId}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  /**
   * Move session to project
   */
  async moveToProject(
    sessionId: string,
    projectId: string | null,
  ): Promise<{ status: string; session: BackendSession }> {
    return authFetch(`${API_BASE}/api/sessions/${sessionId}/move`, {
      method: "POST",
      body: JSON.stringify({ project_id: projectId }),
    });
  },

  /**
   * Toggle session favorite state
   */
  async toggleFavorite(sessionId: string): Promise<{
    status: string;
    is_favorite: boolean;
    session: BackendSession;
  }> {
    return authFetch(`${API_BASE}/api/sessions/${sessionId}/favorite`, {
      method: "POST",
    });
  },

  /**
   * Update session (including name and metadata)
   */
  async update(
    sessionId: string,
    data: { name?: string; metadata?: Record<string, unknown> },
  ): Promise<{ status: string; session: BackendSession }> {
    return authFetch(`${API_BASE}/api/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  /**
   * Mark session as read (clear unread count)
   */
  async markRead(sessionId: string): Promise<void> {
    await authFetch(`${API_BASE}/api/sessions/${sessionId}/mark-read`, {
      method: "POST",
    });
  },

  /**
   * Mark all sessions as read, optionally filtered by project or scheduled task.
   */
  async markAllRead(opts?: {
    projectId?: string;
    scheduledTaskId?: string;
  }): Promise<{ status: string; modified_count: number }> {
    const params = new URLSearchParams();
    if (opts?.projectId) params.set("project_id", opts.projectId);
    if (opts?.scheduledTaskId)
      params.set("scheduled_task_id", opts.scheduledTaskId);
    const qs = params.toString();
    return authFetch(
      `${API_BASE}/api/sessions/mark-all-read${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
  },

  async forkMessage(
    sessionId: string,
    messageId: string,
  ): Promise<{ session: BackendSession; source_session_id: string }> {
    return authFetch(buildMessageForkUrl(sessionId, messageId), {
      method: "POST",
    });
  },

  async createCheckpoint(
    sessionId: string,
    messageId: string,
    name?: string,
  ): Promise<{
    checkpoint: {
      id: string;
      name: string;
      message_id: string;
      created_at?: string;
    };
  }> {
    return authFetch(buildMessageCheckpointUrl(sessionId, messageId), {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  },

  async forkCheckpoint(
    sessionId: string,
    checkpointId: string,
  ): Promise<{ session: BackendSession; source_session_id: string }> {
    return authFetch(buildCheckpointForkUrl(sessionId, checkpointId), {
      method: "POST",
    });
  },
};
