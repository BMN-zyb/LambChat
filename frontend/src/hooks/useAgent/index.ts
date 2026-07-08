// 【useAgent 模块的公共出口（barrel）】统一对外转出各子模块的类型与工具函数，
// 使外部只需从 "./useAgent" 导入，无需感知内部文件拆分。

// Re-export types
export type {
  EventType,
  StreamEvent,
  EventData,
  UseAgentOptions,
  SubagentStackItem,
  HistoryEventData,
  HistoryEvent,
  UseAgentReturn,
  BackendSession,
} from "./types";

// Re-export message parts utilities
export {
  addPartToDepth,
  updateSubagentResult,
  updateSubagentResultInParts,
  updateToolResultInDepth,
  updateToolResultInPartsById,
  createToolPart,
  createThinkingPart,
  createSubagentPart,
  clearAllLoadingStates,
} from "./messageParts";

// Re-export event processor
export { convertAttachments, processMessageEvent } from "./eventProcessor";
export type { ProcessMessageEventResult } from "./eventProcessor";

// Re-export history loader utilities
export {
  reconstructMessagesFromEvents,
  getLastEventTimestamp,
} from "./historyLoader";
