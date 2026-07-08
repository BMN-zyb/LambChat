// ============================================================================
// useAgent 模块的类型定义中心
// ----------------------------------------------------------------------------
// 本文件集中定义了驱动聊天核心 hook（useAgent）所需的全部类型，最关键的是
// 前后端流式交互协议 —— SSE（Server-Sent Events）事件词汇表 EventType。
// 后端在一次「运行（run）」中通过 SSE 持续向前端推送各类事件（正文增量、思考、
// 工具调用、子 agent、审批、沙箱、目标模式、token 用量等），前端据此实时地把
// 事件流转换成一条条消息及其内部的 parts。理解本文件的类型 = 理解整个聊天数据流。
// ============================================================================
import type {
  Message,
  AgentInfo,
  ConnectionStatus,
  FormField,
  MessageAttachment,
  PersonaPresetSnapshot,
} from "../../types";

// Event types from backend
// 【SSE 事件词汇表 —— 前后端流式协议的核心】
// 后端通过 GET /api/chat/sessions/{id}/stream 以 SSE 持续推送事件，每个事件都带有
// 一个 event 字段（取值即下面的联合成员）和一段 JSON 字符串 data（见 EventData）。
// eventProcessor / eventHandlers 会依据 event 字段把事件转换成消息的各类 parts。
// 下面逐一说明每种事件的语义（这是排查数据流问题时最重要的参考）：
export type EventType =
  // 元数据事件：一次 run 开始时下发，携带 session_id / agent 信息等上下文
  | "metadata"
  // 助手正文文本增量（流式 token）：需按到达顺序追加拼接成完整回答
  | "message:chunk"
  // 用户消息回显：含 message_id / 附件 / 启用的技能，用于历史重建与多端同步
  | "user:message"
  // 用户取消：携带 user_id / run_id，表示本次生成被用户主动中止
  | "user:cancel"
  // 思考过程（推理链）文本：thinking_id 标识同一思考块，用于折叠展示
  | "thinking"
  // 工具调用开始：含 tool 名与 args，前端展示「正在调用工具…」
  | "tool:start"
  // 工具调用结果：含 result / success，通过 tool_call_id 与对应的 tool:start 关联
  | "tool:result"
  // 产物结果：工具产出的 artifact（如代码/文档/图表），作为独立可渲染 part
  | "artifact:result"
  // 待办清单更新：todos 全量 + updated_index 指示本次变动项，用于任务规划展示
  | "todo:updated"
  // 会话摘要事件：对当前对话生成的总结
  | "summary"
  // 推荐问题：会话开始/空闲时给出的引导性问题
  | "recommend:questions"
  // 追问建议：一次回答完成后给出的后续可点击问题
  | "followup:questions"
  // 子 agent 调用开始（多 agent 协作）：depth 表示嵌套深度，用于层级展示
  | "agent:call"
  // 子 agent 调用结束：与 agent:call 配对，标记子 agent 任务完成
  | "agent:result"
  // 人工审批请求：需用户确认（choices/default/fields）后主流程才继续
  | "approval_required"
  // 沙箱环境启动中：代码执行环境正在初始化
  | "sandbox:starting"
  // 沙箱环境就绪：可以执行代码
  | "sandbox:ready"
  // 沙箱环境启动失败
  | "sandbox:error"
  // token 用量统计：输入/输出/缓存 token 数、耗时、模型 ID 等
  | "token:usage"
  // 技能集变更：运行期动态新增/删除技能（action / skill_name）
  | "skills:changed"
  // 排队状态更新：高并发排队时下发 status / queue_position
  | "queue_update"
  // 目标模式开始：进入自主迭代模式，直到满足 objective/rubric
  | "goal:start"
  // 目标模式结束
  | "goal:end"
  // 单条消息/回答生成完成（一条 assistant 消息收尾）
  | "complete"
  // 整个 SSE 流结束：终止事件，前端据此判定可以关闭连接（见 sseConnection）
  | "done"
  // 错误事件：携带 error 信息，前端展示并结束本次流
  | "error";

// 单个 SSE 事件的原始结构：event 为事件类型，data 为尚未解析的 JSON 字符串。
export interface StreamEvent {
  event: EventType;
  data: string;
}

// SSE 事件的 data 反序列化后的载荷（所有事件共用一个宽松结构，字段按事件类型选填）。
// 之所以把所有事件的字段合并在一个可选字段的大接口里，是为了让事件处理器可以用
// 同一套类型安全地读取任意事件的数据，而无需为每种事件单独定义类型。
export interface EventData {
  // 通用/元数据字段：会话、agent 身份等
  session_id?: string;
  agent_id?: string;
  agent_name?: string;
  agent_avatar?: string;
  // 工具调用相关：工具名、调用 ID、入参、结果、是否成功
  tool?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  artifact?: Record<string, unknown>;
  success?: boolean;
  // 文本/思考内容：message:chunk 的正文、thinking 的推理文本及其分组 ID
  content?: string;
  thinking_id?: string;
  error?: string;
  type?: string;
  // 步骤/子 agent 相关：步骤名、步骤 ID、输入、嵌套深度
  step_name?: string;
  step_id?: string;
  input?: string;
  depth?: number;
  // 人工审批事件字段：审批项 ID、给用户看的提示、可选项与默认选项
  // approval_required event fields
  id?: string;
  message?: string;
  choices?: string[];
  default?: string;
  // 沙箱事件字段：沙箱实例 ID 与其工作目录
  // sandbox event fields
  sandbox_id?: string;
  work_dir?: string;
  // token 用量事件字段：输入/输出/缓存 token 数、耗时、模型标识等
  // token:usage event fields
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  duration?: number;
  timestamp?: string;
  cache_creation_tokens?: number;
  cache_read_tokens?: number;
  model_id?: string;
  model?: string;
  // 用户消息事件字段：消息 ID、本次启用的技能列表、附件（图片/文件等）
  // user:message event fields
  message_id?: string;
  enabled_skills?: string[];
  attachments?: Array<{
    id: string;
    key: string;
    name: string;
    type: string;
    mime_type: string;
    size: number;
    url: string;
  }>;
  // 用户取消事件字段：发起取消的用户 ID 与被取消的运行 ID
  // user:cancel event fields
  user_id?: string;
  run_id?: string;
  // 技能变更事件字段：动作（新增/删除）、技能名、涉及的文件数
  // skills:changed event fields
  action?: string;
  skill_name?: string;
  files_count?: number;
  // 排队更新事件字段：排队状态与在队列中的位置
  // queue_update event fields
  status?: string;
  queue_position?: number;
  // 目标模式事件字段：目标定义（objective 目标描述、rubric 评分标准、
  // max_iterations 最大迭代次数）以及起止时间
  // goal:start / goal:end event fields
  goal?: {
    objective: string;
    rubric?: string;
    max_iterations?: number;
  };
  started_at?: string;
  ended_at?: string;
  // 待办事件字段：全量待办列表（含状态）与本次被更新项的索引
  // todo event fields
  todos?: Array<{
    content: string;
    activeForm?: string;
    status: "pending" | "in_progress" | "completed" | "cancelled";
  }>;
  updated_index?: number;
  // 摘要事件字段：摘要块 ID
  // summary event fields
  summary_id?: string;
  // 推荐/追问问题事件字段：问题可为纯字符串，或带内容/上传配置的对象
  // recommend:questions / followup:questions event fields
  questions?: Array<
    | string
    | {
        content?: string;
        text?: string;
        title?: string;
        upload?: Record<string, unknown>;
        data_upload?: Record<string, unknown>;
      }
  >;
}

// useAgent hook 的可选配置（由调用方注入的回调与取值函数）。
// 这些回调让 hook 在处理事件时能反向通知外层 UI（如弹出审批框），
// 或在发起请求前从外层读取当前的工具/技能/人设等配置。
export interface UseAgentOptions {
  // 收到 approval_required 事件时回调：外层据此弹出人工审批弹窗
  onApprovalRequired?: (approval: {
    id: string;
    message: string;
    type: string;
    fields?: FormField[];
    expires_at?: string | null;
    timeout?: number;
    metadata?: Record<string, unknown>;
  }) => void;
  // 清空所有待审批项（如流结束/切换会话时）
  onClearApprovals?: () => void;
  // 发送消息前读取：当前启用的工具列表
  getEnabledTools?: () => string[];
  // 发送消息前读取：被禁用的技能列表
  getDisabledSkills?: () => string[];
  // 发送消息前读取：被显式启用的技能列表（undefined 表示不覆盖默认）
  getEnabledSkills?: () => string[] | undefined;
  // 发送消息前读取：当前选中的人设预设 ID
  getPersonaPresetId?: () => string | null;
  // 发送消息前读取：被禁用的 MCP 工具列表
  getDisabledMcpTools?: () => string[];
  // 发送消息前读取：额外的 agent 运行选项（键值对）
  getAgentOptions?: () => Record<string, boolean | string | number>;
  // 收到 skills:changed（新增技能）事件时回调，通知外层刷新技能列表
  onSkillAdded?: (
    skillName: string,
    description: string,
    filesCount: number,
  ) => void;
  // SSE 流结束（done）时回调
  onStreamDone?: () => void;
}

// 当前激活的目标模式规格：目标描述、评分标准、最大迭代次数与运行/起止信息。
export interface ActiveGoalSpec {
  objective: string;
  rubric?: string;
  max_iterations?: number;
  runId?: string;
  started_at?: string;
  ended_at?: string;
}

// Subagent tracking item
// 子 agent 追踪项：用一个栈来跟踪当前嵌套的子 agent 调用。
// agent_id 标识子 agent，depth 为嵌套深度，message_id 关联其对应的消息。
export interface SubagentStackItem {
  agent_id: string;
  depth: number;
  message_id: string;
}

// History event data structure
// 历史事件的 data 结构：与实时 EventData 类似，但仅包含重建历史消息所需字段。
// historyLoader 会读取持久化的历史事件，据此把过往对话重放成消息 parts。
export interface HistoryEventData {
  content?: string;
  tool?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  success?: boolean;
  error?: string;
  depth?: number;
  agent_id?: string;
  agent_name?: string;
  input?: string;
  timestamp?: string;
  sandbox_id?: string;
  work_dir?: string;
  thinking_id?: string;
  todos?: Array<{
    content: string;
    activeForm?: string;
    status: "pending" | "in_progress" | "completed" | "cancelled";
  }>;
  updated_index?: number;
  questions?: Array<
    | string
    | {
        content?: string;
        text?: string;
        title?: string;
        upload?: Record<string, unknown>;
        data_upload?: Record<string, unknown>;
      }
  >;
  attachments?: Array<{
    id: string;
    key: string;
    name: string;
    type: string;
    mime_type: string;
    size: number;
    url: string;
  }>;
  message_id?: string;
  enabled_skills?: string[];
}

// History event from backend
// 后端返回的单条历史事件：event_type 对应实时协议里的事件名，
// data 为对应载荷（用 unknown 兜底以兼容旧数据），并附带时间戳与 run_id。
export interface HistoryEvent {
  id?: string | number;
  event_type: string;
  data: HistoryEventData | unknown;
  timestamp?: string;
  run_id?: string;
}

// Return type for useAgent hook
// useAgent hook 对外暴露的完整接口：既包含聊天状态（消息、加载中、错误、连接状态、
// 目标模式、沙箱状态等），也包含可供 UI 调用的动作（发送消息、停止生成、切换 agent、
// 加载历史、重连 SSE 等）。这是 UI 层与聊天引擎交互的唯一契约。
export interface UseAgentReturn {
  messages: Message[];
  isLoading: boolean;
  isLoadingHistory: boolean;
  error: string | null;
  sessionId: string | null;
  currentProjectId: string | null;
  currentRunId: string | null;
  agents: AgentInfo[];
  currentAgent: string;
  agentsLoading: boolean;
  allowedModelIds: string[] | null;
  isReconnecting: boolean;
  connectionStatus: ConnectionStatus;
  newlyCreatedSession: BackendSession | null;
  activeGoal: ActiveGoalSpec | null;
  goalsByRunId: Record<string, ActiveGoalSpec>;
  isInitializingSandbox: boolean;
  sandboxError: string | null;
  sendMessage: (
    content: string,
    agentOptions?: Record<string, boolean | string | number>,
    attachments?: MessageAttachment[],
    runOptions?: { enabledSkills?: string[] },
  ) => Promise<void>;
  clearActiveGoal: () => void;
  stopGeneration: () => Promise<void>;
  clearMessages: () => void;
  selectAgent: (agentId: string) => void;
  switchAgent: (agentId: string) => void;
  selectTeam: (teamId: string | null) => void;
  selectedTeamId: string | null;
  goalModeEnabled: boolean;
  setGoalModeEnabled: React.Dispatch<React.SetStateAction<boolean>>;
  autoModeEnabled: boolean;
  setAutoModeEnabled: React.Dispatch<React.SetStateAction<boolean>>;
  refreshAgents: () => Promise<void>;
  loadHistory: (
    targetSessionId: string,
    targetRunId?: string,
  ) => Promise<SessionConfig | null>;
  reconnectSSE: () => Promise<void>;
  setPendingProjectId: (id: string | null) => void;
  autoExpandProjectId: string | null;
  clearAutoExpandProjectId: (id?: string | null) => void;
}

// Session configuration restored from metadata
// 从会话 metadata 中恢复出的配置：加载历史会话时用它还原当时的 agent、
// 启用/禁用的工具与技能、人设预设、MCP 工具、所属团队等，保证复现一致的运行环境。
export interface SessionConfig {
  agent_id?: string;
  agent_options?: Record<string, boolean | string | number>;
  disabled_tools?: string[];
  disabled_skills?: string[];
  enabled_skills?: string[];
  persona_preset_id?: string;
  persona_preset_name?: string;
  persona_snapshot?: PersonaPresetSnapshot;
  disabled_mcp_tools?: string[];
  team_id?: string;
}

// Backend session type (simplified)
// 后端会话对象（简化版）：会话 ID、绑定的 agent、创建/更新时间、是否激活、
// 任意元数据与可选名称。新建会话成功后前端会用它更新会话列表。
export interface BackendSession {
  id: string;
  agent_id: string;
  created_at: string;
  updated_at: string;
  is_active: boolean;
  metadata: Record<string, unknown>;
  name?: string;
}
