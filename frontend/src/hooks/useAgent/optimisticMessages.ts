// 【乐观消息】用户点击发送时，先在本地立即插入「用户消息 + 空的流式 assistant 消息」，
// 无需等待后端确认即可即时反馈，提升响应感。后续真实事件到达后再回填 assistant 内容，
// 而用户消息会在收到 user:message 回显时被对齐/去重（见 eventHandlers.handleUserMessage）。

import type { Message } from "../../types/message.ts";
import type { MessageAttachment } from "../../types/upload.ts";
import { uuid } from "../../utils/uuid.ts";

// 入参：已有消息、用户输入内容、附件、启用技能，以及可注入的时间与 ID 生成器（便于测试）。
interface CreateOptimisticMessagesForSendOptions {
  previousMessages: Message[];
  content: string;
  attachments?: MessageAttachment[];
  enabledSkills?: string[];
  now?: Date;
  createId?: () => string;
}

// 返回：追加了乐观消息后的新列表，以及那条空 assistant 消息的 ID（供 SSE 往里写内容）。
interface CreateOptimisticMessagesForSendResult {
  messages: Message[];
  assistantMessageId: string;
}

// 构造发送时的乐观消息对：一条用户消息（含附件/技能）+ 一条 isStreaming 的空 assistant 消息。
// 纯函数，不改动 previousMessages。
export function createOptimisticMessagesForSend({
  previousMessages,
  content,
  attachments,
  enabledSkills,
  now = new Date(),
  createId = () => uuid(),
}: CreateOptimisticMessagesForSendOptions): CreateOptimisticMessagesForSendResult {
  const userMessage: Message = {
    id: createId(),
    role: "user",
    content: content.trim(),
    timestamp: now,
    attachments,
    enabledSkills,
  };

  // 空的流式 assistant 占位消息：其 id 返回给调用方，SSE 事件据此把内容写入这条消息
  const assistantMessage: Message = {
    id: createId(),
    role: "assistant",
    content: "",
    timestamp: now,
    toolCalls: [],
    toolResults: [],
    isStreaming: true,
  };

  return {
    messages: [...previousMessages, userMessage, assistantMessage],
    assistantMessageId: assistantMessage.id,
  };
}
