/**
 * History event loader for useAgent hook
 * Reconstructs messages from stored events.
 *
 * Message transformation logic is unified in processMessageEvent (messageParts.ts).
 * This file handles: event iteration, message reconstruction, and
 * user:message / user:cancel / approval_required which are history-specific.
 */
// 【历史消息重建】把后端持久化的历史事件「重放」成消息列表（用于打开历史会话/刷新页面）。
// 核心思路：按时间排序事件后依次消费，遇到 user:message 收尾上一条 assistant 并新建用户消息，
// 其余事件则累积进「当前 assistant 消息」；具体的「事件→parts」转换复用 processMessageEvent
// （isStreaming=false）。事件通过 run_id 归属到同一次运行的 assistant 消息。
// 本文件还负责历史特有的处理：用户取消的收尾、审批状态回查、以及目标模式的恢复。

import type { Message, MessagePart, FormField } from "../../types";
import { uuid } from "../../utils/uuid";
import { authFetch } from "../../services/api/fetch";
import { buildApiUrl } from "../../services/api/config";
import i18n from "../../i18n";
import type {
  EventData,
  SubagentStackItem,
  HistoryEvent,
  HistoryEventData,
  ActiveGoalSpec,
} from "./types";
import { convertAttachments, processMessageEvent } from "./eventProcessor";
import { clearAllLoadingStates } from "./messageParts";
import { parseDate } from "../../utils/datetime";

// 解析用户消息 ID：优先用事件里的 message_id，其次用 `${run_id}:user`，都没有则临时生成。
function resolveUserMessageId(
  event: HistoryEvent,
  eventData: HistoryEventData,
): string {
  if (typeof eventData.message_id === "string" && eventData.message_id.trim()) {
    return eventData.message_id;
  }
  if (typeof event.run_id === "string" && event.run_id.trim()) {
    return `${event.run_id}:user`;
  }
  return uuid();
}

// 历史重建的可选项：审批回调（用于恢复仍待审批的项）与子 agent 调用栈。
interface ProcessHistoryOptions {
  options?: {
    onApprovalRequired?: (approval: {
      id: string;
      message: string;
      type: string;
      fields?: FormField[];
      metadata?: Record<string, unknown>;
    }) => void;
  };
  activeSubagentStack: SubagentStackItem[];
}

// 解析事件时间戳；无时间戳时回退到给定的毫秒数（用于排序/展示）。
function parseEventTimestamp(
  timestamp: string | undefined,
  fallbackMs: number,
): Date {
  return timestamp ? parseDate(timestamp) : new Date(fallbackMs);
}

// 判断某事件类型是否「可以并入上一条 assistant 消息」：排除用户消息、元数据、结束、审批等
// 这些不产生 assistant 内容或需独立处理的类型。
function canAttachEventTypeToPreviousAssistant(eventType: string): boolean {
  return (
    eventType !== "user:message" &&
    eventType !== "user:cancel" &&
    eventType !== "metadata" &&
    eventType !== "done" &&
    eventType !== "goal:updated" &&
    eventType !== "approval_required"
  );
}

// 判断上一条消息是否是「同一次运行（run_id 相同）的 assistant 消息」，是则可把事件并入它。
function canAttachToPreviousAssistant(
  event: HistoryEvent,
  message: Message | undefined,
): message is Message {
  return (
    message?.role === "assistant" &&
    Boolean(event.run_id) &&
    message.runId === event.run_id
  );
}

/**
 * Process a single history event and update message state.
 * Returns updated currentAssistantMessage or new message.
 */
// 处理单条历史事件并更新消息状态。
// 参数：event 历史事件、currentAssistantMessage 当前正在累积的 assistant 消息、
// processedEventIds 已处理事件 ID 集合、opts 选项（审批回调 + 子 agent 栈）。
// 返回：更新后的 assistant 消息；返回 null 表示应「收尾当前 assistant 并新建用户消息」。
function processHistoryEvent(
  event: HistoryEvent,
  currentAssistantMessage: Message | null,
  processedEventIds: Set<string>,
  opts: ProcessHistoryOptions,
): Message | null {
  const eventType = event.event_type;
  const eventData = event.data as HistoryEventData;
  const depth = eventData.depth || 0;
  const agentId = eventData.agent_id;

  // Track processed event IDs
  // 记录已处理事件 ID，避免与实时流重复消费同一事件
  if (event.id) {
    processedEventIds.add(event.id.toString());
  }

  // Handle user message
  // 用户消息：返回 null，交由外层收尾当前 assistant 并单独创建用户消息
  if (eventType === "user:message") {
    return null; // Signal to push current assistant and create user message
  }

  // Skip events that don't contribute to message content
  // 跳过不产生消息内容的事件（元数据/结束/目标更新）
  if (
    eventType === "metadata" ||
    eventType === "done" ||
    eventType === "goal:updated"
  ) {
    return currentAssistantMessage;
  }

  // Handle approval_required
  // 审批事件：回查审批状态，若仍 pending 则回调外层恢复审批 UI
  if (eventType === "approval_required") {
    const approvalData = eventData as {
      id?: string;
      message?: string;
      type?: string;
      fields?: FormField[];
    };
    if (approvalData.id && opts.options?.onApprovalRequired) {
      authFetch<{
        status: string;
        message?: string;
        type?: string;
        fields?: FormField[];
        metadata?: Record<string, unknown>;
      }>(buildApiUrl(`/human/${approvalData.id}`))
        .then((data) => data ?? null)
        .then((approval) => {
          if (approval?.status === "pending") {
            opts.options?.onApprovalRequired?.({
              id: approvalData.id!,
              message: approval.message || "",
              type: approval.type || "form",
              fields: approval.fields,
              metadata: approval.metadata,
            });
          }
        })
        .catch((e) => {
          console.warn("[loadHistory] Failed to check approval status:", e);
        });
    }
    return currentAssistantMessage;
  }

  // CancelledError with no current message — don't create an empty assistant message
  // 取消错误且当前没有 assistant 消息：不为其创建空的 assistant 消息
  if (eventType === "error") {
    const errorData = eventData as { type?: string };
    if (errorData.type === "CancelledError" && !currentAssistantMessage) {
      return null;
    }
  }

  // Ensure assistant message exists for other event types
  // 其余事件：确保存在承接内容的 assistant 消息（没有则以 run_id 为 ID 新建）
  let msg = currentAssistantMessage;
  if (!msg) {
    const messageId = event.run_id || uuid();
    msg = {
      id: messageId,
      role: "assistant",
      content: "",
      timestamp: parseEventTimestamp(event.timestamp, Date.now()),
      parts: [],
      isStreaming: false,
      runId: event.run_id,
    };
  } else if (event.run_id && !msg.runId) {
    msg = { ...msg, runId: event.run_id };
  }

  // Manage subagent stack
  // 子 agent 调用开始：入栈（与实时流一致，供 processMessageEvent 归属嵌套层级）
  if (eventType === "agent:call") {
    opts.activeSubagentStack.push({
      agent_id: agentId || "unknown",
      depth,
      message_id: msg.id,
    });
  }

  // Use unified event processor
  // 复用统一事件处理器把事件合并进 parts（历史场景 isStreaming=false）
  const result = processMessageEvent(
    eventType,
    eventData as EventData,
    msg.parts || [],
    msg.content,
    msg.toolCalls || [],
    depth,
    opts.activeSubagentStack,
    false, // isStreaming = false for history
    msg.id,
  );

  // Apply result to message
  msg.parts = result.parts;
  msg.content = result.content;
  msg.toolCalls = result.toolCalls;

  if (result.toolResult) {
    msg.toolResults = [...(msg.toolResults || []), result.toolResult];
  }
  if (result.tokenUsage) {
    msg.tokenUsage = result.tokenUsage;
  }
  if (result.duration) {
    msg.duration = result.duration;
  }
  if (result.cancelled) {
    msg.cancelled = true;
  }

  // Pop subagent stack after agent:result
  // 子 agent 调用结束：按 agent_id + message_id 出栈
  if (eventType === "agent:result") {
    const stackIndex = opts.activeSubagentStack.findIndex(
      (item) =>
        item.agent_id === (agentId || "unknown") && item.message_id === msg.id,
    );
    if (stackIndex !== -1) {
      opts.activeSubagentStack.splice(stackIndex, 1);
    }
  }

  return msg;
}

/**
 * Reconstruct messages from history events.
 */
// 从历史事件重建完整消息列表：先按时间排序，逐个消费并组织成 user/assistant 消息序列。
// 期间对 user:message 做去重（按 message_id 与 run_id），并把可归属的事件并入上一条同 run 的 assistant。
export function reconstructMessagesFromEvents(
  events: HistoryEvent[],
  processedEventIds: Set<string>,
  opts: ProcessHistoryOptions,
): Message[] {
  // Sort events by timestamp
  // 按时间戳升序排序，保证重建顺序与真实发生顺序一致
  const sortedEvents = [...events].sort((a, b) => {
    const timeA = parseEventTimestamp(a.timestamp, 0).getTime();
    const timeB = parseEventTimestamp(b.timestamp, 0).getTime();
    return timeA - timeB;
  });

  const reconstructedMessages: Message[] = [];
  let currentAssistantMessage: Message | null = null;
  const seenUserMessageIds = new Set<string>();
  const seenUserMessageRunIds = new Set<string>();

  for (const event of sortedEvents) {
    const eventType = event.event_type;
    const eventData = event.data as HistoryEventData;

    // Handle user message separately
    // 用户消息：先按 message_id / run_id 去重，再收尾当前 assistant，最后追加这条用户消息
    if (eventType === "user:message") {
      const userMessageId = resolveUserMessageId(event, eventData);
      const userMessageRunId =
        typeof event.run_id === "string" && event.run_id.trim()
          ? event.run_id
          : null;
      if (
        seenUserMessageIds.has(userMessageId) ||
        (userMessageRunId && seenUserMessageRunIds.has(userMessageRunId))
      ) {
        continue;
      }
      seenUserMessageIds.add(userMessageId);
      if (userMessageRunId) {
        seenUserMessageRunIds.add(userMessageRunId);
      }

      if (currentAssistantMessage) {
        reconstructedMessages.push(currentAssistantMessage);
        currentAssistantMessage = null;
      }
      const userAttachments = convertAttachments(eventData.attachments);
      const enabledSkills = Array.isArray(eventData.enabled_skills)
        ? eventData.enabled_skills
        : undefined;
      reconstructedMessages.push({
        id: userMessageId,
        role: "user",
        content: eventData.content || "",
        timestamp: parseEventTimestamp(event.timestamp, Date.now()),
        attachments: userAttachments,
        runId: event.run_id,
        enabledSkills,
      });
      continue;
    }

    // Handle user cancel
    // 用户取消：收尾当前 assistant（清理 loading、给未完成工具补「已取消」结果、追加 cancelled 部件）；
    // 若此刻没有 assistant，则单独插入一条仅含 cancelled 部件的 assistant 消息
    if (eventType === "user:cancel") {
      if (currentAssistantMessage) {
        const clearedParts = clearAllLoadingStates(
          currentAssistantMessage.parts || [],
        );
        // Also set result on pending tools for history display
        // 历史展示：给「已取消但无结果」的工具补上「已取消」文案，避免显示为空
        const updatedParts = clearedParts.map((part): MessagePart => {
          if (part.type === "tool" && part.cancelled && !part.result) {
            return {
              ...part,
              result: i18n.t("chat.cancelled"),
              success: false,
            };
          }
          return part;
        });
        const updatedMessage = {
          ...currentAssistantMessage,
          isStreaming: false,
          cancelled: true,
          parts: [...updatedParts, { type: "cancelled" as const }],
        };
        reconstructedMessages.push(updatedMessage);
      } else {
        reconstructedMessages.push({
          id: uuid(),
          role: "assistant",
          content: "",
          timestamp: parseEventTimestamp(event.timestamp, Date.now()),
          parts: [{ type: "cancelled" }],
          runId: event.run_id,
        });
      }
      currentAssistantMessage = null;
      continue;
    }

    if (
      !currentAssistantMessage &&
      canAttachEventTypeToPreviousAssistant(eventType)
    ) {
      const lastMessageIndex = reconstructedMessages.length - 1;
      const lastMessage = reconstructedMessages[lastMessageIndex];
      if (canAttachToPreviousAssistant(event, lastMessage)) {
        const updatedMessage = processHistoryEvent(
          event,
          lastMessage,
          processedEventIds,
          opts,
        );
        if (updatedMessage) {
          reconstructedMessages[lastMessageIndex] = updatedMessage;
        }
        continue;
      }
    }

    // Process other events
    // 其它事件：无法并入上一条 assistant 时，累积到「当前 assistant 消息」
    currentAssistantMessage = processHistoryEvent(
      event,
      currentAssistantMessage,
      processedEventIds,
      opts,
    );
  }

  if (currentAssistantMessage) {
    reconstructedMessages.push(currentAssistantMessage);
  }

  return reconstructedMessages;
}

// prepareMessagesForRunningRun 的返回：重建后的消息列表 + 应继续流式接收的消息 ID。
export interface RunningAssistantPreparationResult {
  messages: Message[];
  streamingMessageId: string;
}

// 当历史属于一次仍在运行的 run 时，尝试从当前内存消息里找回「已乐观插入但历史里还没有」的用户消息，
// 以免重连后该用户气泡丢失。返回该乐观用户消息（补上 runId）或 null。
function getPendingOptimisticUserForRun(
  currentMessages: Message[],
  runId: string,
): Message | null {
  const streamingAssistantIndex = currentMessages.findIndex(
    (message) =>
      message.role === "assistant" &&
      message.isStreaming &&
      (message.runId === runId || message.id === runId),
  );
  if (streamingAssistantIndex <= 0) {
    return null;
  }

  const candidate = currentMessages[streamingAssistantIndex - 1];
  if (candidate?.role !== "user") {
    return null;
  }

  return {
    ...candidate,
    runId: candidate.runId ?? runId,
  };
}

// 为「仍在运行的 run」准备消息：确保存在一条对应该 run 的 assistant 消息并标记为流式中，
// 供后续 SSE 继续往里写内容。若历史里已有该 run 的 assistant 则复用（置 isStreaming），
// 否则新建一条空的流式 assistant 消息。返回消息列表与该流式消息 ID。
export function prepareMessagesForRunningRun(
  messages: Message[],
  runId: string,
  createId: () => string = () => uuid(),
  currentMessages: Message[] = [],
): RunningAssistantPreparationResult {
  const pendingOptimisticUser = getPendingOptimisticUserForRun(
    currentMessages,
    runId,
  );
  const messagesWithPendingUser =
    pendingOptimisticUser &&
    !messages.some(
      (message) => message.role === "user" && message.runId === runId,
    )
      ? [...messages, pendingOptimisticUser]
      : messages;

  const existingAssistant = [...messagesWithPendingUser]
    .reverse()
    .find((message) => message.role === "assistant" && message.runId === runId);

  if (existingAssistant) {
    return {
      streamingMessageId: existingAssistant.id,
      messages: messagesWithPendingUser.map((message) =>
        message.id === existingAssistant.id
          ? { ...message, isStreaming: true }
          : message,
      ),
    };
  }

  const streamingMessageId = createId();
  return {
    streamingMessageId,
    messages: [
      ...messagesWithPendingUser,
      {
        id: streamingMessageId,
        role: "assistant",
        content: "",
        timestamp: new Date(),
        parts: [],
        isStreaming: true,
        runId,
      },
    ],
  };
}

/**
 * Get the last event timestamp from sorted events.
 */
// 取历史事件中最后一个带时间戳的事件时间（从尾部倒查）；无则返回 null。
// 用于设置「历史时间戳水位」，让实时流据此丢弃早于此刻的重复事件。
export function getLastEventTimestamp(events: HistoryEvent[]): Date | null {
  if (events.length === 0) return null;
  let lastEvent: HistoryEvent | null = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].timestamp) {
      lastEvent = events[i];
      break;
    }
  }
  return lastEvent?.timestamp ? parseDate(lastEvent.timestamp) : null;
}

/**
 * Extract the latest active goal from history events.
 *
 * Scans for the most recent `goal:start` / `goal:end` pair and reconstructs
 * an `ActiveGoalSpec` so the UI can show the goal indicator after a page
 * reload or session switch.
 */
// 从历史事件中提取「当前仍激活的目标」：顺序扫描 goal:start / goal:end 累积出最新目标状态，
// 供刷新页面/切换会话后恢复目标指示条。注意：已结束（有 ended_at）或无 objective 的目标返回 null（不恢复）。
export function extractGoalFromEvents(
  events: HistoryEvent[],
): ActiveGoalSpec | null {
  let goal: ActiveGoalSpec | null = null;

  for (const event of events) {
    const eventType = event.event_type;
    if (eventType !== "goal:start" && eventType !== "goal:end") continue;

    const data = event.data as Record<string, unknown> | null | undefined;
    if (!data) continue;

    const goalData = data.goal as Record<string, unknown> | undefined;
    const existing: ActiveGoalSpec = goal ?? {
      objective: "",
    };

    const next: ActiveGoalSpec = {
      objective: (goalData?.objective as string) ?? existing.objective ?? "",
      rubric: (goalData?.rubric as string) ?? existing.rubric,
      started_at: (data.started_at as string) ?? existing.started_at,
    };
    if (event.run_id) next.runId = event.run_id;
    else if (existing.runId) next.runId = existing.runId;
    if (goalData?.max_iterations != null)
      next.max_iterations = goalData.max_iterations as number;
    else if (existing.max_iterations != null)
      next.max_iterations = existing.max_iterations;

    if (eventType === "goal:end") {
      next.ended_at = (data.ended_at as string) ?? undefined;
    }

    goal = next;
  }

  // Don't restore completed goals — only show the bar for still-active ones.
  // 不恢复已完成的目标——仅为仍在进行中的目标显示指示条
  if (!goal || !goal.objective || goal.ended_at) return null;
  return goal;
}

// 按 run_id 分别提取每次运行的目标状态（含已结束的），返回 { runId: 目标 } 映射。
// 与 extractGoalFromEvents 不同：这里保留 ended_at，用于按历史 run 展示各自的目标信息。
export function extractGoalsByRunFromEvents(
  events: HistoryEvent[],
): Record<string, ActiveGoalSpec> {
  const goalsByRunId: Record<string, ActiveGoalSpec> = {};

  for (const event of events) {
    const eventType = event.event_type;
    if (eventType !== "goal:start" && eventType !== "goal:end") continue;
    if (!event.run_id) continue;

    const data = event.data as Record<string, unknown> | null | undefined;
    if (!data) continue;

    const goalData = data.goal as Record<string, unknown> | undefined;
    const existing: ActiveGoalSpec = goalsByRunId[event.run_id] ?? {
      objective: "",
      runId: event.run_id,
    };

    const next: ActiveGoalSpec = {
      objective: (goalData?.objective as string) ?? existing.objective ?? "",
      rubric: (goalData?.rubric as string) ?? existing.rubric,
      runId: event.run_id,
      started_at: (data.started_at as string) ?? existing.started_at,
    };
    if (goalData?.max_iterations != null)
      next.max_iterations = goalData.max_iterations as number;
    else if (existing.max_iterations != null)
      next.max_iterations = existing.max_iterations;

    if (eventType === "goal:end") {
      next.ended_at = (data.ended_at as string) ?? existing.ended_at;
    } else if (existing.ended_at) {
      next.ended_at = existing.ended_at;
    }

    if (next.objective) {
      goalsByRunId[event.run_id] = next;
    }
  }

  return goalsByRunId;
}
