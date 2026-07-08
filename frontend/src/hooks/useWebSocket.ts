// 【任务完成通知的 WebSocket 连接】
// 与 useAgent 的 SSE（逐条消息的流式内容）不同，本 hook 连接 /ws，只负责接收「后台任务完成」的
// 全局通知 task:complete（例如定时任务/异步长任务跑完），用于更新未读数、弹通知等。
// 内含：连接去重与竞态防护、连接后发送 token 鉴权、指数退避重连、以及针对 401 的特殊处理
// （连续鉴权失败达到上限后进入长冷却而非彻底停连）。

import { useEffect, useRef, useCallback, useState } from "react";
import {
  getValidAccessToken,
  refreshAccessToken,
} from "../services/api/tokenManager";
import { getRefreshToken } from "../services/api";
import { buildWebSocketUrl } from "../services/api/config";

// 服务端推送的「任务完成」通知结构：含会话/运行 ID、完成状态与未读数等元信息。
export interface TaskCompleteNotification {
  type: "task:complete";
  data: {
    session_id: string;
    run_id: string;
    status: "completed" | "failed";
    message?: string;
    unread_count?: number;
    project_id?: string | null;
    scheduled_task_id?: string | null;
    is_favorite?: boolean;
  };
}

// hook 选项：收到任务完成通知的回调，以及是否启用连接的开关。
interface UseWebSocketOptions {
  onTaskComplete?: (notification: TaskCompleteNotification) => void;
  enabled?: boolean;
}

// Exponential backoff configuration
// 指数退避配置：初始 1s、上限 30s、每次乘 1.5；连续 3 次鉴权失败后进入 5 分钟冷却
const INITIAL_RECONNECT_DELAY = 1000; // 1 second
const MAX_RECONNECT_DELAY = 30000; // 30 seconds
const RECONNECT_DELAY_MULTIPLIER = 1.5;
const MAX_AUTH_FAILURES = 3; // Switch to long interval after this many consecutive 401s
const AUTH_FAILURE_COOLDOWN = 5 * 60 * 1000; // 5 minutes cooldown after max failures

// useWebSocket：建立并维护到 /ws 的任务通知连接，返回连接状态与手动 connect/disconnect。
export function useWebSocket(options: UseWebSocketOptions = {}) {
  const { onTaskComplete, enabled = true } = options;
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const onTaskCompleteRef = useRef(onTaskComplete);
  const isMountedRef = useRef(true);
  const [isConnected, setIsConnected] = useState(false);

  // Track connection state to prevent race conditions
  const isConnectingRef = useRef(false);
  const isDisconnectingRef = useRef(false);

  // Exponential backoff state
  const reconnectAttemptRef = useRef(0);
  // Consecutive auth failure counter
  const authFailureCountRef = useRef(0);

  // Update ref when callback changes
  // 用 ref 保存最新回调，避免其变化导致连接被重建
  useEffect(() => {
    onTaskCompleteRef.current = onTaskComplete;
  }, [onTaskComplete]);

  // 建立连接：先做各种竞态/重复防护，再取 token、连 WS，并注册各事件回调
  const connect = useCallback(async () => {
    // Prevent multiple simultaneous connection attempts
    if (
      !isMountedRef.current ||
      isConnectingRef.current ||
      isDisconnectingRef.current
    ) {
      console.log(
        "[WebSocket] Skipping connect - already connecting or disconnecting",
      );
      return;
    }

    // Already connected
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    // Already connecting - wait for it to complete
    if (wsRef.current?.readyState === WebSocket.CONNECTING) {
      // Let the existing connection attempt proceed
      return;
    }

    // Close existing connection before creating a new one
    // 新建连接前先关闭旧连接（并短暂等待其关闭完成），避免出现两条并存的连接
    if (wsRef.current) {
      console.log(
        "[WebSocket] Closing existing connection before reconnecting",
      );
      isDisconnectingRef.current = true;

      // Store the old WebSocket to close it
      const oldWs = wsRef.current;
      wsRef.current = null;

      // Close the old connection
      try {
        oldWs.close();
      } catch (e) {
        console.warn("[WebSocket] Error closing old connection:", e);
      }

      // Small delay to allow connection to close properly
      await new Promise((resolve) => setTimeout(resolve, 100));

      isDisconnectingRef.current = false;
    }

    isConnectingRef.current = true;

    try {
      const token = await getValidAccessToken();
      if (!isMountedRef.current) {
        return;
      }
      if (!token) {
        console.warn("[WebSocket] No auth token, skipping connection");
        return;
      }

      // Token sent after connection (more secure than URL query param)
      const wsUrl = buildWebSocketUrl("/ws");

      console.log("[WebSocket] Connecting to:", wsUrl);

      const ws = new WebSocket(wsUrl);

      // Send authentication after connection is established
      // 连接建立后再发送鉴权消息；此时先不置 isConnected，等服务端回 auth:ok 才算真正连上
      ws.onopen = () => {
        console.log("[WebSocket] Connected, sending auth");
        ws.send(JSON.stringify({ type: "auth", token }));
        // Don't set isConnected yet — wait for auth:ok from server
      };

      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);

          if (message.type === "auth:ok") {
            // Auth confirmed by server — now truly connected
            // 服务端确认鉴权：此刻才标记为已连接，并重置退避与鉴权失败计数
            isConnectingRef.current = false;
            setIsConnected(true);
            reconnectAttemptRef.current = 0;
            authFailureCountRef.current = 0;
            console.log("[WebSocket] Auth confirmed");
            return;
          }

          console.log("[WebSocket] Received:", message);

          // 收到任务完成通知：回调外层处理（更新未读数/弹通知等）
          if (message.type === "task:complete" && onTaskCompleteRef.current) {
            onTaskCompleteRef.current(message);
          }
        } catch (e) {
          console.error("[WebSocket] Failed to parse message:", e);
        }
      };

      // 关闭回调：区分手动断开 / 鉴权失败 / 普通断开，决定是否及如何重连
      ws.onclose = (event) => {
        console.log("[WebSocket] Disconnected:", event.code, event.reason);
        isConnectingRef.current = false;
        setIsConnected(false);

        // Check if this was a manual disconnect BEFORE resetting the flag
        const wasManualDisconnect = isDisconnectingRef.current;
        isDisconnectingRef.current = false; // Reset here after socket is fully closed

        wsRef.current = null;

        // Don't reconnect on auth failure - token is invalid/expired
        // 4001: server explicitly rejects auth; reason may also indicate Unauthorized
        // 鉴权失败（4001 / Unauthorized）：有 refresh token 则刷新后重连；否则累计失败次数，
        // 达到上限则进入长冷却（而非永久停连），冷却后再试
        if (event.code === 4001 || event.reason === "Unauthorized") {
          if (getRefreshToken()) {
            void (async () => {
              try {
                await refreshAccessToken();
                authFailureCountRef.current = 0;
                if (isMountedRef.current && enabled && !wasManualDisconnect) {
                  connect();
                }
              } catch {
                // Don't redirect here — let authFetch / useAuth handle it.
                // A silent redirect from WebSocket background reconnection
                // is jarring; the user will get redirected on their next
                // intentional API call.
                console.warn(
                  "[WebSocket] Token refresh failed, will retry later",
                );
                authFailureCountRef.current++;
              }
            })();
            return;
          }
          authFailureCountRef.current++;
          if (authFailureCountRef.current >= MAX_AUTH_FAILURES) {
            // Switch to long-interval polling instead of permanently disabling
            console.warn(
              `[WebSocket] Auth failed ${
                authFailureCountRef.current
              } times, retrying in ${AUTH_FAILURE_COOLDOWN / 1000}s`,
            );
            if (enabled && !wasManualDisconnect) {
              reconnectTimeoutRef.current = setTimeout(() => {
                reconnectTimeoutRef.current = null;
                authFailureCountRef.current = 0; // Reset counter for the cooldown retry
                if (!isMountedRef.current) {
                  return;
                }
                connect();
              }, AUTH_FAILURE_COOLDOWN);
            }
            return;
          }
          console.warn(
            `[WebSocket] Auth failed (${authFailureCountRef.current}/${MAX_AUTH_FAILURES}), will retry`,
          );
          // Fall through to normal reconnect with backoff
        } else {
          // Non-auth failure: reset auth failure counter
          authFailureCountRef.current = 0;
        }

        // Only attempt to reconnect if still enabled and not manually closed
        // 仅在仍启用、无待执行重连、且非手动断开时才按指数退避安排重连
        if (
          enabled &&
          reconnectTimeoutRef.current === null &&
          !wasManualDisconnect
        ) {
          // Calculate delay with exponential backoff
          const delay = Math.min(
            INITIAL_RECONNECT_DELAY *
              Math.pow(RECONNECT_DELAY_MULTIPLIER, reconnectAttemptRef.current),
            MAX_RECONNECT_DELAY,
          );
          reconnectAttemptRef.current++;

          console.log(
            `[WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttemptRef.current})...`,
          );

          reconnectTimeoutRef.current = setTimeout(() => {
            console.log("[WebSocket] Reconnecting...");
            reconnectTimeoutRef.current = null;
            if (!isMountedRef.current) {
              return;
            }
            connect();
          }, delay);
        }
      };

      ws.onerror = (error) => {
        console.error("[WebSocket] Error:", error);
        isConnectingRef.current = false;
      };

      wsRef.current = ws;
    } catch (e) {
      console.error("[WebSocket] Connection failed:", e);
      isConnectingRef.current = false;
    }
  }, [enabled]);

  // 手动断开：清除待执行重连、标记断开中（防止 onclose 里再触发重连）、关闭连接并复位计数
  const disconnect = useCallback(() => {
    // Clear any pending reconnect
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      const ws = wsRef.current;
      // Mark as disconnecting BEFORE closing to prevent reconnect attempts in onclose
      isDisconnectingRef.current = true;
      ws.close();
      wsRef.current = null;
    }
    isConnectingRef.current = false;
    setIsConnected(false);
    // Reset reconnect attempt counter on manual disconnect
    reconnectAttemptRef.current = 0;
    authFailureCountRef.current = 0;
    // NOTE: Don't reset isDisconnectingRef here - let the onclose handler do it
    // This prevents race conditions where connect() is called before the socket finishes closing
  }, []);

  // Store connect/disconnect in refs to avoid deps issues
  // 用 ref 保存最新的 connect/disconnect，供下方 effect 调用而不必将其列为依赖
  const connectRef = useRef(connect);
  const disconnectRef = useRef(disconnect);
  connectRef.current = connect;
  disconnectRef.current = disconnect;

  // 依据 enabled 建立或断开连接；卸载时标记已卸载并断开，避免卸载后仍触发重连
  useEffect(() => {
    isMountedRef.current = true;
    if (enabled) {
      connectRef.current();
    } else {
      disconnectRef.current();
    }

    return () => {
      isMountedRef.current = false;
      disconnectRef.current();
    };
  }, [enabled]);

  return {
    isConnected,
    connect,
    disconnect,
  };
}
