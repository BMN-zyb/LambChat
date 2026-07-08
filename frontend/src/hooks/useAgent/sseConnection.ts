/**
 * SSE Connection utilities for useAgent hook
 * Handles SSE connection, reconnection, and stream management
 */
// 【SSE 连接与重连管理】本文件封装聊天流式通道的建立与自愈：
// - 使用 @microsoft/fetch-event-source 连接 GET /api/chat/sessions/{id}/stream；
// - 处理 401（刷新 token 后重连）、连接建立/消息/错误/关闭的各阶段回调；
// - 通过「终止事件（done/complete/带 run_id 的 error）」判定该正常收尾还是异常重连；
// - 采用指数退避 + 抖动的重连策略，避免雪崩；
// - useSSEReconnect 还监听页面可见性与网络 online/offline，在恢复时自动重连。

import { useCallback, useEffect } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { uuid } from "../../utils/uuid";
import { sessionApi } from "../../services/api";
import { buildApiUrl } from "../../services/api/config";
import {
  getValidAccessToken,
  refreshAccessToken,
} from "../../services/api/tokenManager";
import { getRefreshToken } from "../../services/api/token";
import type { EventType, StreamEvent } from "./types";
import { handleStreamEvent, type EventHandlerContext } from "./eventHandlers";
import { clearAllLoadingStates } from "./messageParts";
import type { Message, ConnectionStatus } from "../../types";

/**
 * SSE Connection context
 */
// SSE 连接过程中共享的一组可变 ref（在 React 渲染之外保存连接状态）：
export interface SSEConnectionContext extends EventHandlerContext {
  // 当前连接的中止控制器，用于主动断开/切换连接
  abortControllerRef: React.MutableRefObject<AbortController | null>;
  // 连接进行中标志，防止重复发起连接
  isConnectingRef: React.MutableRefObject<boolean>;
  // 当前正在流式接收的 assistant 消息 ID
  streamingMessageIdRef: React.MutableRefObject<string | null>;
  // 待执行的重连定时器句柄
  reconnectTimeoutRef: React.MutableRefObject<ReturnType<
    typeof setTimeout
  > | null>;
  // 重连累计次数，用于计算指数退避延迟
  retryCountRef: React.MutableRefObject<number>;
  // 最新消息列表的 ref 镜像，便于在回调中读取不受闭包过期影响的最新值
  messagesRef: React.MutableRefObject<Message[]>;
}

/**
 * Exponential backoff for reconnection
 */
// 计算重连延迟（毫秒）：以 2^retryCount 秒为基数、上限 30 秒，再叠加最多 1 秒的随机抖动，
// 从而实现指数退避并打散并发重连，避免大量客户端同时重连造成服务端雪崩。
export function getReconnectDelay(retryCount: number): number {
  const baseDelay = Math.min(Math.pow(2, retryCount), 30) * 1000;
  const jitter = Math.random() * 1000;
  return baseDelay + jitter;
}

/**
 * Clear reconnect timeout
 */
// 清除待执行的重连定时器（若存在），并将句柄置空，避免重复触发。
export function clearReconnectTimeout(
  reconnectTimeoutRef: React.MutableRefObject<ReturnType<
    typeof setTimeout
  > | null>,
): void {
  if (reconnectTimeoutRef.current) {
    clearTimeout(reconnectTimeoutRef.current);
    reconnectTimeoutRef.current = null;
  }
}

// SSE 连接关闭后的处置动作：terminal = 正常收尾（已收到终止事件）；retry = 需要重连。
export type SSECloseAction = "terminal" | "retry";

// 类型守卫：判断值是否为非 null 的对象，供下方安全读取字段使用。
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

// 判断一个 error 事件的载荷是否代表「真正的终止性错误」而非中途的传输抖动。
// 约定：只要携带了 type / run_id / trace_id 之一，就认为它是业务侧下发的最终错误，
// 应当结束本次流；否则视为传输层错误，走重连逻辑。
function isTerminalErrorPayload(data: unknown): boolean {
  if (!isRecord(data)) {
    return false;
  }

  return (
    typeof data.type === "string" ||
    typeof data.run_id === "string" ||
    typeof data.trace_id === "string"
  );
}

// 终止事件判定：done / complete 一定是终止；error 需进一步看载荷是否为终止性错误。
// 只有收到终止事件，连接关闭时才按「正常结束」处理，否则触发重连。
export function isTerminalSSEEvent(eventType: string, data?: unknown): boolean {
  if (eventType === "done" || eventType === "complete") {
    return true;
  }

  if (eventType === "error") {
    return isTerminalErrorPayload(data);
  }

  return false;
}

// 依据「是否已收到终止事件」决定关闭后的动作：收到则收尾，否则重连。
export function getSSECloseAction({
  receivedTerminalEvent,
}: {
  receivedTerminalEvent: boolean;
}): SSECloseAction {
  return receivedTerminalEvent ? "terminal" : "retry";
}

/**
 * Connect to SSE stream
 */
// 建立一次 SSE 连接并处理其完整生命周期。
// 参数：targetSessionId 会话 ID、targetRunId 本次运行 ID、messageId 承接流式内容的消息 ID、
// ctx 连接上下文（各类 ref 与状态 setter）、hasRetried 是否已因 401 刷新 token 重试过（防止死循环）。
// 关键流程：去重 → 中止旧连接 → 取 token → onopen 处理 401/握手 → onmessage 解析并分发事件、
// 标记终止事件 → onerror/onclose 根据是否收到终止事件决定重连或收尾。
export async function connectToSSE(
  targetSessionId: string,
  targetRunId: string,
  messageId: string,
  ctx: SSEConnectionContext,
  hasRetried = false,
): Promise<void> {
  const {
    abortControllerRef,
    isConnectingRef,
    streamingMessageIdRef,
    setConnectionStatus,
    retryCountRef,
  } = ctx;

  // 若已有连接正在建立则直接跳过，避免并发重复连接
  if (isConnectingRef.current) {
    console.log("[SSE] Connection already in progress, skipping...");
    return;
  }
  isConnectingRef.current = true;
  streamingMessageIdRef.current = messageId;

  // 中止上一个连接（若存在），再创建新的中止控制器来接管本次连接
  if (abortControllerRef.current) {
    abortControllerRef.current.abort();
  }
  abortControllerRef.current = new AbortController();

  // 取有效 access token，附加到 Authorization 头（无 token 则匿名连接）
  const token = await getValidAccessToken();
  const headers: Record<string, string> = {};
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  console.log(
    `[SSE] Connecting: session=${targetSessionId}, run_id=${targetRunId}`,
  );

  // 标记本次流是否已收到终止事件（决定关闭时是收尾还是重连）
  let receivedTerminalEvent = false;

  setConnectionStatus("connecting");
  retryCountRef.current = 0;

  try {
    await fetchEventSource(
      buildApiUrl(
        `/api/chat/sessions/${targetSessionId}/stream?run_id=${targetRunId}`,
      ),
      {
        headers,
        signal: abortControllerRef.current.signal,
        // 即使标签页处于后台也保持连接，避免切走后丢失流式内容
        openWhenHidden: true,
        // 握手回调：处理鉴权与 HTTP 状态
        onopen: async (response) => {
          // 401 未授权：首次失败则刷新 token 后重连一次；已重试过或无 refresh token 则放弃
          if (response.status === 401) {
            if (hasRetried) {
              // refreshAccessToken() in the first attempt already handled redirect
              // if needed, so just abort and throw
              throw new Error("SSE unauthorized after token refresh");
            }
            if (!getRefreshToken()) {
              throw new Error("SSE unauthorized: no refresh token");
            }
            try {
              await refreshAccessToken();
            } catch {
              throw new Error("SSE unauthorized: token refresh failed");
            }
            abortControllerRef.current?.abort();
            isConnectingRef.current = false;
            // 携带 hasRetried=true 递归重连，确保至多因 401 重试一次
            await connectToSSE(
              targetSessionId,
              targetRunId,
              messageId,
              ctx,
              true,
            );
            return;
          }
          // 其它非 2xx 状态直接抛错
          if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
          }
          // 握手成功：标记已连接并重置重试计数
          console.log("[SSE] Connection established");
          setConnectionStatus("connected");
          retryCountRef.current = 0;
        },
        // 消息回调：过滤心跳、解析 JSON、识别终止/错误事件并分发给事件处理器
        onmessage: (event) => {
          // 心跳事件，忽略
          if (event.event === "ping") return;
          const eventId = event.id || uuid();
          let parsedData: Record<string, unknown>;
          try {
            parsedData = JSON.parse(event.data);
          } catch {
            // Ignore parse errors
            return;
          }
          // 非终止性 error 视为传输层抖动：切到重连中并抛错，交由重连逻辑处理
          if (
            event.event === "error" &&
            !isTerminalSSEEvent(event.event, parsedData)
          ) {
            setConnectionStatus("reconnecting");
            throw new Error("SSE transport error before terminal event");
          }
          // 收到终止事件（done/complete/终止性 error）则置位，供 onclose 判定正常收尾
          if (isTerminalSSEEvent(event.event, parsedData)) {
            receivedTerminalEvent = true;
          }
          // 组装为统一的 StreamEvent，交给 handleStreamEvent 转换成消息 parts
          const timestamp = parsedData._timestamp as string | undefined;
          const streamEvent: StreamEvent = {
            event: event.event as EventType,
            data: event.data,
          };
          handleStreamEvent(streamEvent, messageId, eventId, timestamp, ctx);
        },
        // 传输错误回调：标记重连中（返回值不抛错时 fetch-event-source 会自动重试）
        onerror: (err) => {
          console.error("[SSE] Connection error:", err);
          setConnectionStatus("reconnecting");
        },
        // 关闭回调：依据是否已收到终止事件决定重连还是收尾
        onclose: () => {
          console.log("[SSE] Connection closed");
          const closeAction = getSSECloseAction({ receivedTerminalEvent });
          // 未收到终止事件却被关闭：抛错以触发重连
          if (closeAction === "retry") {
            setConnectionStatus("reconnecting");
            throw new Error("SSE closed before terminal event");
          }
          // 正常收尾：置为断开、结束沙箱初始化态，并清除该消息上所有 loading 占位
          setConnectionStatus("disconnected");
          isConnectingRef.current = false;
          ctx.setIsInitializingSandbox(false);
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
        },
      },
    );
  } catch (err) {
    // 主动中止（切换连接或组件卸载）不算异常，直接返回
    if (err instanceof Error && err.name === "AbortError") {
      console.log("[SSE] Connection aborted");
      return;
    }
    console.error("[SSE] Connection error:", err);
    setConnectionStatus("disconnected");
  } finally {
    isConnectingRef.current = false;
  }
}

/**
 * Smart reconnect with exponential backoff
 */
// 智能重连：从 ref 读取当前会话/运行/消息，先向后端查询任务状态，
// 若任务已结束则直接收尾，否则按指数退避延迟后重新建立 SSE 连接。
// 相比直接重连，这里多了「状态探测」以避免为已完成的任务白白重连。
export async function reconnectSSE(
  ctx: SSEConnectionContext & {
    sessionIdRef: React.MutableRefObject<string | null>;
    currentRunIdRef: React.MutableRefObject<string | null>;
    isReconnectFromHistoryRef: React.MutableRefObject<boolean>;
  },
): Promise<void> {
  const {
    sessionIdRef,
    currentRunIdRef,
    streamingMessageIdRef,
    abortControllerRef,
    isConnectingRef,
    reconnectTimeoutRef,
    retryCountRef,
    messagesRef,
    isReconnectFromHistoryRef,
    setConnectionStatus,
  } = ctx;

  const currentSessId = sessionIdRef.current;
  const currentRId = currentRunIdRef.current;
  const currentMsgId = streamingMessageIdRef.current;

  // 缺少会话或运行 ID 时无法重连，直接返回
  if (!currentSessId || !currentRId) {
    console.log("[SSE] No session/run ID, skipping reconnect");
    return;
  }

  // 清除已排队的重连定时器，避免重复调度
  clearReconnectTimeout(reconnectTimeoutRef);

  // 中止当前连接并清空控制器
  if (abortControllerRef.current) {
    abortControllerRef.current.abort();
    abortControllerRef.current = null;
  }

  isConnectingRef.current = false;

  // 重连前先探测任务状态：若已完成/出错则无需重连，直接收尾并清除消息 loading 态
  try {
    const statusData = await sessionApi.getStatus(currentSessId, currentRId);
    if (statusData.status === "completed" || statusData.status === "error") {
      console.log("[SSE] Task already completed");
      setConnectionStatus("disconnected");
      ctx.setIsInitializingSandbox(false);
      streamingMessageIdRef.current = null;
      // Clear loading states on the message
      if (currentMsgId) {
        ctx.setMessages((prev) =>
          prev.map((m) =>
            m.id === currentMsgId
              ? {
                  ...m,
                  isStreaming: false,
                  parts: clearAllLoadingStates(m.parts || []),
                }
              : m,
          ),
        );
      }
      return;
    }
  } catch (err) {
    console.error("[SSE] Failed to check task status:", err);
  }

  setConnectionStatus("reconnecting");

  // 任务仍在进行：按当前重试次数计算退避延迟，并把重试计数加一
  const delay = getReconnectDelay(retryCountRef.current);
  retryCountRef.current += 1;
  console.log(
    `[SSE] Scheduling reconnect in ${delay}ms (retry ${retryCountRef.current})`,
  );

  // 延迟到点后发起重连：确认目标消息仍存在，并标记本次为「来自历史的重连」
  reconnectTimeoutRef.current = setTimeout(async () => {
    if (currentMsgId) {
      const msgs = messagesRef.current;
      const lastMsg = msgs.find((m) => m.id === currentMsgId);
      if (lastMsg) {
        isReconnectFromHistoryRef.current = true;
        await connectToSSE(currentSessId, currentRId, currentMsgId, ctx);
      }
    }
  }, delay);
}

/**
 * Options for the useSSEReconnect hook
 */
// useSSEReconnect 的入参：提供构造连接上下文的工厂、各类 ref 与当前连接状态/更新器。
export interface SSEReconnectOptions {
  createSSEContext: () => SSEConnectionContext;
  sessionIdRef: React.MutableRefObject<string | null>;
  currentRunIdRef: React.MutableRefObject<string | null>;
  isReconnectFromHistoryRef: React.MutableRefObject<boolean>;
  streamingMessageIdRef: React.MutableRefObject<string | null>;
  connectionStatus: ConnectionStatus;
  setConnectionStatus: (status: ConnectionStatus) => void;
}

/**
 * Hook that manages SSE reconnection on visibility change and network events.
 * Returns a handleReconnectSSE function for manual use.
 */
// 管理 SSE 自动重连的 hook：监听「标签页重新可见」与「网络恢复 online」两类时机，
// 在连接已断开且存在进行中的流时自动触发重连；同时返回可供手动调用的重连函数。
export function useSSEReconnect(
  opts: SSEReconnectOptions,
): () => Promise<void> {
  const {
    createSSEContext,
    sessionIdRef,
    currentRunIdRef,
    isReconnectFromHistoryRef,
    streamingMessageIdRef,
    connectionStatus,
    setConnectionStatus,
  } = opts;

  // 手动重连入口：合并连接上下文与会话/运行 ref 后委托给 reconnectSSE
  const handleReconnectSSE = useCallback(async () => {
    const ctx = {
      ...createSSEContext(),
      sessionIdRef,
      currentRunIdRef,
      isReconnectFromHistoryRef,
    };
    await reconnectSSE(ctx);
  }, [
    createSSEContext,
    sessionIdRef,
    currentRunIdRef,
    isReconnectFromHistoryRef,
  ]);

  // Handle visibility change — reconnect when tab becomes visible
  // 监听页面可见性：标签页重新可见且连接已断开、又确有进行中的流时自动重连
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (
        document.visibilityState === "visible" &&
        connectionStatus === "disconnected" &&
        sessionIdRef.current &&
        currentRunIdRef.current &&
        streamingMessageIdRef.current
      ) {
        handleReconnectSSE();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [
    connectionStatus,
    handleReconnectSSE,
    sessionIdRef,
    currentRunIdRef,
    streamingMessageIdRef,
  ]);

  // Handle network status changes — reconnect on online, mark disconnected on offline
  // 监听网络状态：online 时在有进行中的流的前提下重连；offline 时直接标记为断开
  useEffect(() => {
    const handleOnline = () => {
      if (
        connectionStatus === "disconnected" &&
        sessionIdRef.current &&
        currentRunIdRef.current &&
        streamingMessageIdRef.current
      ) {
        handleReconnectSSE();
      }
    };

    const handleOffline = () => {
      setConnectionStatus("disconnected");
    };

    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);

    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, [
    connectionStatus,
    handleReconnectSSE,
    sessionIdRef,
    currentRunIdRef,
    streamingMessageIdRef,
    setConnectionStatus,
  ]);

  return handleReconnectSSE;
}
