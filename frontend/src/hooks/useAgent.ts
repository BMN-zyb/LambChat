/**
 * Main useAgent hook
 * Provides agent communication, message management, and SSE streaming
 */
// 【驱动整个聊天的核心 hook】统一管理：agent 列表与选择、消息状态、SSE 流式收发、
// 历史加载、目标模式、发送/停止/清空等操作，并把这些能力打包成 UseAgentReturn 提供给 UI。
// 大量「跨渲染保持的状态」放在 ref 中（如连接控制器、去重集合、子 agent 栈、最新消息镜像），
// 以便在 SSE 异步回调里读到最新值、且不因每次渲染而重建连接。具体的 SSE 连接、事件处理、
// 事件→parts 转换、历史重建等已拆分到 ./useAgent/ 下的子模块，本文件负责编排与 React 状态。

import { useState, useCallback, useRef, useEffect } from "react";
import toast from "react-hot-toast";
import i18n from "../i18n";
import { uuid } from "../utils/uuid";
import type {
  Message,
  AgentInfo,
  AgentListResponse,
  ConnectionStatus,
  MessageAttachment,
} from "../types";
import { sessionApi, type BackendSession } from "../services/api";
import { authenticatedRequest } from "../services/api/authenticatedRequest";
import { API_BASE } from "../services/api/config";
import { feedbackApi } from "../services/api/feedback";
import { useAuth } from "../hooks/useAuth";
import { Permission } from "../types/auth";
import {
  type UseAgentOptions,
  type SubagentStackItem,
  type HistoryEvent,
  type UseAgentReturn,
  type ActiveGoalSpec,
} from "./useAgent/types";
import {
  reconstructMessagesFromEvents,
  getLastEventTimestamp,
  prepareMessagesForRunningRun,
  extractGoalFromEvents,
  extractGoalsByRunFromEvents,
} from "./useAgent/historyLoader";
import { clearAllLoadingStates } from "./useAgent/messageParts";
import { type EventHandlerContext } from "./useAgent/eventHandlers";
import {
  connectToSSE,
  clearReconnectTimeout,
  useSSEReconnect,
  type SSEConnectionContext,
} from "./useAgent/sseConnection";
import { createOptimisticMessagesForSend } from "./useAgent/optimisticMessages";
import { resolveRunEnabledSkills } from "./useAgent/runSkillOverrides";
import { planGoalSubmission } from "./useAgent/goalCommands";
import { translateBackendError } from "../utils/backendErrors";
import { dispatchSessionTitleUpdated } from "../utils/sessionTitleEvents";
import { resolveAvailableAgentId } from "./useAgent/agentSelection";

// useAgent：聊天引擎主 hook。options 提供外层注入的回调与取值函数（见 UseAgentOptions），
// 返回聊天状态与操作集合（见 UseAgentReturn）。
export function useAgent(options?: UseAgentOptions): UseAgentReturn {
  const { hasAnyPermission } = useAuth();
  // 是否有权限读取反馈（用于加载历史时附带 like/dislike 状态）
  const canReadFeedback = hasAnyPermission([
    Permission.FEEDBACK_READ,
    Permission.FEEDBACK_WRITE,
  ]);

  // State
  // React 状态：消息列表、加载态、会话/运行标识、agent 列表与选择、连接状态、
  // 目标模式与沙箱状态等，均驱动 UI 渲染
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [currentAgent, setCurrentAgent] = useState<string>("");
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [allowedModelIds, setAllowedModelIds] = useState<string[] | null>(null);
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>("disconnected");
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [newlyCreatedSession, setNewlyCreatedSession] =
    useState<BackendSession | null>(null);
  const [isInitializingSandbox, setIsInitializingSandbox] = useState(false);
  const [sandboxError, setSandboxError] = useState<string | null>(null);
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);
  const [activeGoal, setActiveGoal] = useState<ActiveGoalSpec | null>(null);
  const [goalsByRunId, setGoalsByRunId] = useState<
    Record<string, ActiveGoalSpec>
  >({});
  const [goalModeEnabled, setGoalModeEnabled] = useState(false);
  // 自动模式开关：初始值从 localStorage 恢复（读取失败则默认关闭）
  const [autoModeEnabled, setAutoModeEnabled] = useState(() => {
    try {
      return localStorage.getItem("lamb-chat-auto-mode") === "true";
    } catch {
      return false;
    }
  });

  // Persist autoModeEnabled to localStorage
  // 自动模式变化时持久化到 localStorage
  useEffect(() => {
    try {
      localStorage.setItem("lamb-chat-auto-mode", String(autoModeEnabled));
    } catch {
      /* storage unavailable */
    }
  }, [autoModeEnabled]);

  // Refs for connection management
  // 连接管理相关的 ref（跨渲染保持，不触发重渲染）：
  // 中止控制器、待用/自动展开的项目 ID、连接中/加载历史中/发送中标志、历史加载请求序号、
  // 重连定时器与重试计数
  const abortControllerRef = useRef<AbortController | null>(null);
  const pendingProjectIdRef = useRef<string | null>(null);
  const autoExpandProjectIdRef = useRef<string | null>(null);
  const isConnectingRef = useRef(false);
  const isLoadingHistoryRef = useRef(false);
  const isSendingRef = useRef(false);
  const loadHistoryRequestIdRef = useRef(0);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const retryCountRef = useRef(0);

  // Track processed event IDs to prevent duplicates
  const processedEventIdsRef = useRef<Set<string>>(new Set());

  // Track last event timestamp from history
  const lastHistoryTimestampRef = useRef<Date | null>(null);

  // Subagent tracking stack
  const activeSubagentStackRef = useRef<SubagentStackItem[]>([]);

  // Current streaming message ID
  const streamingMessageIdRef = useRef<string | null>(null);

  // Flag for reconnect from history
  const isReconnectFromHistoryRef = useRef<boolean>(false);

  // Stream version to invalidate stale SSE events after clearMessages
  // 流版本号：clearMessages 时自增，用于让「清空后仍在途」的旧 SSE 事件失效被丢弃
  const streamVersionRef = useRef(0);

  // Keep sessionId/runId in ref for closure access
  // 把 sessionId/runId/messages 同步进 ref：供 SSE 异步回调读取最新值，规避闭包捕获旧值
  const sessionIdRef = useRef<string | null>(null);
  const currentRunIdRef = useRef<string | null>(null);
  const messagesRef = useRef<Message[]>([]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    currentRunIdRef.current = currentRunId;
  }, [currentRunId]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // Create event handler context
  // 构造事件处理上下文：把 options、各 ref 与 state setter 打包，供事件处理器使用
  const createEventHandlerContext = useCallback(
    (): EventHandlerContext => ({
      options,
      sessionIdRef,
      processedEventIdsRef,
      lastHistoryTimestampRef,
      activeSubagentStackRef,
      streamVersionRef,
      setSessionId,
      setMessages,
      setConnectionStatus: (status) =>
        setConnectionStatus(status as ConnectionStatus),
      setIsInitializingSandbox,
      setSandboxError,
      setActiveGoal,
      setGoalsByRunId,
    }),
    [options],
  );

  // Create SSE connection context
  // 在事件处理上下文之上，再补充 SSE 连接所需的 ref（中止器、重连定时器等）
  const createSSEContext = useCallback(
    (): SSEConnectionContext => ({
      ...createEventHandlerContext(),
      abortControllerRef,
      isConnectingRef,
      streamingMessageIdRef,
      reconnectTimeoutRef,
      retryCountRef,
      messagesRef,
    }),
    [createEventHandlerContext],
  );

  // Ref for currentAgent to avoid dependency changes triggering refetch
  // 用 ref 镜像 currentAgent，避免它作为依赖导致 fetchAgents 反复重建
  const currentAgentRef = useRef(currentAgent);
  useEffect(() => {
    currentAgentRef.current = currentAgent;
  }, [currentAgent]);

  // Fetch available agents
  // 拉取可用 agent 列表与允许的模型；并据当前选择/默认值解析出应选中的 agent
  const fetchAgents = useCallback(async () => {
    setAgentsLoading(true);
    try {
      const response = await authenticatedRequest(`${API_BASE}/api/agents`, {
        headers: {
          "Content-Type": "application/json",
        },
      });
      if (!response.ok) throw new Error("Failed to fetch agents");
      const data: AgentListResponse = await response.json();
      const availableAgents = data.agents || [];
      setAgents(availableAgents);
      setAllowedModelIds(data.allowed_model_ids ?? null);
      const nextAgentId = resolveAvailableAgentId(
        currentAgentRef.current,
        data.default_agent,
        availableAgents,
      );
      if (nextAgentId !== currentAgentRef.current) {
        currentAgentRef.current = nextAgentId;
        setCurrentAgent(nextAgentId);
      }
    } catch (err) {
      console.error("Failed to fetch agents:", err);
    } finally {
      setAgentsLoading(false);
    }
  }, []); // No dependencies - uses ref instead

  // Load agents on mount
  // 挂载时加载一次 agent 列表
  useEffect(() => {
    fetchAgents();
  }, [fetchAgents]);

  // Refresh agents when page becomes visible (e.g., switching back to /chat tab)
  // 页面重新可见时刷新 agent 列表（例如从其它标签页切回 /chat）
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        fetchAgents();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [fetchAgents]);

  // Listen for agent preference updates to refresh agents list and apply new default
  // 监听「agent 偏好已更新」事件：重新拉取列表，并在无进行中会话时应用新的默认 agent
  useEffect(() => {
    const handleAgentPreferenceUpdated = async () => {
      // Fetch fresh agents data
      setAgentsLoading(true);
      try {
        const response = await authenticatedRequest(`${API_BASE}/api/agents`, {
          headers: {
            "Content-Type": "application/json",
          },
        });
        if (!response.ok) throw new Error("Failed to fetch agents");
        const data: AgentListResponse = await response.json();

        // Update agents list
        const availableAgents = data.agents || [];
        setAgents(availableAgents);
        setAllowedModelIds(data.allowed_model_ids ?? null);

        // Apply the new default agent if user doesn't have an active session
        // (i.e., no current messages means it's a good time to switch)
        const hasActiveSession = messagesRef.current.length > 0;
        const nextAgentId = resolveAvailableAgentId(
          hasActiveSession ? currentAgentRef.current : "",
          data.default_agent,
          availableAgents,
        );
        if (nextAgentId !== currentAgentRef.current) {
          currentAgentRef.current = nextAgentId;
          setCurrentAgent(nextAgentId);
        }
      } catch (err) {
        console.error("Failed to fetch agents after preference update:", err);
      } finally {
        setAgentsLoading(false);
      }
    };

    window.addEventListener(
      "agent-preference-updated",
      handleAgentPreferenceUpdated,
    );
    return () => {
      window.removeEventListener(
        "agent-preference-updated",
        handleAgentPreferenceUpdated,
      );
    };
  }, []);

  // Cleanup on unmount
  // 卸载时清理：中止连接并取消待执行的重连定时器
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      clearReconnectTimeout(reconnectTimeoutRef);
    };
  }, []);

  // Load message history from backend
  // 从后端加载并重建历史会话消息。
  // 参数：targetSessionId 会话 ID，可选 targetRunId 指定运行。返回从 metadata 还原的 SessionConfig（供 UI 恢复配置）。
  // 难点：用递增的 requestId 做「过期请求」保护——切换会话时旧请求的结果会被丢弃；
  // events/status/feedback 三个请求并行发起以减少等待；若任务仍在运行则重建后再后台重连 SSE。
  const loadHistory = useCallback(
    async (targetSessionId: string, targetRunId?: string) => {
      // 每次调用自增请求号；后续用 isStaleHistoryLoad() 判断本次结果是否已被更新的加载覆盖
      loadHistoryRequestIdRef.current += 1;
      const requestId = loadHistoryRequestIdRef.current;
      const isStaleHistoryLoad = () =>
        requestId !== loadHistoryRequestIdRef.current;

      if (isLoadingHistoryRef.current) {
        console.log(
          "[loadHistory] Switching to new session, aborting previous load...",
        );
      }
      isLoadingHistoryRef.current = true;
      setIsLoadingHistory(true);

      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      isConnectingRef.current = false;
      streamingMessageIdRef.current = null;
      clearReconnectTimeout(reconnectTimeoutRef);

      setIsLoading(true);
      setMessages([]);
      setError(null);

      // 重置去重集合与历史时间戳水位，避免上一个会话的状态串扰
      processedEventIdsRef.current.clear();
      lastHistoryTimestampRef.current = null;
      const markReadPromise = sessionApi
        .markRead(targetSessionId)
        .catch(() => {});

      // Clear approvals before loading new session
      options?.onClearApprovals?.();

      try {
        await markReadPromise;
        if (isStaleHistoryLoad()) return null;

        const sessionData = await sessionApi.get(targetSessionId);
        if (isStaleHistoryLoad()) return null;

        if (sessionData) {
          setSessionId(targetSessionId);
          setCurrentProjectId(
            (sessionData.metadata?.project_id as string) || null,
          );

          const currentRunId =
            targetRunId ||
            (sessionData.metadata?.current_run_id as string) ||
            null;

          // 从 metadata 提取配置信息
          const sessionConfig = {
            agent_id: (sessionData.metadata?.agent_id as string) || undefined,
            agent_options:
              (sessionData.metadata?.agent_options as Record<
                string,
                boolean | string | number
              >) || undefined,
            disabled_tools:
              (sessionData.metadata?.disabled_tools as string[]) || undefined,
            disabled_skills:
              (sessionData.metadata?.disabled_skills as string[]) || undefined,
            enabled_skills:
              (sessionData.metadata?.enabled_skills as string[]) || undefined,
            persona_preset_id:
              (sessionData.metadata?.persona_preset_id as string) || undefined,
            persona_preset_name:
              (sessionData.metadata?.persona_preset_name as string) ||
              undefined,
            persona_snapshot:
              (sessionData.metadata?.persona_snapshot as
                | import("../types").PersonaPresetSnapshot
                | undefined) || undefined,
            disabled_mcp_tools:
              (sessionData.metadata?.disabled_mcp_tools as string[]) ||
              undefined,
            team_id: (sessionData.metadata?.team_id as string) || undefined,
          };
          setGoalModeEnabled(false);

          // 并行发起 events、status 和 feedback 请求，减少串行等待时间
          const eventsPromise = sessionApi.getEvents(targetSessionId);
          const statusPromise = currentRunId
            ? sessionApi.getStatus(targetSessionId, currentRunId).catch((e) => {
                console.warn("[loadHistory] Failed to check status:", e);
                return null;
              })
            : Promise.resolve(null);
          const feedbackPromise = canReadFeedback
            ? feedbackApi
                .list(0, 100, undefined, undefined, targetSessionId)
                .catch((e) => {
                  console.warn("[loadHistory] Failed to load feedback:", e);
                  return null;
                })
            : Promise.resolve(null);

          const [eventsData, statusData, feedbackList] = await Promise.all([
            eventsPromise,
            statusPromise,
            feedbackPromise,
          ]);
          if (isStaleHistoryLoad()) return null;

          let isTaskRunning = false;
          if (statusData) {
            isTaskRunning =
              statusData.status === "pending" ||
              statusData.status === "running";
          }

          // 有历史事件：重建消息、附加反馈、恢复目标状态
          if (eventsData.events && eventsData.events.length > 0) {
            let reconstructedMessages = reconstructMessagesFromEvents(
              eventsData.events as HistoryEvent[],
              processedEventIdsRef.current,
              { options, activeSubagentStack: activeSubagentStackRef.current },
            );

            // Apply feedback (already loaded in parallel)
            if (feedbackList && feedbackList.items.length > 0) {
              const feedbackMap = new Map(
                feedbackList.items.map((f) => [
                  f.run_id,
                  { feedback: f.rating, feedbackId: f.id },
                ]),
              );
              reconstructedMessages = reconstructedMessages.map((msg) => {
                if (msg.runId) {
                  const feedbackInfo = feedbackMap.get(msg.runId);
                  if (feedbackInfo) {
                    return {
                      ...msg,
                      feedback: feedbackInfo.feedback,
                      feedbackId: feedbackInfo.feedbackId,
                    };
                  }
                }
                return msg;
              });
            }

            const lastTimestamp = getLastEventTimestamp(
              eventsData.events as HistoryEvent[],
            );
            if (lastTimestamp) {
              lastHistoryTimestampRef.current = lastTimestamp;
            }
            if (isStaleHistoryLoad()) return null;

            // Reconstruct active goal from history events (goal:start / goal:end)
            const restoredGoal = extractGoalFromEvents(
              eventsData.events as HistoryEvent[],
            );
            if (isStaleHistoryLoad()) return null;
            setActiveGoal(restoredGoal);
            setGoalsByRunId(
              extractGoalsByRunFromEvents(eventsData.events as HistoryEvent[]),
            );

            // When the task is still running, target the assistant message for
            // that same run. If history has the user message but no assistant
            // events yet, append a fresh assistant bubble after the latest user.
            // 任务仍在运行：定位/准备该 run 对应的流式 assistant 消息，随后「发射即忘」地后台重连 SSE，
            // 让 loadHistory 能立刻返回 sessionConfig，不被长连接阻塞
            if (isTaskRunning && currentRunId) {
              setCurrentRunId(currentRunId);

              const prepared = prepareMessagesForRunningRun(
                reconstructedMessages,
                currentRunId,
                undefined,
                messagesRef.current,
              );
              reconstructedMessages = prepared.messages;
              const streamingMessageId = prepared.streamingMessageId;

              setMessages(reconstructedMessages);

              // Fire-and-forget SSE reconnect so that loadHistory
              // returns sessionConfig immediately, allowing the caller
              // (useSessionSync) to restore model selection and other UI
              // state without being blocked by the long-lived connection.
              isReconnectFromHistoryRef.current = false;
              const ctx = createSSEContext();
              connectToSSE(
                targetSessionId,
                currentRunId,
                streamingMessageId,
                ctx,
              ).catch((e) => {
                console.warn("[loadHistory] SSE reconnect failed:", e);
              });
            } else {
              setMessages(reconstructedMessages);
            }
          } else {
            // 无历史事件：清空消息与目标；若任务仍在运行则新建流式占位并后台重连 SSE
            setMessages([]);
            setActiveGoal(null);
            setGoalsByRunId({});

            if (isTaskRunning && currentRunId) {
              setCurrentRunId(currentRunId);
              isReconnectFromHistoryRef.current = false;

              const streamingMessageId = uuid();
              const prepared = prepareMessagesForRunningRun(
                [],
                currentRunId,
                () => streamingMessageId,
                messagesRef.current,
              );
              setMessages(prepared.messages);
              // Fire-and-forget SSE reconnect (same reason as above).
              const ctx = createSSEContext();
              connectToSSE(
                targetSessionId,
                currentRunId,
                streamingMessageId,
                ctx,
              ).catch((e) => {
                console.warn("[loadHistory] SSE reconnect failed:", e);
              });
            }
          }

          // Return sessionConfig *before* any SSE reconnect so that the
          // caller can immediately restore model selection / agent / config.

          return sessionConfig;
        }
      } catch (err) {
        if (isStaleHistoryLoad()) return null;
        console.error("Failed to load session:", err);
        setError(i18n.t("chat.requestFailed"));
      } finally {
        // 仅当本次加载未被更新的请求取代时才收尾状态（避免过期请求覆盖最新加载的 UI 态）
        if (!isStaleHistoryLoad()) {
          setIsLoading(false);
          setIsLoadingHistory(false);
          isLoadingHistoryRef.current = false;
        }
      }

      return null;
    },
    [options, createSSEContext, canReadFeedback],
  );

  // Send message
  // 发送消息的主流程：
  // 1) 目标模式命令解析（可能只切换目标而不真正发送）；2) 去抖/中止旧连接；3) 插入乐观消息；
  // 4) 组装工具/技能/人设/模型等选项并调用 submitChat；5) 用返回的 session_id/run_id 回填消息与会话元数据；
  // 6) 建立 SSE 连接接收流式回复。参数：content 文本、agentOptions 运行选项、attachments 附件、runOptions 单次技能覆盖。
  const sendMessage = useCallback(
    async (
      content: string,
      agentOptions?: Record<string, boolean | string | number>,
      attachments?: MessageAttachment[],
      runOptions?: { enabledSkills?: string[] },
    ) => {
      if (!content.trim()) return;
      // 递增历史请求号，使任何进行中的历史加载结果作废（避免与本次发送竞争）
      loadHistoryRequestIdRef.current += 1;

      // 解析目标模式：clear/invalid 等「无需发送」的情况在此直接处理并返回
      const goalPlan = planGoalSubmission(content, goalModeEnabled);
      if (goalPlan.handledWithoutSend) {
        if (goalPlan.errorKey) {
          setError(i18n.t(goalPlan.errorKey, "Please enter a goal"));
          return;
        }
        setGoalModeEnabled(goalPlan.nextGoalModeEnabled);
        setActiveGoal(goalPlan.nextActiveGoal);
        setError(null);
        return;
      }
      content = goalPlan.content;
      setGoalModeEnabled(goalPlan.nextGoalModeEnabled);
      setActiveGoal(goalPlan.nextActiveGoal);

      // 发送去重：同一时刻只允许一次发送在途
      if (isSendingRef.current) {
        console.log(
          "[sendMessage] Already sending, ignoring duplicate request",
        );
        return;
      }
      isSendingRef.current = true;

      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      isConnectingRef.current = false;
      clearReconnectTimeout(reconnectTimeoutRef);

      processedEventIdsRef.current.clear();
      lastHistoryTimestampRef.current = null;

      // 乐观更新：立即插入用户消息 + 空的流式 assistant 占位，先给出反馈再等后端
      const { messages: optimisticMessages, assistantMessageId } =
        createOptimisticMessagesForSend({
          previousMessages: messagesRef.current,
          content,
          attachments,
          enabledSkills: runOptions?.enabledSkills,
        });

      setMessages(optimisticMessages);
      setIsLoading(true);
      setError(null);
      let finalAssistantMessageId = assistantMessageId;

      try {
        // 用户发送消息时标记当前 session 为已读
        if (sessionId) {
          sessionApi.markRead(sessionId).catch(() => {});
        }

        // 获取当前禁用的 skills 和 mcp_tools
        const personaPresetId = options?.getPersonaPresetId?.() || null;
        const disabledSkills = options?.getDisabledSkills?.() || [];
        const enabledSkills = resolveRunEnabledSkills({
          personaPresetId,
          personaEnabledSkills: options?.getEnabledSkills?.(),
          runEnabledSkills: runOptions?.enabledSkills,
        });
        const disabledMcpTools = options?.getDisabledMcpTools?.() || [];

        // Merge session-level agent options (e.g. model) with ChatInput values
        // 合并会话级 agent 选项（如模型）与本次输入框传入的选项
        const fullAgentOptions = {
          ...options?.getAgentOptions?.(),
          ...agentOptions,
        };
        const requestTeamId = currentAgent === "team" ? selectedTeamId : null;
        const goalForRun = goalPlan.goal;

        // 提交聊天请求，后端返回本次的 session_id / run_id / 队列状态等
        const submitData = (await sessionApi.submitChat(
          currentAgent,
          content,
          sessionId ?? undefined,
          fullAgentOptions,
          attachments,
          pendingProjectIdRef.current ?? undefined,
          disabledSkills,
          disabledMcpTools,
          personaPresetId,
          enabledSkills,
          requestTeamId,
          goalForRun,
        )) as {
          session_id: string;
          run_id: string;
          trace_id: string;
          status: string;
          queue_position?: number;
        };

        const newSessionId = submitData.session_id;
        const newRunId = submitData.run_id;
        const projectId = pendingProjectIdRef.current;

        if (goalForRun) {
          const goalWithRunId = {
            ...goalForRun,
            runId: newRunId,
          };
          setActiveGoal((prev) =>
            prev
              ? {
                  ...prev,
                  runId: newRunId,
                }
              : goalWithRunId,
          );
          setGoalsByRunId((prev) => ({
            ...prev,
            [newRunId]: goalWithRunId,
          }));
        }

        // Clear pending project ID after use
        pendingProjectIdRef.current = null;

        // Handle queued status — show toast and wait via SSE
        // 排队状态：提示排队位置，后续通过 SSE 的 queue_update 等待轮到
        if (submitData.status === "queued") {
          toast.loading(
            i18n.t("chat.queued", { position: submitData.queue_position }),
            { id: "chat-queue", duration: Infinity },
          );
        }

        // 新建会话（首条消息）：记录 session、构建会话元数据（配置快照），并异步生成会话标题
        if (!sessionId && newSessionId) {
          setSessionId(newSessionId);
          const now = new Date().toISOString();

          // 构建完整的对话配置
          const conversationConfig: Record<string, unknown> = {
            current_run_id: newRunId,
            agent_id: currentAgent,
            agent_options: fullAgentOptions,
            disabled_skills: disabledSkills,
            enabled_skills: enabledSkills,
            persona_preset_id: personaPresetId,
            disabled_mcp_tools: disabledMcpTools,
          };
          if (projectId) {
            conversationConfig.project_id = projectId;
          }
          if (currentAgent === "team" && selectedTeamId) {
            conversationConfig.team_id = selectedTeamId;
          }

          const newSession: BackendSession = {
            id: newSessionId,
            agent_id: currentAgent,
            created_at: now,
            updated_at: now,
            is_active: true,
            metadata: conversationConfig,
          };
          setNewlyCreatedSession(newSession);
          setCurrentProjectId(projectId);

          sessionApi
            .generateTitle(newSessionId, content, i18n.language)
            .then((result) => {
              setNewlyCreatedSession((prev) =>
                prev
                  ? {
                      ...prev,
                      name: result.title,
                      updated_at: new Date().toISOString(),
                    }
                  : null,
              );
              dispatchSessionTitleUpdated({
                sessionId: newSessionId,
                title: result.title,
              });
            })
            .catch((err) => {
              console.warn("[sendMessage] Failed to generate title:", err);
            });
        } else if (sessionId && newRunId) {
          // 更新现有 session 的 metadata
          const conversationConfig: Record<string, unknown> = {
            ...((newlyCreatedSession?.metadata as Record<string, unknown>) ||
              {}),
            current_run_id: newRunId,
            agent_id: currentAgent,
            agent_options: fullAgentOptions,
            disabled_skills: disabledSkills,
            enabled_skills: enabledSkills,
            persona_preset_id: personaPresetId,
            disabled_mcp_tools: disabledMcpTools,
          };
          if (currentAgent === "team" && selectedTeamId) {
            conversationConfig.team_id = selectedTeamId;
          }

          setNewlyCreatedSession((prev) =>
            prev
              ? {
                  ...prev,
                  metadata: conversationConfig,
                  updated_at: new Date().toISOString(),
                }
              : null,
          );
        }
        // 用真实 run_id 替换乐观 assistant 消息的临时 ID，使后续 SSE 事件能匹配到它
        if (newRunId) {
          setCurrentRunId(newRunId);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMessageId
                ? {
                    ...m,
                    id: newRunId,
                    runId: newRunId,
                  }
                : m,
            ),
          );
        }

        const streamSessionId = newSessionId || sessionId;
        const streamRunId = newRunId;
        finalAssistantMessageId = newRunId || assistantMessageId;

        if (!streamSessionId || !streamRunId) {
          throw new Error("Missing session_id or run_id");
        }

        isReconnectFromHistoryRef.current = false;
        const ctx = createSSEContext();
        // 建立 SSE 连接，开始接收本次运行的流式回复
        await connectToSSE(
          streamSessionId,
          streamRunId,
          finalAssistantMessageId,
          ctx,
        );
      } catch (err) {
        // 主动中止不算错误；其余错误：写入 assistant 消息、清理 loading 并断开连接
        if (err instanceof Error && err.name === "AbortError") {
          return;
        }
        const errorMessage =
          err instanceof Error
            ? translateBackendError(err.message, i18n.t.bind(i18n))
            : i18n.t("chat.unknownError");
        setError(errorMessage);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === finalAssistantMessageId
              ? {
                  ...m,
                  content: i18n.t("chat.errorPrefix", { error: errorMessage }),
                  isStreaming: false,
                  parts: clearAllLoadingStates(m.parts || []),
                }
              : m,
          ),
        );
        setConnectionStatus("disconnected");
        setIsInitializingSandbox(false);
      } finally {
        setIsLoading(false);
        isSendingRef.current = false;
      }
    },
    [
      sessionId,
      currentAgent,
      createSSEContext,
      newlyCreatedSession?.metadata,
      options,
      selectedTeamId,
      goalModeEnabled,
    ],
  );

  // 停止生成：复位发送/加载/沙箱态，清空审批与所有消息的 loading 占位，并调用后端取消接口
  const stopGeneration = useCallback(async () => {
    isSendingRef.current = false;
    setIsLoading(false);
    setIsInitializingSandbox(false);
    setSandboxError(null);

    // Clear approvals immediately (don't wait for SSE cancel event which may never arrive)
    options?.onClearApprovals?.();

    // Clear loading states on all messages and their parts
    setMessages((prev) =>
      prev.map((m) => ({
        ...m,
        isStreaming: false,
        parts: clearAllLoadingStates(m.parts || []),
      })),
    );

    const currentSessionId = sessionIdRef.current;
    if (currentSessionId) {
      try {
        await sessionApi.cancel(currentSessionId);
      } catch (error) {
        console.error(
          "[stopGeneration] Failed to call backend cancel API:",
          error,
        );
      }
    }
  }, [options]);

  // 清空会话：重置所有消息与连接状态，并自增 streamVersion 使在途 SSE 事件全部作废
  const clearMessages = useCallback(() => {
    loadHistoryRequestIdRef.current += 1;
    streamVersionRef.current += 1;
    setMessages([]);
    setIsLoading(false);
    setIsLoadingHistory(false);
    isLoadingHistoryRef.current = false;
    isSendingRef.current = false;
    setSessionId(null);
    setError(null);
    setCurrentRunId(null);
    setConnectionStatus("disconnected");
    processedEventIdsRef.current.clear();
    lastHistoryTimestampRef.current = null;
    streamingMessageIdRef.current = null;
    sessionIdRef.current = null;
    currentRunIdRef.current = null;
    activeSubagentStackRef.current = [];
    setGoalModeEnabled(false);
    setActiveGoal(null);
    setGoalsByRunId({});
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    clearReconnectTimeout(reconnectTimeoutRef);
  }, []);

  // 清除当前激活目标并关闭目标模式
  const clearActiveGoal = useCallback(() => {
    setGoalModeEnabled(false);
    setActiveGoal(null);
  }, []);

  // 选择 agent：切换后清空当前会话（开启新对话）
  const selectAgent = useCallback(
    (agentId: string) => {
      setCurrentAgent(agentId);
      clearMessages();
    },
    [clearMessages],
  );

  // Switch agent without clearing messages (for mode toggling)
  // 切换 agent 但不清空消息（用于模式切换，如普通/团队模式来回切）
  const switchAgent = useCallback((agentId: string) => {
    setCurrentAgent(agentId);
  }, []);

  // Select a team for team-mode agent
  // 为「团队模式」选择具体团队
  const selectTeam = useCallback((teamId: string | null) => {
    setSelectedTeamId(teamId);
  }, []);

  // Reconnect function (managed by useSSEReconnect hook)
  // 手动/自动重连函数（由 useSSEReconnect 管理可见性与网络事件触发）
  const handleReconnectSSE = useSSEReconnect({
    createSSEContext,
    sessionIdRef,
    currentRunIdRef,
    isReconnectFromHistoryRef,
    streamingMessageIdRef,
    connectionStatus,
    setConnectionStatus,
  });

  // 对外返回聊天状态与操作集合（契约见 UseAgentReturn），供 UI 组件消费
  return {
    messages,
    isLoading,
    isLoadingHistory,
    error,
    sessionId,
    currentRunId,
    agents,
    currentAgent,
    agentsLoading,
    allowedModelIds,
    isReconnecting: connectionStatus === "reconnecting",
    connectionStatus,
    newlyCreatedSession,
    activeGoal,
    goalsByRunId,
    isInitializingSandbox,
    sandboxError,
    sendMessage,
    clearActiveGoal,
    stopGeneration,
    clearMessages,
    selectAgent,
    switchAgent,
    selectTeam,
    selectedTeamId,
    goalModeEnabled,
    setGoalModeEnabled,
    autoModeEnabled,
    setAutoModeEnabled,
    refreshAgents: fetchAgents,
    loadHistory,
    reconnectSSE: handleReconnectSSE,
    setPendingProjectId: (id: string | null) => {
      pendingProjectIdRef.current = id;
      autoExpandProjectIdRef.current = id;
    },
    autoExpandProjectId: autoExpandProjectIdRef.current,
    clearAutoExpandProjectId: (id?: string | null) => {
      if (
        id === undefined ||
        id === null ||
        autoExpandProjectIdRef.current === id
      ) {
        autoExpandProjectIdRef.current = null;
      }
    },
    currentProjectId,
  };
}

// Re-export types and utilities
export type {
  UseAgentOptions,
  UseAgentReturn,
  BackendSession,
} from "./useAgent/types";
