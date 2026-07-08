import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";
import { BlockPreviewPortal } from "../../chat/ChatMessage/items/McpBlockPreview";
import { SessionSidebar } from "../../panels/SessionSidebar";
import type { SessionSidebarHandle } from "../../panels/SessionSidebar";
import { useSettingsContext } from "../../../contexts/SettingsContext";
import { useAgent } from "../../../hooks/useAgent";
import { useApprovals } from "../../../hooks/useApprovals";
import { useAuth } from "../../../hooks/useAuth";
import { useTools } from "../../../hooks/useTools";
import { useSkills } from "../../../hooks/useSkills";
import { personaPresetApi } from "../../../services/api";
import { usePersonaPresets } from "../../../hooks/usePersonaPresets";
import { useProjectManager } from "../../../hooks/useProjectManager";
import { appNotificationService } from "../../../services/notifications/appNotificationService";
import { useSessionConfig } from "../../../hooks/useSessionConfig";
import {
  Permission,
  type ToolCategory,
  type SkillSource,
  type PersonaPreset,
  type PersonaPresetSnapshot,
} from "../../../types";
import { useDragAndDrop } from "./useDragAndDrop";
import { useWebSocketNotifications } from "./useWebSocketNotifications";
import { useAgentOptions } from "./useAgentOptions";
import { useSessionSync } from "./useSessionSync";
import {
  getExternalNavigationPreviewRequest,
  getExternalNavigationTargetFile,
  shouldScrollToBottomAfterExternalNavigation,
} from "./externalNavigationState";
import {
  reconcileCurrentModelSelection,
  resolveDefaultModelSelection,
} from "./modelSelection";
import { getRestoredModelSelection } from "./sessionState";
import { getTeamRouteRequest } from "./teamRouteState";
import { resolvePersonaAgentId } from "../../../hooks/useAgent/agentSelection";
import { AppShell } from "./AppShell";
import { ChatView } from "./ChatView";
import { shouldShowMessageOutline } from "./messageOutline";
import { buildEffectiveSkills, countEnabledSkills } from "./skillAvailability";

const SCHEDULED_TASK_DEFAULTS_KEY = "lambchat_scheduled_task_defaults";
const CHAT_SKILL_LIST_PARAMS = { limit: 100 };

// ChatAppContent 的 props：外壳共享状态（个人资料弹窗、侧栏折叠、移动侧栏）由父级 AppContent 传入
export interface ChatAppContentProps {
  showProfileModal: boolean;
  onCloseProfileModal: () => void;
  versionInfo: import("../../../types").VersionInfo | null;
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  mobileSidebarOpen: boolean;
  setMobileSidebarOpen: (open: boolean) => void;
  onShowProfile: () => void;
}

// 聊天标签页的状态编排容器（Container 组件）：集中调用所有聊天相关 hooks
//（会话/消息、工具、技能、人设预设、Agent、审批、通知等），把整理后的数据与
// 回调透传给展示层 ChatView；自身几乎不含视觉结构，只包裹 AppShell 与拖拽遮罩。
export function ChatAppContent({
  showProfileModal,
  onCloseProfileModal,
  versionInfo,
  sidebarCollapsed,
  setSidebarCollapsed,
  mobileSidebarOpen,
  setMobileSidebarOpen,
  onShowProfile,
}: ChatAppContentProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { enableSkills, availableModels, defaultModel } = useSettingsContext();
  const { hasPermission, isAuthenticated } = useAuth();

  // 整页拖拽上传：捕获拖到窗口任意处的文件，作为待发送附件
  const { isPageDragging, pageDragAttachments, setPageDragAttachments } =
    useDragAndDrop();

  // 工具调用审批：收集需要用户确认的请求，并提供响应/新增/清空方法
  const {
    approvals,
    respondToApproval,
    addApproval,
    clearApprovals,
    isLoading: approvalLoading,
  } = useApprovals({ sessionId: null });

  // 可用工具列表及其加载/开关状态（含「按当前 Agent 刷新工具」）
  const {
    tools,
    isLoading: toolsLoading,
    totalCount: totalToolsCount,
    getDisabledToolNames,
    refreshToolsForAgent,
  } = useTools();

  // 技能列表（受全局 enableSkills 开关控制）
  const {
    skills,
    isLoading: skillsLoading,
    pendingSkillNames,
    isMutating: skillsMutating,
    fetchSkills,
  } = useSkills({ enabled: enableSkills, listParams: CHAT_SKILL_LIST_PARAMS });

  // 人设预设的读写权限，以及分页/搜索/标签筛选状态（列表参数随之变化）
  const canReadPersonaPresets = hasPermission(Permission.PERSONA_PRESET_READ);
  const canManagePersonaPresets =
    hasPermission(Permission.PERSONA_PRESET_WRITE) ||
    hasPermission(Permission.PERSONA_PRESET_ADMIN);
  const [personaPresetPage, setPersonaPresetPage] = useState(1);
  const [personaPresetQuery, setPersonaPresetQuery] = useState("");
  const [personaPresetTag, setPersonaPresetTag] = useState<string | null>(null);
  const personaPresetPageSize = 12;
  const personaPresetListParams = useMemo(
    () => ({
      skip: (personaPresetPage - 1) * personaPresetPageSize,
      limit: personaPresetPageSize,
      q: personaPresetQuery.trim() || undefined,
      tag: personaPresetTag || undefined,
    }),
    [personaPresetPage, personaPresetQuery, personaPresetTag],
  );
  const {
    presets: personaPresets,
    total: personaPresetsTotal,
    isLoading: personaPresetsLoading,
    isLoadingMore: personaPresetsLoadingMore,
    isMutating: personaPresetsMutating,
    usePreset: activatePersonaPreset,
    updatePreference: updatePersonaPreference,
    copyPreset: copyPersonaPreset,
    createPreset: createPersonaPreset,
    updatePreset: updatePersonaPreset,
    loadMore: loadMorePersonaPresets,
  } = usePersonaPresets({
    enabled: canReadPersonaPresets,
    listParams: personaPresetListParams,
  });

  const handlePersonaPresetSearchChange = useCallback((query: string) => {
    setPersonaPresetQuery(query);
  }, []);
  const handlePersonaPresetTagChange = useCallback((tag: string | null) => {
    setPersonaPresetTag(tag);
  }, []);

  const hasMorePersonaPresets = personaPresets.length < personaPresetsTotal;
  const handleLoadMorePersonaPresets = useCallback(() => {
    if (!hasMorePersonaPresets || personaPresetsLoadingMore) return;
    loadMorePersonaPresets(personaPresetListParams);
  }, [
    hasMorePersonaPresets,
    personaPresetsLoadingMore,
    loadMorePersonaPresets,
    personaPresetListParams,
  ]);

  const projectManager = useProjectManager();

  // 用 ref 保存「本次会话配置」快照，供 useAgent 的各 getter 在发送时同步读取最新值
  const sessionConfigRef = useRef({
    disabledSkills: [] as string[],
    enabledSkills: undefined as string[] | undefined,
    personaPresetId: null as string | null,
    disabledMcpTools: [] as string[],
    agentOptions: {} as Record<string, boolean | string | number>,
  });

  // 核心会话 hook：持有消息流、当前会话/运行、连接状态、Agent/团队选择、目标模式等，
  // 并暴露发送/停止/清空/切换 Agent 等操作；通过传入的回调接管审批与技能新增事件。
  const {
    messages,
    sessionId,
    currentRunId,
    isLoading,
    isLoadingHistory,
    agents,
    currentAgent,
    allowedModelIds: agentAllowedModelIds,
    connectionStatus,
    newlyCreatedSession,
    activeGoal,
    goalsByRunId,
    sendMessage,
    clearActiveGoal,
    stopGeneration,
    clearMessages,
    switchAgent,
    selectTeam,
    selectedTeamId,
    goalModeEnabled,
    setGoalModeEnabled,
    autoModeEnabled,
    setAutoModeEnabled,
    loadHistory,
    setPendingProjectId,
    autoExpandProjectId,
    clearAutoExpandProjectId,
    currentProjectId,
  } = useAgent({
    onApprovalRequired: (approval) => {
      void appNotificationService.notify({
        type: "approval",
        title: t("approvals.needsConfirmation"),
        body: approval.message,
        route: sessionId ? `/chat/${sessionId}` : "/chat",
        dedupeKey: `approval:${approval.id}`,
        importance: "high",
      });
      addApproval({
        id: approval.id,
        message: approval.message,
        type: "form",
        fields: approval.fields || [],
        status: "pending",
        session_id: sessionId,
        metadata: approval.metadata,
      });
    },
    onClearApprovals: () => {
      clearApprovals();
    },
    getEnabledTools: getDisabledToolNames,
    getDisabledSkills: () => sessionConfigRef.current.disabledSkills,
    getEnabledSkills: () => sessionConfigRef.current.enabledSkills,
    getPersonaPresetId: () => sessionConfigRef.current.personaPresetId,
    getDisabledMcpTools: () => sessionConfigRef.current.disabledMcpTools,
    getAgentOptions: () => sessionConfigRef.current.agentOptions,
    onSkillAdded: (
      skillName: string,
      _description: string,
      filesCount: number,
    ) => {
      console.log(
        `[AppContent] Skill added: ${skillName} (${filesCount} files), refreshing skills list`,
      );
      setTimeout(() => fetchSkills(), 500);
    },
  });

  // 使用人设预设前，若当前处于「团队」模式则切回普通 Agent 模式（人设不作用于团队）
  const switchToPersonaAgentMode = useCallback(() => {
    if (currentAgent !== "team") return;
    const nextAgentId = resolvePersonaAgentId(currentAgent, undefined, agents);
    if (nextAgentId && nextAgentId !== currentAgent) {
      switchAgent(nextAgentId);
    }
    selectTeam(null);
  }, [agents, currentAgent, selectTeam, switchAgent]);

  // Agent 发生切换时刷新其可用工具集
  const prevAgentRef = useRef(currentAgent);
  useEffect(() => {
    if (prevAgentRef.current !== currentAgent) {
      prevAgentRef.current = currentAgent;
      refreshToolsForAgent(currentAgent);
    }
  }, [currentAgent, refreshToolsForAgent]);

  // 按当前 Agent 允许的模型 id 过滤可选模型（null 表示不限制，[] 表示无可用模型）
  const filteredModels = useMemo(() => {
    if (!availableModels) return null;
    if (agentAllowedModelIds === null) return availableModels;
    if (agentAllowedModelIds.length === 0) return [];
    return availableModels.filter((m) => agentAllowedModelIds.includes(m.id));
  }, [availableModels, agentAllowedModelIds]);

  const {
    agentOptionValues,
    currentAgentOptions,
    handleToggleAgentOption,
    restoreAgentOptions,
    resetAgentOptionDefaults,
  } = useAgentOptions(agents, currentAgent);

  const {
    config: sessionConfig,
    toggleSkill: toggleSessionSkill,
    toggleMcpTool: toggleSessionMcpTool,
    setAgentOption: setSessionAgentOption,
    setPersonaPreset,
    clearPersonaPreset,
    resetToDefaults,
    restoreConfig: restoreSessionConfig,
  } = useSessionConfig({
    getDefaultAgentOptions: () => agentOptionValues,
  });

  const [currentModelId, setCurrentModelId] = useState<string>(() => {
    return localStorage.getItem("defaultModelId") || "";
  });
  const [currentModelValue, setCurrentModelValue] = useState<string>(
    () => localStorage.getItem("defaultModel") || defaultModel,
  );

  const isSessionRestoredRef = useRef(false);
  const lastTeamRouteRequestRef = useRef<string | null>(null);

  // 从 /persona 页面跳转过来时，用路由 state 或 localStorage 恢复选中的人设预设
  // Restore persona from localStorage when navigating from /persona page
  useEffect(() => {
    const personaId = searchParams.get("persona");
    if (!personaId) return;
    const state = location.state as
      | {
          personaPresetId?: string;
          personaSnapshot?: PersonaPresetSnapshot;
        }
      | null
      | undefined;
    setSearchParams(
      (prev) => {
        prev.delete("persona");
        return prev;
      },
      { replace: true },
    );
    if (
      state?.personaPresetId === personaId &&
      state.personaSnapshot?.preset_id === personaId
    ) {
      switchToPersonaAgentMode();
      setPersonaPreset(personaId, state.personaSnapshot);
      return;
    }
    try {
      const raw = localStorage.getItem("lambchat_session_config");
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (parsed.personaPresetId === personaId && parsed.personaSnapshot) {
        switchToPersonaAgentMode();
        setPersonaPreset(personaId, parsed.personaSnapshot);
      }
    } catch {
      /* ignore */
    }
  }, [
    location.state,
    searchParams,
    setSearchParams,
    setPersonaPreset,
    switchToPersonaAgentMode,
  ]);

  // 处理「通过路由指定 Agent/团队」的请求：切换后清理 URL 上的 agent/team 参数
  useEffect(() => {
    const teamRequest = getTeamRouteRequest(searchParams, location.state);
    if (!teamRequest) return;
    const requestKey = `${teamRequest.agentId}:${teamRequest.teamId}`;
    if (lastTeamRouteRequestRef.current === requestKey) return;
    lastTeamRouteRequestRef.current = requestKey;

    switchAgent(teamRequest.agentId);
    selectTeam(teamRequest.teamId);
    setSearchParams(
      (prev) => {
        prev.delete("agent");
        prev.delete("team");
        return prev;
      },
      { replace: true },
    );
  }, [location.state, searchParams, selectTeam, setSearchParams, switchAgent]);

  // 会话尚未从历史恢复时，校正当前模型选择（结合可用模型列表与 localStorage 默认值）
  useEffect(() => {
    if (isSessionRestoredRef.current) return;
    const nextSelection = reconcileCurrentModelSelection({
      availableModels,
      currentModelId,
      currentModelValue,
      storedDefaultId: localStorage.getItem("defaultModelId") || "",
      storedDefaultValue: localStorage.getItem("defaultModel") || "",
      fallbackDefaultValue: defaultModel,
    });

    if (nextSelection.modelId && nextSelection.modelId !== currentModelId) {
      setCurrentModelId(nextSelection.modelId);
    }
    if (
      nextSelection.modelValue &&
      nextSelection.modelValue !== currentModelValue
    ) {
      setCurrentModelValue(nextSelection.modelValue);
    }
  }, [availableModels, currentModelId, currentModelValue, defaultModel]);

  useEffect(() => {
    handleToggleAgentOption("model", currentModelValue);
    setSessionAgentOption("model", currentModelValue);
    handleToggleAgentOption("model_id", currentModelId);
    setSessionAgentOption("model_id", currentModelId);
  }, [
    currentModelValue,
    currentModelId,
    handleToggleAgentOption,
    setSessionAgentOption,
  ]);

  useEffect(() => {
    if (!currentAgent && !currentModelId && !currentModelValue) return;
    localStorage.setItem(
      SCHEDULED_TASK_DEFAULTS_KEY,
      JSON.stringify({
        agentId: currentAgent,
        modelId: currentModelId,
        modelValue: currentModelValue,
      }),
    );
  }, [currentAgent, currentModelId, currentModelValue]);

  const handleSelectModel = useCallback(
    (modelId: string, modelValue: string) => {
      setCurrentModelId(modelId);
      setCurrentModelValue(modelValue);
    },
    [],
  );

  // 在 render 期间同步写入 ref，确保 getAgentOptions 总能拿到最新 model_id；
  // 若改用 useEffect 会晚一拍，导致使用默认模型时 model_id 缺失。
  // Sync ref synchronously during render so getAgentOptions always has
  // the latest model_id — useEffect introduces a one-tick delay that
  // can cause model_id to be missing when using the default model.
  sessionConfigRef.current = {
    ...sessionConfig,
    enabledSkills: sessionConfig.personaSnapshot
      ? sessionConfig.personaSnapshot.skill_names
      : undefined,
    personaPresetId: sessionConfig.personaPresetId,
    agentOptions: {
      ...agentOptionValues,
      ...(currentModelValue ? { model: currentModelValue } : {}),
      ...(currentModelId ? { model_id: currentModelId } : {}),
    },
  };

  const handleUsePersonaPreset = useCallback(
    async (preset: PersonaPreset) => {
      const snapshot = await activatePersonaPreset(preset.id);
      if (snapshot) {
        switchToPersonaAgentMode();
        setPersonaPreset(preset.id, snapshot);
      }
      return snapshot;
    },
    [activatePersonaPreset, setPersonaPreset, switchToPersonaAgentMode],
  );

  const handleCopyPersonaPreset = useCallback(
    async (preset: PersonaPreset) => {
      await copyPersonaPreset(preset.id);
    },
    [copyPersonaPreset],
  );

  const handleTogglePersonaPreference = useCallback(
    async (
      preset: PersonaPreset,
      preference: { is_favorite?: boolean; is_pinned?: boolean },
    ) => {
      await updatePersonaPreference(preset.id, preference);
    },
    [updatePersonaPreference],
  );

  const handleSavePersonaPreset = useCallback(
    async (
      preset: PersonaPreset | null,
      data: {
        name: string;
        description: string;
        system_prompt: string;
        tags: string[];
        skill_names: string[];
      },
    ) => {
      if (preset) {
        await updatePersonaPreset(preset.id, data);
      } else {
        await createPersonaPreset(data);
      }
    },
    [createPersonaPreset, updatePersonaPreset],
  );

  // 叠加「本次会话临时禁用的 MCP 工具」后的有效工具列表
  const effectiveTools = useMemo(() => {
    const sessionDisabled = new Set(sessionConfig.disabledMcpTools);
    if (sessionDisabled.size === 0) return tools;
    return tools.map((t) => {
      if (t.category !== "mcp") return t;
      return { ...t, enabled: t.enabled && !sessionDisabled.has(t.name) };
    });
  }, [tools, sessionConfig.disabledMcpTools]);

  // 叠加人设技能名单与会话级禁用后的有效技能列表
  const effectiveSkills = useMemo(
    () =>
      buildEffectiveSkills({
        skills,
        skillsLoading,
        personaSkillNames: sessionConfig.personaSnapshot?.skill_names,
        disabledSkillNames: sessionConfig.disabledSkills,
      }),
    [
      skills,
      skillsLoading,
      sessionConfig.personaSnapshot?.skill_names,
      sessionConfig.disabledSkills,
    ],
  );
  const effectiveEnabledSkillsCount = useMemo(
    () => countEnabledSkills(effectiveSkills),
    [effectiveSkills],
  );

  const effectiveToggleTool = useCallback(
    (toolName: string) => {
      const tool = tools.find((t) => t.name === toolName);
      if (!tool) return;

      if (tool.category === "mcp") {
        toggleSessionMcpTool(toolName);
      }
    },
    [tools, toggleSessionMcpTool],
  );

  const effectiveToggleCategory = useCallback(
    (category: ToolCategory, enabled: boolean) => {
      if (category === "mcp") {
        tools
          .filter((t) => t.category === "mcp" && !t.system_disabled)
          .forEach((t) => {
            const isInSessionDisabled = sessionConfig.disabledMcpTools.includes(
              t.name,
            );
            if (enabled && isInSessionDisabled) {
              toggleSessionMcpTool(t.name);
            } else if (!enabled && !isInSessionDisabled) {
              toggleSessionMcpTool(t.name);
            }
          });
      }
    },
    [tools, sessionConfig.disabledMcpTools, toggleSessionMcpTool],
  );

  const effectiveToggleAll = useCallback(
    (enabled: boolean) => {
      tools
        .filter((t) => t.category === "mcp" && !t.system_disabled)
        .forEach((t) => {
          const isInSessionDisabled = sessionConfig.disabledMcpTools.includes(
            t.name,
          );
          if (enabled && isInSessionDisabled) {
            toggleSessionMcpTool(t.name);
          } else if (!enabled && !isInSessionDisabled) {
            toggleSessionMcpTool(t.name);
          }
        });
    },
    [tools, sessionConfig.disabledMcpTools, toggleSessionMcpTool],
  );

  const effectiveToggleSkill = useCallback(
    async (name: string): Promise<boolean> => {
      toggleSessionSkill(name);
      return true;
    },
    [toggleSessionSkill],
  );

  const effectiveToggleSkillCategory = useCallback(
    async (category: SkillSource, enabled: boolean): Promise<boolean> => {
      skills
        .filter((s) => s.enabled && s.source === category)
        .forEach((s) => {
          const isInSessionDisabled = sessionConfig.disabledSkills.includes(
            s.name,
          );
          if (enabled && isInSessionDisabled) {
            toggleSessionSkill(s.name);
          } else if (!enabled && !isInSessionDisabled) {
            toggleSessionSkill(s.name);
          }
        });
      return true;
    },
    [skills, sessionConfig.disabledSkills, toggleSessionSkill],
  );

  const effectiveToggleAllSkills = useCallback(
    async (enabled: boolean): Promise<boolean> => {
      skills
        .filter((s) => s.enabled)
        .forEach((s) => {
          const isInSessionDisabled = sessionConfig.disabledSkills.includes(
            s.name,
          );
          if (enabled && isInSessionDisabled) {
            toggleSessionSkill(s.name);
          } else if (!enabled && !isInSessionDisabled) {
            toggleSessionSkill(s.name);
          }
        });
      return true;
    },
    [skills, sessionConfig.disabledSkills, toggleSessionSkill],
  );

  const effectiveEnabledToolsCount = useMemo(
    () => effectiveTools.filter((t) => t.enabled).length,
    [effectiveTools],
  );

  // 是否有发送消息的权限（无权限时输入框禁用）
  const canSendMessage = hasPermission(Permission.CHAT_WRITE);

  const sidebarRef = useRef<SessionSidebarHandle>(null);

  // 订阅 WebSocket 通知：把会话未读数等实时更新推给侧栏
  useWebSocketNotifications({
    sessionId,
    enabled: isAuthenticated,
    onSessionUnread: (sid, count, projectId, isFavorite, scheduledTaskId) => {
      sidebarRef.current?.updateSessionUnread(
        sid,
        count,
        projectId,
        isFavorite,
        scheduledTaskId,
      );
    },
  });

  const [externalNavigationTargetRunId, setExternalNavigationTargetRunId] =
    useState<string | null>(null);
  const [
    externalNavigationTargetRunPending,
    setExternalNavigationTargetRunPending,
  ] = useState(false);
  const externalNavigationTargetFile = getExternalNavigationTargetFile(
    location.state,
  );
  const externalNavigationPreviewRequest = getExternalNavigationPreviewRequest(
    location.state,
  );
  const externalScrollToBottom = shouldScrollToBottomAfterExternalNavigation(
    location.state,
  );
  const externalNavigationRunId = searchParams.get("run_id")?.trim() || null;
  const externalNavigationToken =
    externalNavigationTargetFile ||
    externalScrollToBottom ||
    externalNavigationRunId
      ? location.key
      : null;
  const resolvedExternalNavigationTargetRunId =
    externalNavigationTargetRunId || externalNavigationRunId;

  // 外部导航带 traceId 时，向后端查出对应 run_id，供消息列表定位并滚动到该轮
  useEffect(() => {
    const targetTraceId = externalNavigationTargetFile?.traceId ?? undefined;

    if (!sessionId || !targetTraceId) {
      setExternalNavigationTargetRunId(null);
      setExternalNavigationTargetRunPending(false);
      return;
    }

    let cancelled = false;
    setExternalNavigationTargetRunPending(true);

    const resolveTargetRunId = async () => {
      try {
        const { sessionApi } = await import("../../../services/api");
        const response = await sessionApi.getRuns(sessionId, {
          trace_id: targetTraceId,
        });
        if (cancelled) {
          return;
        }

        const matchedRun =
          response.runs.find((run) => run.trace_id === targetTraceId) ?? null;
        setExternalNavigationTargetRunId(matchedRun?.run_id ?? null);
        setExternalNavigationTargetRunPending(false);
      } catch (err) {
        if (!cancelled) {
          console.warn(
            "[AppContent] Failed to resolve external navigation run:",
            err,
          );
          setExternalNavigationTargetRunId(null);
          setExternalNavigationTargetRunPending(false);
        }
      }
    };

    resolveTargetRunId();

    return () => {
      cancelled = true;
    };
  }, [sessionId, externalNavigationTargetFile?.traceId]);

  // 加载历史会话后恢复其配置：Agent、技能、人设（API 优先、metadata 快照兜底）、团队、Agent 选项与模型
  const handleConfigRestored = useCallback(
    (config: {
      agent_id?: string;
      agent_options?: Record<string, boolean | string | number>;
      disabled_skills?: string[];
      enabled_skills?: string[];
      persona_preset_id?: string;
      persona_preset_name?: string;
      persona_snapshot?: import("../../../types").PersonaPresetSnapshot;
      disabled_mcp_tools?: string[];
      disabled_tools?: string[];
      team_id?: string;
    }) => {
      console.log("[AppContent] Restoring session config:", config);

      isSessionRestoredRef.current = true;

      if (config.agent_id) {
        switchAgent(config.agent_id);
      }

      restoreSessionConfig(config);

      // Fetch latest persona snapshot by ID (API-first for normal views;
      // shared page uses its own SharedPage component and is unaffected).
      // The snapshot in metadata serves as a fallback until the API responds.
      if (config.persona_preset_id) {
        personaPresetApi
          .use(config.persona_preset_id)
          .then((snapshot) => {
            if (snapshot) {
              setPersonaPreset(config.persona_preset_id!, snapshot);
            }
          })
          .catch(() => {
            /* preset may have been deleted — keep metadata snapshot */
          });
      }

      if (config.team_id) {
        selectTeam(config.team_id);
      } else {
        selectTeam(null);
      }

      if (config.agent_options) {
        restoreAgentOptions(config.agent_options);

        const restoredModelSelection = getRestoredModelSelection(config);
        if (restoredModelSelection.modelId) {
          setCurrentModelId(restoredModelSelection.modelId);
        }
        if (restoredModelSelection.modelValue) {
          setCurrentModelValue(restoredModelSelection.modelValue);
        }
      }
    },
    [
      restoreSessionConfig,
      restoreAgentOptions,
      switchAgent,
      selectTeam,
      setPersonaPreset,
    ],
  );

  const { handleSelectSession, handleNewSession } = useSessionSync({
    activeTab: "chat",
    sessionId,
    loadHistory,
    clearMessages,
    onConfigRestored: handleConfigRestored,
  });

  // 新建会话并把模型/会话配置/Agent 选项全部重置为默认值
  const handleNewSessionWithReset = useCallback(() => {
    const nextSelection = resolveDefaultModelSelection({
      availableModels,
      storedDefaultId: localStorage.getItem("defaultModelId") || "",
      storedDefaultValue: localStorage.getItem("defaultModel") || "",
      fallbackDefaultValue: defaultModel,
    });

    handleNewSession();
    resetToDefaults();

    resetAgentOptionDefaults();

    setCurrentModelId(nextSelection.modelId);
    setCurrentModelValue(nextSelection.modelValue);
  }, [
    availableModels,
    defaultModel,
    handleNewSession,
    resetToDefaults,
    resetAgentOptionDefaults,
  ]);

  const handleMobileClose = useCallback(
    () => setMobileSidebarOpen(false),
    [setMobileSidebarOpen],
  );
  const handleSelectSessionAndClose = useCallback(
    (id: string) => {
      handleSelectSession(id);
      setMobileSidebarOpen(false);
    },
    [handleSelectSession, setMobileSidebarOpen],
  );
  const handleNewSessionAndClose = useCallback(() => {
    handleNewSessionWithReset();
    setMobileSidebarOpen(false);
  }, [handleNewSessionWithReset, setMobileSidebarOpen]);

  const outlineToggleRef = useRef<(() => void) | null>(null);
  const handleToggleOutline = useCallback(() => {
    outlineToggleRef.current?.();
  }, []);

  // 渲染：AppShell 提供外壳（含会话侧栏），内部为整页拖拽遮罩 + 展示层 ChatView。
  return (
    <AppShell
      activeTab="chat"
      showProfileModal={showProfileModal}
      onCloseProfileModal={onCloseProfileModal}
      versionInfo={versionInfo}
      setMobileSidebarOpen={setMobileSidebarOpen}
      currentProjectId={currentProjectId}
      projectManager={projectManager}
      onNewSession={handleNewSessionWithReset}
      onShowProfile={onShowProfile}
      availableModels={filteredModels}
      currentModelId={currentModelId}
      onSelectModel={handleSelectModel}
      sessionId={sessionId}
      showOutlineButton={shouldShowMessageOutline(messages)}
      onToggleOutline={handleToggleOutline}
      sidebar={
        <SessionSidebar
          ref={sidebarRef}
          currentSessionId={sessionId}
          onSelectSession={handleSelectSessionAndClose}
          onNewSession={handleNewSessionAndClose}
          onSetPendingProjectId={setPendingProjectId}
          autoExpandProjectId={autoExpandProjectId}
          onConsumeAutoExpandProjectId={clearAutoExpandProjectId}
          newSession={newlyCreatedSession}
          mobileOpen={mobileSidebarOpen}
          onMobileClose={handleMobileClose}
          isCollapsed={sidebarCollapsed}
          onToggleCollapsed={setSidebarCollapsed}
          onShowProfile={onShowProfile}
        />
      }
    >
      <>
        {/* 整页拖拽文件时显示的「拖放到此上传」全屏遮罩 */}
        {isPageDragging && (
          <div className="safe-area-viewport-padding fixed inset-0 z-[9999] flex items-center justify-center bg-stone-500/5 transition-colors dark:bg-stone-500/10">
            <div className="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-stone-400 bg-white/95 px-16 py-12 shadow-xl transition-colors dark:border-stone-500 dark:bg-stone-800/95">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                className="h-12 w-12 text-stone-500 dark:text-stone-400"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"
                />
              </svg>
              <span className="text-lg font-medium text-stone-600 dark:text-stone-300">
                {t("chat.dropFilesHere", "Drop files here to upload")}
              </span>
            </div>
          </div>
        )}

        <ChatView
          messages={messages}
          sessionId={sessionId}
          currentRunId={currentRunId}
          isLoading={isLoading}
          isLoadingHistory={isLoadingHistory}
          connectionStatus={connectionStatus}
          canSendMessage={canSendMessage}
          tools={effectiveTools}
          onToggleTool={effectiveToggleTool}
          onToggleCategory={effectiveToggleCategory}
          onToggleAll={effectiveToggleAll}
          toolsLoading={toolsLoading}
          enabledToolsCount={effectiveEnabledToolsCount}
          totalToolsCount={totalToolsCount}
          skills={effectiveSkills}
          onToggleSkill={effectiveToggleSkill}
          onToggleSkillCategory={effectiveToggleSkillCategory}
          onToggleAllSkills={effectiveToggleAllSkills}
          skillsLoading={skillsLoading}
          pendingSkillNames={pendingSkillNames}
          skillsMutating={skillsMutating}
          enabledSkillsCount={effectiveEnabledSkillsCount}
          totalSkillsCount={effectiveSkills.length}
          enableSkills={enableSkills}
          personaPresets={personaPresets}
          personaPresetsTotal={personaPresetsTotal}
          hasMorePersonaPresets={hasMorePersonaPresets}
          isLoadingMorePersonaPresets={personaPresetsLoadingMore}
          onLoadMorePersonaPresets={handleLoadMorePersonaPresets}
          personaPresetsPage={personaPresetPage}
          onPersonaPresetsPageChange={setPersonaPresetPage}
          onPersonaPresetsSearchChange={handlePersonaPresetSearchChange}
          onPersonaPresetsTagChange={handlePersonaPresetTagChange}
          selectedPersonaPresetId={sessionConfig.personaPresetId}
          selectedPersonaName={sessionConfig.personaSnapshot?.name || null}
          selectedPersonaSnapshot={sessionConfig.personaSnapshot}
          personaSkillsControlled={false}
          personaPresetsLoading={personaPresetsLoading}
          personaPresetsMutating={personaPresetsMutating}
          onUsePersonaPreset={handleUsePersonaPreset}
          onTogglePersonaPreference={handleTogglePersonaPreference}
          onCopyPersonaPreset={handleCopyPersonaPreset}
          onSavePersonaPreset={handleSavePersonaPreset}
          onClearPersonaPreset={clearPersonaPreset}
          canManagePersonaPresets={canManagePersonaPresets}
          agentOptions={currentAgentOptions}
          agentOptionValues={agentOptionValues}
          onToggleAgentOption={handleToggleAgentOption}
          agents={agents}
          currentAgent={currentAgent}
          onSelectAgent={switchAgent}
          selectedTeamId={selectedTeamId}
          onSelectTeam={selectTeam}
          approvals={approvals}
          onRespondApproval={respondToApproval}
          approvalLoading={approvalLoading}
          onSendMessage={(content, sendAttachments, runOptions) =>
            void sendMessage(content, undefined, sendAttachments, runOptions)
          }
          onStopGeneration={stopGeneration}
          activeGoal={activeGoal}
          goalsByRunId={goalsByRunId}
          onClearActiveGoal={clearActiveGoal}
          autoModeEnabled={autoModeEnabled}
          goalModeEnabled={goalModeEnabled}
          onToggleAutoMode={setAutoModeEnabled}
          onToggleGoalMode={setGoalModeEnabled}
          attachments={pageDragAttachments}
          onAttachmentsChange={setPageDragAttachments}
          externalNavigationToken={externalNavigationToken}
          externalNavigationTargetFile={externalNavigationTargetFile}
          externalNavigationPreview={externalNavigationPreviewRequest}
          externalNavigationTargetRunId={resolvedExternalNavigationTargetRunId}
          externalNavigationTargetRunPending={
            externalNavigationTargetRunPending
          }
          externalScrollToBottom={externalScrollToBottom}
          outlineToggleRef={outlineToggleRef}
        />
        <BlockPreviewPortal />
      </>
    </AppShell>
  );
}
