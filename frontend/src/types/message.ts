import type { MessageAttachment } from "./upload";

// ============================================
// Message Types
// ============================================

// 一条聊天消息。role 区分用户/助手/系统；content 为纯文本汇总，
// 而 parts 才是按到达顺序渲染的「内容块」序列（文本、工具、思考、子 agent、沙箱等）。
// 其余字段承载工具调用/结果、token 用量、耗时、附件、反馈、取消态与本次启用技能等元信息。
export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: Date;
  toolCalls?: ToolCall[];
  toolResults?: ToolResult[];
  isStreaming?: boolean;
  // 有序内容块 - 用于按顺序渲染文本和工具调用
  parts?: MessagePart[];
  // Token 使用统计
  tokenUsage?: TokenUsagePart;
  // 对话耗时（毫秒）
  duration?: number;
  // 用户消息附件
  attachments?: MessageAttachment[];
  // 运行 ID - 用于反馈
  runId?: string;
  // 用户对该消息的反馈 (从 feedback API 加载)
  feedback?: import("./feedback").RatingValue;
  // 反馈 ID
  feedbackId?: string;
  // 是否被取消
  cancelled?: boolean;
  // 用户消息发送时启用的技能名称列表
  enabledSkills?: string[];
}

// 消息内容块类型
// 【消息内容块的可辨识联合】一条 assistant 消息由若干有序 part 组成，按 type 区分渲染方式：
// text 正文 / tool 工具调用 / artifact 产物 / subagent 子 agent（可嵌套）/ thinking 思考 /
// sandbox 沙箱状态 / token_usage 用量 / cancelled 取消标记 / todo 待办 / summary 摘要 /
// recommend_questions 推荐问题。eventProcessor 正是把 SSE 事件转换成这些 part。
export type MessagePart =
  | TextPart
  | ToolPart
  | ArtifactPart
  | SubagentPart
  | ThinkingPart
  | SandboxPart
  | TokenUsagePart
  | CancelledPart
  | TodoPart
  | SummaryPart
  | RecommendQuestionsPart;

// Sandbox 状态块类型（用于渲染沙箱初始化状态）
export interface SandboxPart {
  type: "sandbox";
  status: "starting" | "ready" | "error" | "cancelled";
  sandbox_id?: string;
  work_dir?: string;
  error?: string;
  timestamp?: string;
  startedAt?: string;
  completedAt?: string;
}

// Token 使用统计块类型
export interface TokenUsagePart {
  type: "token_usage";
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_creation_tokens?: number;
  cache_read_tokens?: number;
  model_id?: string;
  model?: string;
}

// 取消状态块类型
export interface CancelledPart {
  type: "cancelled";
}

// Todo 任务列表块类型
export type TodoStatus = "pending" | "in_progress" | "completed" | "cancelled";

export interface TodoItem {
  content: string;
  activeForm?: string;
  status: TodoStatus;
}

// 待办清单块：items 为全部待办项，isStreaming 表示仍在更新中。
export interface TodoPart {
  type: "todo";
  items: TodoItem[];
  isStreaming?: boolean;
}

// 摘要块：对当前对话/阶段的总结；按 summary_id 区分并可流式累加。
export interface SummaryPart {
  type: "summary";
  content: string;
  summary_id?: string;
  depth?: number;
  agent_id?: string;
  isStreaming?: boolean;
}

// 单条推荐/追问问题：content 为问题文本，upload 为可选的附带上传配置。
export interface RecommendQuestion {
  content: string;
  upload?: Record<string, unknown>;
}

// 推荐问题块：一组可点击的引导/追问问题。
export interface RecommendQuestionsPart {
  type: "recommend_questions";
  questions: RecommendQuestion[];
  depth?: number;
  agent_id?: string;
}

// 文本块：助手正文的一段。depth>0 / agent_id 表示它属于某个子 agent。
export interface TextPart {
  type: "text";
  content: string;
  depth?: number;
  agent_id?: string;
}

// 思考块：模型的推理过程。thinking_id 用于把同一思考的多段增量归并。
export interface ThinkingPart {
  type: "thinking";
  content: string;
  thinking_id?: string;
  depth?: number;
  agent_id?: string;
  isStreaming?: boolean;
}

// 工具调用块：name/args 为调用信息；result/success/error 为返回；
// isPending 表示等待结果、cancelled 表示被取消；depth/agent_id 标识归属层级。
export interface ToolPart {
  type: "tool";
  id?: string;
  name: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  success?: boolean;
  error?: string;
  isPending?: boolean;
  cancelled?: boolean;
  depth?: number;
  agent_id?: string;
  startedAt?: string;
  completedAt?: string;
}

// 产物的具体载荷：可为单个文件（含预览信息），或整个项目/文件夹（含文件数与预览）。
export type ArtifactPartArtifact =
  | {
      kind: "file";
      id: string;
      name: string;
      path: string;
      description?: string;
      fileSize?: number;
      preview: {
        kind: "file";
        previewKey: string;
        filePath: string;
        s3Key?: string;
        signedUrl?: string;
        fileSize?: number;
      };
    }
  | {
      kind: "project";
      id: string;
      name: string;
      mode: "project" | "folder";
      fileCount: number;
      template: string;
      preview: {
        kind: "project";
        previewKey: string;
        project: Record<string, unknown>;
      };
    };

// 产物块：包裹一个 artifact 及其成功/错误态，供 UI 以卡片/预览形式渲染。
export interface ArtifactPart {
  type: "artifact";
  artifact: ArtifactPartArtifact;
  success?: boolean;
  error?: string;
  depth?: number;
  agent_id?: string;
  completedAt?: string;
}

// 子 agent 块：表示一次子 agent 调用。depth 为嵌套深度，parts 承载其内部内容（可再嵌套），
// status 跟踪其生命周期（pending/running/complete/error/cancelled）。
export interface SubagentPart {
  type: "subagent";
  agent_id: string;
  agent_name: string;
  agent_avatar?: string;
  input: string;
  result?: string;
  success?: boolean;
  error?: string; // 错误信息
  isPending?: boolean;
  cancelled?: boolean;
  depth: number;
  // 子代理内部的内容（嵌套）
  parts?: MessagePart[];
  // 时间追踪
  startedAt?: number; // Unix timestamp (ms)
  completedAt?: number; // Unix timestamp (ms)
  // 状态: pending | running | complete | error | cancelled
  status?: "pending" | "running" | "complete" | "error" | "cancelled";
}

// 工具调用（发起时）：id 关联结果，name 工具名，args 入参。
export interface ToolCall {
  id?: string;
  name: string;
  args: Record<string, unknown>;
}

// 工具结果（返回时）：通过 id/name 关联对应调用，result 为返回、success 表示成败。
export interface ToolResult {
  id?: string;
  name: string;
  result: string | Record<string, unknown>;
  success: boolean;
}

// DeepAgents event types
// DeepAgents 相关的原始消息/事件结构（对接后端 agent 框架的中间数据形态）
export interface AIMessage {
  content: string;
  tool_calls?: RawToolCall[];
  id?: string;
}

export interface RawToolCall {
  name: string;
  args: Record<string, unknown>;
  id?: string;
}

export interface ToolMessage {
  content: string;
  name: string;
  tool_call_id?: string;
}

export interface DeepAgentState {
  messages?: (AIMessage | ToolMessage)[];
}

export interface StreamEventData {
  content: string;
  metadata: Record<string, unknown>;
  session_id?: string;
}

// ============================================
// Form Field Types (Human Tool)
// ============================================

// 人工审批表单的字段类型（文本/多行/数字/勾选/下拉/单选/多选）。
export type FormFieldType =
  | "text"
  | "textarea"
  | "number"
  | "checkbox"
  | "select"
  | "radio"
  | "multi_select";

// 单个表单字段定义：名称、标签、类型、占位/默认值、是否必填与可选项等。
export interface FormField {
  name: string;
  label: string;
  type: FormFieldType;
  placeholder?: string;
  default?: unknown;
  required: boolean;
  options?: string[];
  multiple?: boolean;
}

// 待人工审批项：对应 approval_required 事件，含提示信息、表单字段、状态与过期/超时等。
export interface PendingApproval {
  id: string;
  message: string;
  type: "form";
  fields: FormField[];
  status: "pending" | "approved" | "rejected";
  session_id?: string | null;
  expires_at?: string | null;
  timeout?: number;
  metadata?: Record<string, unknown>;
}

// 旧版/通用流事件结构（非当前 SSE 词汇表，历史/兼容用途）。
export interface StreamEvent {
  type:
    | "thinking"
    | "content"
    | "tool_call"
    | "tool_result"
    | "step"
    | "complete"
    | "error";
  content: string;
  metadata: Record<string, unknown>;
}

// 一次 agent 运行的汇总响应：成败、总结消息、步数、步骤日志与会话 ID。
export interface AgentResponse {
  success: boolean;
  message: string;
  steps: number;
  logs: AgentStep[];
  session_id: string;
}

// 单个 agent 步骤：序号、思考、工具调用与结果。
export interface AgentStep {
  step: number;
  thought?: string;
  tool_calls: ToolCall[];
  tool_results: ToolResult[];
}

// ============================================
// SSE Connection Types
// ============================================

// SSE 连接状态：连接中 / 已连接 / 重连中 / 已断开（驱动连接指示 UI）。
export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

// 连接状态快照：当前状态、重试次数与上次连接成功时间。
export interface ConnectionState {
  status: ConnectionStatus;
  retryCount: number;
  lastConnectedAt: Date | null;
}

// ============================================
// Run Types (Multi-turn Conversation)
// ============================================

// 单次运行（一轮问答）的摘要：运行/追踪 ID、起止时间、状态、事件数与触发的用户消息。
export interface RunSummary {
  run_id: string;
  trace_id: string;
  agent_id?: string;
  started_at: string;
  completed_at?: string;
  status: "pending" | "running" | "completed" | "failed";
  event_count: number;
  user_message?: string;
}
