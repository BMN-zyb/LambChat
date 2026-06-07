export interface RunConversationMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

export interface ScheduledTaskDefaults {
  agentId?: string;
  modelId?: string;
  modelValue?: string;
}
