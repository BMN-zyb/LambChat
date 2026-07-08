import type { ToolCall, MessagePart } from "./message";

// ============================================
// Session Types
// ============================================

// 会话完整对象：ID、所属用户/agent、工作目录、时间、状态、消息列表与任意元数据。
export interface Session {
  id: string;
  user_id?: string;
  agent_id: string;
  workspace_dir: string;
  created_at: string;
  updated_at: string;
  status: "active" | "archived";
  messages: SessionMessage[];
  metadata: Record<string, unknown>;
}

// 会话内的一条消息（后端存储形态）：additional_kwargs 可携带工具调用、是否部分、以及 parts。
export interface SessionMessage {
  role: "user" | "assistant" | "system" | "human" | "ai";
  content: string;
  created_at?: string;
  additional_kwargs?: {
    tool_calls?: ToolCall[];
    partial?: boolean;
    parts?: MessagePart[];
  };
}

// 会话摘要（列表项）：不含完整消息，仅含计数与元数据，用于会话列表展示。
export interface SessionSummary {
  session_id: string;
  agent_id: string;
  created_at: string;
  updated_at: string;
  status: "active" | "archived";
  message_count: number;
  metadata: Record<string, unknown>;
}

// 会话 + 其消息 + 事件总数的组合响应。
export interface SessionWithMessages {
  session: Session;
  messages: SessionMessage[];
  total_events: number;
}

// 会话列表分页响应：会话摘要数组、总数与分页参数。
export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  limit: number;
  offset: number;
}

// 单条已持久化的 SSE 事件记录（历史重建的数据源）：事件类型、载荷、时间戳与运行 ID。
export interface SSEEventRecord {
  id: string;
  event_type: string;
  data: Record<string, unknown>;
  timestamp: string;
  run_id?: string;
}

// 会话事件列表响应：一个会话下的全部历史事件。
export interface SessionEventsResponse {
  events: SSEEventRecord[];
}
