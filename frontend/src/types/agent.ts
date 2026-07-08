// ============================================
// Agent Types
// ============================================

// agent 可配置项定义：类型、默认值、标签（支持 i18n key）、图标与下拉可选项等，
// 用于在 UI 上动态渲染每个 agent 暴露的开关/参数（如模型选择、思考深度等）。
export interface AgentOption {
  type: "boolean" | "string" | "number";
  default: boolean | string | number;
  label: string;
  label_key?: string; // i18n translation key for label
  description?: string;
  description_key?: string; // i18n translation key for description
  icon?: string; // lucide-react icon name (e.g., "Brain", "Zap", "Settings")
  options?: { value: string | number; label?: string; label_key?: string }[]; // For select/dropdown type options
}

// 单个 agent 的展示信息：ID/名称/描述/版本/图标/多语言标签，是否支持沙箱，及其可配置项。
export interface AgentInfo {
  id: string;
  name: string;
  description: string;
  version: string;
  sort_order?: number;
  icon?: string;
  labels?: AgentCatalogLabels;
  supports_sandbox?: boolean;
  options?: Record<string, AgentOption>;
}

// 获取可用 agent 列表的响应：agent 数组、总数、默认 agent 与当前用户允许的模型 ID。
export interface AgentListResponse {
  agents: AgentInfo[];
  count: number;
  default_agent?: string;
  allowed_model_ids?: string[] | null;
}

// Workflow event types
// 工作流步骤数据：步骤 ID/名称、执行的 agent、状态与结果。
export interface WorkflowStepData {
  step_id: string;
  step_name: string;
  agent_id?: string;
  status?: "running" | "completed" | "failed";
  result?: string;
}

// ============================================
// Agent Config Types
// ============================================

// Agent configuration (global)
// agent 全局配置：管理端维护的启用状态、图标、排序与多语言标签。
export interface AgentConfig {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  icon?: string;
  sort_order?: number;
  labels?: AgentCatalogLabels;
}

// 某一语言下 agent 的名称与描述。
export interface AgentCatalogLocale {
  name: string;
  description: string;
}

// agent 的多语言标签映射：语言码 → 该语言的名称/描述。
export type AgentCatalogLabels = Record<string, AgentCatalogLocale>;

// agent 目录配置（管理端完整形态）：含启用状态、图标、排序与多语言标签。
export interface AgentCatalogConfig {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  icon: string;
  sort_order: number;
  labels: AgentCatalogLabels;
}

// agent 目录配置响应：目录项数组与可用 agent ID 列表。
export interface AgentCatalogConfigResponse {
  agents: AgentCatalogConfig[];
  available_agents: string[];
}

// Global agent config response
// 全局 agent 配置响应：配置项数组与可用 agent 列表。
export interface GlobalAgentConfigResponse {
  agents: AgentConfig[];
  available_agents: string[];
}

// Role's accessible agents
// 角色可访问的 agent 分配：角色及其被允许使用的 agent 列表。
export interface RoleAgentAssignment {
  role_id: string;
  role_name: string;
  allowed_agents: string[];
}

// Response after updating role's accessible agents
// 更新角色可访问 agent 后的响应。
export interface RoleAgentAssignmentResponse {
  role_id: string;
  role_name: string;
  allowed_agents: string[];
}

// User's default agent preference
// 用户的默认 agent 偏好（null 表示未设置）。
export interface UserAgentPreference {
  default_agent_id: string | null;
}

// Response for user agent preference operations
// 用户默认 agent 偏好操作的响应。
export interface UserAgentPreferenceResponse {
  default_agent_id: string | null;
}

// Role's accessible models
// 角色可访问的模型分配：角色及其被允许使用的模型列表（configured 表示是否已显式配置）。
export interface RoleModelAssignment {
  role_id: string;
  role_name: string;
  allowed_models: string[];
  configured?: boolean;
}
