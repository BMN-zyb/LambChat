/**
 * Stream event handlers for useAgent hook
 * Handles all incoming SSE events and updates messages accordingly.
 *
 * Message transformation logic is unified in processMessageEvent (messageParts.ts).
 * This file handles: SSE parsing, duplicate detection, subagent stack management,
 * and React state updates (side effects).
 */
// 【SSE 事件的总调度层（副作用侧）】
// 每个到达的 SSE 事件都先经过 handleStreamEvent：
// 1) 去重（按事件 ID / 时间戳），防止重连时重复消费；
// 2) 用 streamVersion 丢弃「清空消息后仍在途」的过期事件；
// 3) 纯副作用类事件（metadata/goal/complete/审批/技能变更等）就地处理并 return；
// 4) 需要改写消息内容的事件统一委托给 processMessageEvent（messageParts.ts）转成 parts；
// 5) 维护子 agent 调用栈、沙箱状态、错误态等副作用。
// 简言之：本文件负责「解析 + 副作用 + React 状态更新」，纯粹的「事件→parts」转换在 eventProcessor。

import type { Message, MessagePart, FormField } from "../../types";
import { uuid } from "../../utils/uuid";
import { authFetch } from "../../services/api/fetch";
import { buildApiUrl } from "../../services/api/config";
import { sessionApi } from "../../services/api/session";
import i18n from "../../i18n";
import { translateBackendError } from "../../utils/backendErrors";
import { parseDate } from "../../utils/datetime";
import type {
  StreamEvent,
  EventData,
  SubagentStackItem,
  UseAgentOptions,
} from "./types";
import { clearAllLoadingStates } from "./messageParts";
import { convertAttachments, processMessageEvent } from "./eventProcessor";
import { dispatchToolMutationRefresh } from "../../components/chat/ChatMessage/items/toolMutationEvents";

/**
 * Context passed to event handler
 */
// 事件处理所需的上下文：既有跨事件保持的可变 ref，也有更新 React 状态的 setter。
// 关键字段：
// - processedEventIdsRef：已处理事件 ID 集合，用于去重（重连会重发历史事件）；
// - lastHistoryTimestampRef：已消费的最新时间戳水位，早于它的事件视为重复而丢弃；
// - activeSubagentStackRef：当前嵌套的子 agent 调用栈；
// - streamVersionRef：流版本号，clearMessages 时自增，用于丢弃在途的过期事件。
export interface EventHandlerContext {
  options?: UseAgentOptions;
  sessionIdRef: React.MutableRefObject<string | null>;
  processedEventIdsRef: React.MutableRefObject<Set<string>>;
  lastHistoryTimestampRef: React.MutableRefObject<Date | null>;
  activeSubagentStackRef: React.MutableRefObject<SubagentStackItem[]>;
  streamVersionRef: React.MutableRefObject<number>;
  setSessionId: (id: string) => void;
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  setConnectionStatus: (status: string) => void;
  setIsInitializingSandbox: (loading: boolean) => void;
  setSandboxError: (error: string | null) => void;
  setActiveGoal: React.Dispatch<
    React.SetStateAction<import("./types").ActiveGoalSpec | null>
  >;
  setGoalsByRunId: React.Dispatch<
    React.SetStateAction<Record<string, import("./types").ActiveGoalSpec>>
  >;
}

/**
 * Handle incoming SSE events
 */
// 处理单个 SSE 事件（在 sseConnection 的 onmessage 中被调用）。
// 参数：event 原始事件、messageId 当前流式 assistant 消息 ID、eventId 用于去重、
// eventTimestamp 事件时间戳、ctx 处理上下文。无返回值，全部通过副作用更新状态。
export function handleStreamEvent(
  event: StreamEvent,
  messageId: string,
  eventId: string,
  eventTimestamp: string | undefined,
  ctx: EventHandlerContext,
): void {
  console.log("[handleStreamEvent] Received event:", {
    eventType: event.event,
    messageId,
    eventId,
  });

  // 去重①：按事件 ID 跳过已处理事件（重连时后端可能重发同一事件）
  // Skip if already processed by ID
  if (ctx.processedEventIdsRef.current.has(eventId)) {
    console.log("[SSE] Skipping duplicate event by ID:", eventId);
    return;
  }

  // 去重②：早于「历史时间戳水位」的事件视为已消费过的旧事件，丢弃
  // Skip if this event is older than the last history timestamp
  if (eventTimestamp && ctx.lastHistoryTimestampRef.current) {
    const eventTime = parseDate(eventTimestamp);
    const historyTime = ctx.lastHistoryTimestampRef.current;
    if (eventTime < historyTime) {
      console.log(
        "[SSE] Skipping duplicate event by timestamp:",
        eventId,
        eventTime.toISOString(),
        "<=",
        historyTime.toISOString(),
      );
      return;
    }
  }

  // 标记该事件已处理，并把时间戳水位推进到目前见过的最大值
  ctx.processedEventIdsRef.current.add(eventId);
  if (eventTimestamp) {
    const eventTime = parseDate(eventTimestamp);
    const previousTime = ctx.lastHistoryTimestampRef.current;
    if (!previousTime || eventTime > previousTime) {
      ctx.lastHistoryTimestampRef.current = eventTime;
    }
  }

  // 限制去重集合大小，避免长时间流式导致内存无上限增长（清空是安全的，见下方英文说明）
  // Cap the dedup set to prevent unbounded memory growth during long streams.
  // Safe to clear: event dedup is only needed within a single streaming session,
  // and the set is fully cleared on loadHistory/sendMessage/clearMessages.
  if (ctx.processedEventIdsRef.current.size > 10_000) {
    ctx.processedEventIdsRef.current.clear();
  }

  // 在处理事件的此刻快照流版本号：若期间调用过 clearMessages（版本自增），
  // 则这些「在途的旧事件」应被丢弃（见下方 stale 检查）
  // Capture stream version at event processing time to detect stale events.
  // If clearMessages() was called while SSE events were still in-flight,
  // the version will have been incremented and these stale events should be dropped.
  const streamVersion = ctx.streamVersionRef.current;

  // 解析事件类型与 JSON 载荷（解析失败则用空对象兜底，避免抛错中断流）
  const eventType = event.event;
  let data: EventData = {};
  try {
    data = JSON.parse(event.data);
  } catch {
    // Fallback for non-JSON data
  }

  // 嵌套深度：>0 表示该事件来自子 agent（用于层级渲染与子 agent 栈匹配）
  const depth = data.depth || 0;

  // Events handled entirely by side effects (no message transformation)
  // 第一类：纯副作用事件，就地处理后直接 return，不进入 parts 转换流程
  switch (eventType) {
    // 元数据：仅在尚未确定会话 ID 且流版本未变时，回填 session_id
    case "metadata": {
      if (
        data.session_id &&
        !ctx.sessionIdRef.current &&
        ctx.streamVersionRef.current === streamVersion
      ) {
        ctx.setSessionId(data.session_id);
      }
      return;
    }

    // 目标模式开始：更新当前激活目标，并按 run_id 归档目标（缺失字段回退到已有值）
    case "goal:start": {
      ctx.setActiveGoal((prev) => {
        const goal: import("./types").ActiveGoalSpec = {
          objective: data.goal?.objective ?? prev?.objective ?? "",
          rubric: data.goal?.rubric ?? prev?.rubric,
          started_at: data.started_at ?? prev?.started_at,
        };
        if (data.run_id) goal.runId = data.run_id;
        else if (prev?.runId) goal.runId = prev.runId;
        if (data.goal?.max_iterations != null)
          goal.max_iterations = data.goal.max_iterations;
        else if (prev?.max_iterations != null)
          goal.max_iterations = prev.max_iterations;
        return goal;
      });
      if (data.run_id) {
        ctx.setGoalsByRunId((prev) => ({
          ...prev,
          [data.run_id!]: {
            objective:
              data.goal?.objective ?? prev[data.run_id!]?.objective ?? "",
            rubric: data.goal?.rubric ?? prev[data.run_id!]?.rubric,
            ...(data.goal?.max_iterations != null
              ? { max_iterations: data.goal.max_iterations }
              : prev[data.run_id!]?.max_iterations != null
                ? { max_iterations: prev[data.run_id!]!.max_iterations }
                : {}),
            runId: data.run_id,
            started_at: data.started_at ?? prev[data.run_id!]?.started_at,
          },
        }));
      }
      return;
    }

    // 目标模式结束：补齐 ended_at，并在短暂展示完成态后自动清除目标标签
    case "goal:end": {
      ctx.setActiveGoal((prev) => {
        const goal: import("./types").ActiveGoalSpec = {
          objective: data.goal?.objective ?? prev?.objective ?? "",
          rubric: data.goal?.rubric ?? prev?.rubric,
          started_at: data.started_at ?? prev?.started_at,
          ended_at: data.ended_at,
        };
        if (data.run_id) goal.runId = data.run_id;
        else if (prev?.runId) goal.runId = prev.runId;
        if (data.goal?.max_iterations != null)
          goal.max_iterations = data.goal.max_iterations;
        else if (prev?.max_iterations != null)
          goal.max_iterations = prev.max_iterations;
        return goal;
      });
      if (data.run_id) {
        ctx.setGoalsByRunId((prev) => ({
          ...prev,
          [data.run_id!]: {
            objective:
              data.goal?.objective ?? prev[data.run_id!]?.objective ?? "",
            rubric: data.goal?.rubric ?? prev[data.run_id!]?.rubric,
            ...(data.goal?.max_iterations != null
              ? { max_iterations: data.goal.max_iterations }
              : prev[data.run_id!]?.max_iterations != null
                ? { max_iterations: prev[data.run_id!]!.max_iterations }
                : {}),
            runId: data.run_id,
            started_at: data.started_at ?? prev[data.run_id!]?.started_at,
            ended_at: data.ended_at ?? prev[data.run_id!]?.ended_at,
          },
        }));
      }
      // Auto-dismiss the goal chip after a short delay so the user sees
      // the completed state briefly before it disappears.
      // 短暂延迟后自动清除目标标签，让用户先看到「已完成」再消失
      setTimeout(() => ctx.setActiveGoal(null), 2000);
      return;
    }

    // 用户消息回显：把后端确认的用户消息合并进列表（含乐观消息的对齐，见下方函数）
    case "user:message": {
      handleUserMessage(data, messageId, eventTimestamp, ctx);
      return;
    }

    // 用户取消：按「已取消」处理该消息，但保持连接开启（后续可能还有收尾事件）
    case "user:cancel": {
      handleError(data, messageId, ctx, true, { keepConnectionOpen: true });
      return;
    }

    // 完成/结束：结束流式态、清除 loading 占位、断开连接、标记已读并回调 onStreamDone
    case "complete":
    case "done": {
      ctx.setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? {
                ...m,
                isStreaming: false,
                parts: clearAllLoadingStates(m.parts || []),
              }
            : m,
        ),
      );
      ctx.setConnectionStatus("disconnected");
      // AI 回复完成，用户正在查看当前 session，立即标记为已读
      const activeSessionId = ctx.sessionIdRef.current;
      if (activeSessionId) {
        sessionApi.markRead(activeSessionId).catch(() => {});
      }
      ctx.options?.onStreamDone?.();
      return;
    }

    // 排队更新：当状态变为 processing（轮到本次请求）时提示「开始处理」
    case "queue_update": {
      if (data.status === "processing") {
        import("react-hot-toast").then(({ default: toast }) => {
          toast.dismiss("chat-queue");
          toast.success(i18n.t("chat.queueStart"), { duration: 2000 });
        });
      }
      return;
    }

    // 人工审批请求：拉取审批详情并回调外层弹出审批 UI（见下方函数）
    case "approval_required": {
      handleApprovalRequired(data, ctx);
      return;
    }

    // 技能集变更：通知外层某技能被创建/更新，用于刷新技能列表并提示
    case "skills:changed": {
      if (ctx.options?.onSkillAdded) {
        const action = (data.action as string) || "updated";
        const description =
          action === "created"
            ? i18n.t("chat.skillCreated")
            : i18n.t("chat.skillUpdated");
        ctx.options.onSkillAdded(
          (data.skill_name as string) || "",
          description,
          (data.files_count as number) || 0,
        );
      }
      return;
    }
  }

  // Drop stale events if clearMessages() was called mid-stream
  // 若处理期间流版本已改变（clearMessages 被调用），说明是过期事件，丢弃
  if (ctx.streamVersionRef.current !== streamVersion) {
    return;
  }

  // Only process known message-transforming event types
  // 第二类：会改写消息内容的事件白名单；未知事件仅告警不处理
  const MESSAGE_EVENTS = new Set([
    "agent:call",
    "agent:result",
    "thinking",
    "message:chunk",
    "tool:start",
    "tool:result",
    "artifact:result",
    "sandbox:starting",
    "sandbox:ready",
    "sandbox:error",
    "token:usage",
    "todo:updated",
    "summary",
    "recommend:questions",
    "followup:questions",
    "error",
  ]);
  if (!MESSAGE_EVENTS.has(eventType)) {
    console.warn("[SSE] Unhandled event type:", eventType);
    return;
  }

  // Events that transform message state via processMessageEvent
  const subagentStack = ctx.activeSubagentStackRef.current;

  // Manage subagent stack as side effect
  // 子 agent 调用开始：入栈，后续事件据栈顶归属到对应子 agent 的嵌套层级
  if (eventType === "agent:call") {
    const agentId = data.agent_id || "unknown";
    subagentStack.push({ agent_id: agentId, depth, message_id: messageId });
  }

  // 只更新目标消息：把本事件交给 processMessageEvent 合并进 parts/content/toolCalls，
  // 并把可选的工具结果/token 用量/耗时/取消态回写到消息上
  ctx.setMessages((prev) =>
    prev.map((m) => {
      if (m.id !== messageId) return m;

      const result = processMessageEvent(
        eventType,
        data,
        m.parts || [],
        m.content,
        m.toolCalls || [],
        depth,
        subagentStack,
        true, // isStreaming
        messageId,
      );

      const updated = {
        ...m,
        parts: result.parts,
        content: result.content,
        toolCalls: result.toolCalls,
      };

      if (result.toolResult) {
        updated.toolResults = [...(m.toolResults || []), result.toolResult];
      }
      if (result.tokenUsage) {
        updated.tokenUsage = result.tokenUsage;
      }
      if (result.duration) {
        updated.duration = result.duration;
      }
      if (result.cancelled) {
        updated.isStreaming = false;
        updated.cancelled = true;
      }

      return updated;
    }),
  );

  // Pop subagent stack after agent:result
  // 子 agent 调用结束：按 agent_id + message_id 从栈中移除对应项
  if (eventType === "agent:result") {
    const agentId = data.agent_id || "unknown";
    const stackIndex = subagentStack.findIndex(
      (item) => item.agent_id === agentId && item.message_id === messageId,
    );
    if (stackIndex !== -1) {
      subagentStack.splice(stackIndex, 1);
    }
  }

  // 工具执行成功（非失败）：广播「数据可能已变更」，让相关视图（如文件树）刷新
  if (eventType === "tool:result" && data.success !== false) {
    dispatchToolMutationRefresh(data.result);
  }

  // Sandbox side effects
  // 沙箱副作用：启动中→显示初始化态；就绪→结束初始化态；出错→结束并记录错误
  if (eventType === "sandbox:starting") {
    ctx.setIsInitializingSandbox(true);
    ctx.setSandboxError(null);
  }
  if (eventType === "sandbox:ready") {
    ctx.setIsInitializingSandbox(false);
  }
  if (eventType === "sandbox:error") {
    ctx.setIsInitializingSandbox(false);
    ctx.setSandboxError(data.error || i18n.t("chat.sandboxInitFailed"));
  }

  // Error side effects
  // 错误副作用：断开连接、结束沙箱初始化态并清空待审批项（错误内容由 processMessageEvent 写入 parts）
  if (eventType === "error") {
    ctx.setConnectionStatus("disconnected");
    ctx.setIsInitializingSandbox(false);
    ctx.options?.onClearApprovals?.();
  }
}

// ---- Events handled outside processMessageEvent ----
// ---- 以下事件不走 processMessageEvent，单独处理 ----

// 处理 user:message 事件：把后端确认的用户消息并入列表。
// 难点在于与「乐观消息」对齐——发送时前端已先插入一条本地用户消息，此处需避免重复：
// 若能匹配到内容相同（或去掉前缀标记后相同）的已存在消息，则原地更新而非新增。
function handleUserMessage(
  data: EventData,
  _messageId: string,
  eventTimestamp: string | undefined,
  ctx: EventHandlerContext,
): void {
  // 从形如「[标记] 正文」的内容中提取乐观消息的原始正文，用于与本地乐观消息比对
  const extractOptimisticContent = (content: string): string | null => {
    const match = content.match(/^\[[^\]]+\]\s([\s\S]*)$/);
    return match ? match[1] : null;
  };
  // 解析消息 ID：优先用后端 message_id，其次用 `${run_id}:user`，都没有则临时生成
  const resolvedMessageId =
    typeof data.message_id === "string" && data.message_id.trim()
      ? data.message_id
      : typeof data.run_id === "string" && data.run_id.trim()
        ? `${data.run_id}:user`
        : uuid();
  const userContent = data.content || "";
  const userAttachments = convertAttachments(data.attachments) || [];
  const enabledSkills = Array.isArray(data.enabled_skills)
    ? data.enabled_skills
    : undefined;

  if (userContent) {
    ctx.setMessages((prev) => {
      // 列表为空：直接作为首条用户消息插入
      if (prev.length === 0) {
        const newUserMessage: Message = {
          id: resolvedMessageId,
          role: "user",
          content: userContent,
          timestamp: eventTimestamp ? parseDate(eventTimestamp) : new Date(),
          attachments: userAttachments,
          enabledSkills,
        };
        return [...prev, newUserMessage];
      }
      // 已存在完全相同内容的用户消息：视为重复，直接跳过
      const existingUserMsg = prev.find(
        (m) => m.role === "user" && m.content === userContent,
      );
      if (existingUserMsg) return prev;

      // 尝试与乐观消息对齐：若去掉前缀标记后的正文能匹配到已插入的乐观消息，
      // 则就地把它替换为后端确认版本（更新内容/附件/技能），避免出现两条重复消息
      const optimisticContent = extractOptimisticContent(userContent);
      if (optimisticContent) {
        for (let index = prev.length - 1; index >= 0; index -= 1) {
          const candidate = prev[index];
          if (
            candidate?.role === "user" &&
            candidate.content === optimisticContent
          ) {
            const updatedMessages = [...prev];
            updatedMessages[index] = {
              ...candidate,
              content: userContent,
              attachments:
                userAttachments.length > 0
                  ? userAttachments
                  : candidate.attachments,
              enabledSkills,
            };
            return updatedMessages;
          }
        }
      }

      // 未匹配到乐观消息：作为新用户消息插入。
      // 若此刻已有正在流式的 assistant 消息，则把用户消息插到它之前，保持时序正确
      const newUserMessage: Message = {
        id: resolvedMessageId,
        role: "user",
        content: userContent,
        timestamp: eventTimestamp ? parseDate(eventTimestamp) : new Date(),
        attachments: userAttachments,
        enabledSkills,
      };
      const streamingAssistantIndex = prev.findIndex(
        (m) => m.role === "assistant" && m.isStreaming,
      );
      if (streamingAssistantIndex !== -1) {
        const newMessages = [...prev];
        newMessages.splice(streamingAssistantIndex, 0, newUserMessage);
        return newMessages;
      }
      return [...prev, newUserMessage];
    });
  }
}

// 处理错误 / 取消：把目标消息标记为结束。
// forceCancelled 或后端类型为 CancelledError 时按「已取消」展示（追加 cancelled part），
// 否则将消息内容替换为带前缀的错误提示。keepConnectionOpen 控制是否顺带断开连接。
function handleError(
  data: EventData,
  messageId: string,
  ctx: EventHandlerContext,
  forceCancelled?: boolean,
  options?: { keepConnectionOpen?: boolean },
): void {
  const errorMsg = data.error
    ? translateBackendError(data.error, i18n.t.bind(i18n))
    : i18n.t("chat.unknownError");
  const isCancelled = forceCancelled || data.type === "CancelledError";

  ctx.setMessages((prev) =>
    prev.map((m) => {
      if (m.id !== messageId) return m;
      if (isCancelled) {
        return {
          ...m,
          isStreaming: false,
          cancelled: true,
          parts: appendCancelledPart(clearAllLoadingStates(m.parts || [])),
        };
      }
      return {
        ...m,
        content: i18n.t("chat.errorPrefix", { error: errorMsg }),
        isStreaming: false,
        parts: clearAllLoadingStates(m.parts || []),
      };
    }),
  );
  if (!options?.keepConnectionOpen) {
    ctx.setConnectionStatus("disconnected");
    ctx.setIsInitializingSandbox(false);
  }
  ctx.options?.onClearApprovals?.();
}

// 若 parts 中尚无 cancelled 标记，则追加一个「已取消」part（幂等，避免重复追加）
function appendCancelledPart(parts: MessagePart[]): MessagePart[] {
  if (parts.some((part) => part.type === "cancelled")) {
    return parts;
  }
  return [...parts, { type: "cancelled" }];
}

// 处理 approval_required 事件：按审批 ID 拉取审批详情，
// 仅当仍处于 pending 状态时才回调外层弹出审批 UI（超时/已处理则忽略）。
async function handleApprovalRequired(
  data: EventData,
  ctx: EventHandlerContext,
): Promise<void> {
  if (data.id && ctx.options?.onApprovalRequired) {
    try {
      const approval = await authFetch<{
        status: string;
        message?: string;
        type?: string;
        fields?: FormField[];
        expires_at?: string | null;
        metadata?: Record<string, unknown>;
      }>(buildApiUrl(`/human/${data.id}`));
      if (!approval) return;
      if (approval && approval.status === "pending") {
        ctx.options?.onApprovalRequired?.({
          id: data.id!,
          message: approval.message || "",
          type: approval.type || "form",
          fields: approval.fields || [],
          expires_at: approval.expires_at || null,
          timeout: (data as Record<string, unknown>).timeout as
            | number
            | undefined,
          metadata: approval.metadata,
        });
      }
    } catch (err) {
      console.warn("[SSE] Failed to check approval status:", err);
    }
  }
}
