import { useMemo, useCallback, useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../../hooks/useAuth";
import { ChatMessage } from "../../chat/ChatMessage";
import { AttachmentPreviewHost } from "../../chat/AttachmentPreviewHost";
import { RevealPreviewHost } from "../../chat/ChatMessage/items/RevealPreviewHost";
import { SessionImageGalleryProvider } from "../../chat/ChatMessage/sessionImageGallery";
import { PersistentToolPanelHost } from "../../chat/ChatMessage/items/persistentToolPanelState";
import { ChatInput } from "../../chat/ChatInput";
import { WelcomePage } from "../../chat/WelcomePage";
import { Virtuoso, type ListRange } from "react-virtuoso";
import { ApprovalPanel } from "../../panels/ApprovalPanel";
import { SessionScheduledTasksButton } from "../../panels/ScheduledTaskPanel";
import {
  ChatSkeleton,
  ChatSkeletonMessagesOnly,
} from "../../skeletons/ChatSkeletons";
import { useMessageScroll } from "./useMessageScroll";
import {
  getAtBottomThresholdPx,
  getInitialBottomItemLocation,
  getMessageListFooterSpacerClass,
} from "./messageScrollUtils";
import { getNextMessageListSessionKey } from "./useMessageScroll";
import {
  isSessionRunning,
  shouldShowStreamingFooterSkeleton,
} from "./sessionState";
import type { MessageAttachment } from "../../../types";
import type { ChatViewProps } from "./ChatViewProps";
import { useCurrentTeam, resolveChatAssistantIdentity } from "./ChatViewProps";
import { useChatOutline } from "./useChatOutline";
import { useRevealPreview } from "./useRevealPreview";
import { findCancelledRetryTarget } from "../../chat/ChatMessage/cancelledRetry";
import {
  getGoalForMessage,
  getVisibleActiveGoalForMessages,
} from "../../chat/goalVisibility";
import { sessionApi } from "../../../services/api";

const FLOATING_SCROLL_BUTTON_OFFSET_CLASS = "bottom-full mb-3";

// 聊天主界面组件：装配「消息列表（虚拟滚动）+ 审批面板 + 预览宿主 + 底部输入框」。
// 由 ChatAppContent 把会话数据、工具/技能/人设、Agent 等大量状态透传进来（见 ChatViewProps）。
// 消息为空时展示欢迎页 WelcomePage，否则用 react-virtuoso 渲染 ChatMessage 列表。
export function ChatView({
  messages,
  sessionId,
  currentRunId,
  isLoading,
  isLoadingHistory,
  connectionStatus,
  canSendMessage,
  tools,
  onToggleTool,
  onToggleCategory,
  onToggleAll,
  toolsLoading,
  enabledToolsCount,
  totalToolsCount,
  skills,
  onToggleSkill,
  onToggleSkillCategory,
  onToggleAllSkills,
  skillsLoading,
  pendingSkillNames,
  skillsMutating,
  enabledSkillsCount,
  totalSkillsCount,
  enableSkills,
  personaPresets,
  personaPresetsTotal,
  hasMorePersonaPresets,
  isLoadingMorePersonaPresets,
  onLoadMorePersonaPresets,
  personaPresetsPage,
  onPersonaPresetsPageChange,
  onPersonaPresetsSearchChange,
  onPersonaPresetsTagChange,
  selectedPersonaPresetId,
  selectedPersonaName,
  selectedPersonaSnapshot,
  personaSkillsControlled,
  personaPresetsLoading,
  personaPresetsMutating,
  onUsePersonaPreset,
  onTogglePersonaPreference,
  onCopyPersonaPreset,
  onSavePersonaPreset,
  onClearPersonaPreset,
  canManagePersonaPresets,
  agentOptions,
  agentOptionValues,
  onToggleAgentOption,
  agents,
  currentAgent,
  onSelectAgent,
  selectedTeamId,
  onSelectTeam,
  onOpenTeamBuilder,
  approvals,
  onRespondApproval,
  approvalLoading,
  onSendMessage,
  onStopGeneration,
  activeGoal,
  goalsByRunId,
  onClearActiveGoal,
  attachments,
  onAttachmentsChange,
  externalNavigationToken,
  externalNavigationTargetFile,
  externalNavigationPreview,
  externalNavigationTargetRunId,
  externalNavigationTargetRunPending,
  externalScrollToBottom,
  outlineToggleRef,
  autoModeEnabled = false,
  goalModeEnabled = false,
  onToggleAutoMode,
  onToggleGoalMode,
}: ChatViewProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { user } = useAuth();
  // 会话是否正在运行（存在流式消息或正在加载）——决定输入框显示发送还是停止
  const sessionRunning = isSessionRunning(messages, isLoading);
  const scheduledTasksRefreshKey = [
    sessionId ?? "",
    currentRunId ?? "",
    messages.length,
    isLoading ? "loading" : "idle",
  ].join(":");
  const hasVisibleStreamingMessage = messages.some(
    (message) => message.role === "assistant" && message.isStreaming,
  );

  // 是否在列表底部显示「流式生成中」骨架占位：连接正常、会话运行中，但还没有可见流式消息时
  const showStreamingFooterSkeleton = shouldShowStreamingFooterSkeleton({
    connectionStatus,
    sessionRunning,
    messageCount: messages.length,
    hasVisibleStreamingMessage,
  });

  // 根据当前小时选择问候语 i18n key（凌晨/上午/下午/晚上），用于欢迎页问候
  const getGreetingKey = () => {
    const h = new Date().getHours();
    if (h < 6) return "chat.goodEvening";
    if (h < 12) return "chat.goodMorning";
    if (h < 18) return "chat.goodAfternoon";
    return "chat.goodEvening";
  };
  const greeting = user?.username
    ? t(getGreetingKey(), { name: user.username })
    : t(getGreetingKey());

  const previousSessionIdRef = useRef<string | null | undefined>(sessionId);
  const [messageListSessionKey, setMessageListSessionKey] = useState(
    sessionId ?? "__new_session__",
  );
  const [visibleRange, setVisibleRange] = useState<ListRange | null>(null);

  // 消息滚动管理：容器/虚拟列表 refs、是否贴近顶/底、历史滚动稳定态，
  // 以及滚动到底/顶等方法；同时负责响应「外部导航」（跳转到某文件/某 run）。
  const {
    messagesContainerRef,
    virtuosoRef,
    virtuosoScrollerRef,
    messagesEndRef,
    isNearBottom,
    isNearTop,
    isHistoryScrollSettling,
    handleVirtuosoAtBottomChange,
    scrollToBottom,
    scrollToTop,
  } = useMessageScroll(
    messages,
    sessionId,
    externalNavigationToken,
    externalNavigationTargetFile,
    externalNavigationTargetRunId,
    externalNavigationTargetRunPending,
    externalScrollToBottom,
    isLoadingHistory,
    null,
  );

  // 计算虚拟列表的 key：切换会话时改用新 key 强制重建列表，避免沿用旧滚动位置
  useEffect(() => {
    const previousSessionId = previousSessionIdRef.current;
    previousSessionIdRef.current = sessionId;
    setMessageListSessionKey((previousKey) => {
      const nextKey = getNextMessageListSessionKey({
        previousSessionId,
        sessionId,
        messageCount: messages.length,
        previousKey,
      });
      return nextKey === previousKey ? previousKey : nextKey;
    });
  }, [messages.length, sessionId]);

  // --- Assistant identity ---
  const currentPersonaAvatar = useMemo(() => {
    const preset = personaPresets.find((p) => p.id === selectedPersonaPresetId);
    return preset?.avatar ?? null;
  }, [personaPresets, selectedPersonaPresetId]);
  const currentTeam = useCurrentTeam(currentAgent, selectedTeamId);
  // 解析当前助手身份（头像 + 名称）：优先取人设预设头像，其次团队/Agent
  const assistantIdentity = useMemo(
    () =>
      resolveChatAssistantIdentity({
        currentAgent,
        currentPersonaAvatar,
        currentTeam,
        selectedPersonaName,
      }),
    [currentAgent, currentPersonaAvatar, currentTeam, selectedPersonaName],
  );

  // --- Outline panel (side effects managed by hook) ---
  useChatOutline(
    messages,
    visibleRange,
    virtuosoRef,
    assistantIdentity.avatar,
    outlineToggleRef,
    t,
  );

  // --- Reveal preview ---
  const {
    activePreview,
    handleOpenPreview,
    handleClosePreview,
    handlePreviewInteraction,
    latestAutoPreview,
  } = useRevealPreview(
    messages,
    messagesContainerRef,
    scrollToBottom,
    isNearBottom,
    sessionId,
    externalNavigationToken,
    externalNavigationPreview,
    currentRunId,
    isLoadingHistory,
  );

  // --- Goal visibility ---
  const visibleActiveGoal = useMemo(
    () => getVisibleActiveGoalForMessages(activeGoal, messages),
    [activeGoal, messages],
  );
  const isMobileViewport =
    typeof window !== "undefined" ? window.innerWidth < 640 : false;
  const shouldHideHistoryMeasurementFrame =
    isLoadingHistory || isHistoryScrollSettling;

  // --- Message action handlers ---
  // 从某条消息处「分叉」出新会话：调用 API 成功后跳转到新会话
  const handleForkMessage = useCallback(
    async (messageId: string) => {
      if (!sessionId) return;
      try {
        const response = await sessionApi.forkMessage(sessionId, messageId);
        toast.success(t("chat.message.forkSuccess"));
        navigate(`/chat/${response.session.id}`);
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : t("chat.message.forkFailed"),
        );
      }
    },
    [navigate, sessionId, t],
  );

  // 重试被取消的消息：找到目标消息的内容与附件后重新发送（仅会话空闲且有发送权限时）
  const handleRetryCancelledMessage = useCallback(
    (messageId: string) => {
      if (sessionRunning || !canSendMessage) {
        return;
      }

      const target = findCancelledRetryTarget(messages, messageId);
      if (!target) {
        return;
      }

      onSendMessage(target.content, target.attachments);
    },
    [canSendMessage, messages, onSendMessage, sessionRunning],
  );

  // 点击推荐问题：把该问题直接作为新消息发送
  const handleRecommendQuestionClick = useCallback(
    (question: string) => {
      if (sessionRunning || !canSendMessage) {
        return;
      }
      onSendMessage(question);
    },
    [canSendMessage, onSendMessage, sessionRunning],
  );

  // --- Virtuoso rendering ---
  const handleVirtuosoRangeChanged = useCallback((range: ListRange) => {
    setVisibleRange((current) =>
      current?.startIndex === range.startIndex &&
      current?.endIndex === range.endIndex
        ? current
        : range,
    );
  }, []);
  const handleVirtuosoFollowOutput = useCallback(
    (isAtBottom: boolean) => {
      if (shouldHideHistoryMeasurementFrame) {
        return isAtBottom ? "auto" : false;
      }
      return isAtBottom ? "smooth" : false;
    },
    [shouldHideHistoryMeasurementFrame],
  );

  // 虚拟列表自定义组件：Scroller 绑定滚动容器 ref；Footer 放流式骨架与底部锚点
  const virtuosoComponents = useMemo(
    () => ({
      Scroller: (
        scrollerProps: React.HTMLAttributes<HTMLDivElement> & {
          children?: React.ReactNode;
          ref?: React.Ref<HTMLDivElement>;
        },
      ) => {
        const { children, ref: vRef, ...props } = scrollerProps;
        return (
          <div
            {...props}
            className={`chat-message-scroller ${props.className ?? ""}`}
            ref={(el: HTMLDivElement | null) => {
              virtuosoScrollerRef.current = el;
              if (typeof vRef === "function") vRef(el);
              else if (vRef)
                (
                  vRef as React.MutableRefObject<HTMLDivElement | null>
                ).current = el;
            }}
          >
            {children}
          </div>
        );
      },
      Footer: () => (
        <>
          {showStreamingFooterSkeleton && (
            <div className="pb-4">
              <ChatSkeletonMessagesOnly count={3} />
            </div>
          )}
          <div
            ref={messagesEndRef}
            className={getMessageListFooterSpacerClass(isMobileViewport)}
          />
        </>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showStreamingFooterSkeleton],
  );

  // 单个列表项的渲染：把每条 message 交给 ChatMessage，并传入预览/分叉/重试等回调
  const virtuosoItemContent = useCallback(
    (index: number, message: (typeof messages)[number]) => (
      <ChatMessage
        message={message}
        sessionId={sessionId ?? undefined}
        runId={currentRunId ?? undefined}
        isLastMessage={index === messages.length - 1}
        personaAvatar={assistantIdentity.avatar}
        personaName={assistantIdentity.name}
        activePreview={activePreview}
        latestAutoPreview={latestAutoPreview}
        onOpenPreview={handleOpenPreview}
        onForkMessage={handleForkMessage}
        onRecommendQuestionClick={handleRecommendQuestionClick}
        onRetryCancelledMessage={handleRetryCancelledMessage}
        activeGoal={
          getGoalForMessage(goalsByRunId, message) ?? visibleActiveGoal
        }
        isFirst={index === 0}
      />
    ),
    [
      sessionId,
      currentRunId,
      messages.length,
      assistantIdentity.avatar,
      assistantIdentity.name,
      activePreview,
      latestAutoPreview,
      handleOpenPreview,
      handleForkMessage,
      handleRecommendQuestionClick,
      handleRetryCancelledMessage,
      visibleActiveGoal,
      goalsByRunId,
    ],
  );

  // 底部输入框与欢迎页输入框共用的一组 props，集中构造以避免重复
  // Shared ChatInput props to avoid duplication
  const chatInputProps = {
    onSend: (
      content: string,
      _options?: Record<string, boolean | string | number>,
      sendAttachments?: MessageAttachment[],
      runOptions?: { enabledSkills?: string[] },
    ) => onSendMessage(content, sendAttachments, runOptions),
    onStop: onStopGeneration,
    isLoading: sessionRunning,
    canSend: canSendMessage,
    tools,
    onToggleTool,
    onToggleCategory,
    onToggleAll,
    toolsLoading,
    enabledToolsCount,
    totalToolsCount,
    skills,
    onToggleSkill,
    onToggleSkillCategory,
    onToggleAllSkills,
    skillsLoading,
    pendingSkillNames,
    skillsMutating,
    enabledSkillsCount,
    totalSkillsCount,
    enableSkills,
    personaPresets,
    personaPresetsTotal,
    personaPresetsPage,
    onPersonaPresetsPageChange,
    onPersonaPresetsSearchChange,
    onPersonaPresetsTagChange,
    selectedPersonaPresetId,
    selectedPersonaName,
    personaSkillsControlled,
    personaPresetsLoading,
    personaPresetsMutating,
    onUsePersonaPreset,
    onTogglePersonaPreference,
    onCopyPersonaPreset,
    onSavePersonaPreset,
    onClearPersonaPreset,
    canManagePersonaPresets,
    agentOptions,
    agentOptionValues,
    onToggleAgentOption,
    agents,
    currentAgent,
    onSelectAgent,
    selectedTeamId,
    onSelectTeam,
    onOpenTeamBuilder,
    attachments,
    onAttachmentsChange,
    autoModeEnabled,
    goalModeEnabled,
    onToggleAutoMode,
    onToggleGoalMode,
  };

  // 渲染：图库 Provider 包裹 → 主区（空态欢迎页 / 虚拟消息列表）→ 审批面板、
  // 预览宿主、附件预览、持久化工具面板 → 底部悬浮滚动按钮与 ChatInput。
  return (
    <SessionImageGalleryProvider messages={messages}>
      <main
        ref={messagesContainerRef}
        className="relative flex-1 min-h-0 overflow-hidden"
      >
        {/* Frosted glass fade mask — visual transition between messages and input */}
        <div
          className="pointer-events-none absolute bottom-0 left-0 right-0 z-10"
          style={{
            height: 48,
            background:
              "linear-gradient(to bottom, transparent, var(--theme-bg))",
          }}
        />
        {/* 空消息：加载中显示骨架，否则显示欢迎页；有消息则渲染虚拟列表 */}
        {messages.length === 0 ? (
          isLoading ? (
            <ChatSkeleton count={8} />
          ) : (
            <WelcomePage
              greeting={greeting}
              subtitle={t("chat.welcomeSubtitle", "How can I help you today?")}
              refreshLabel={t("chat.welcomeRefresh", "Refresh")}
              personasLabel={t("personaPresets.title", "Personas")}
              starterPromptsLabel={t(
                "personaPresets.starterPrompts",
                "Start a conversation",
              )}
              changePersonaLabel={t("personaPresets.change", "Change persona")}
              personaPresets={personaPresets}
              hasMorePersonaPresets={hasMorePersonaPresets}
              isLoadingMorePersonaPresets={isLoadingMorePersonaPresets}
              onLoadMorePersonaPresets={onLoadMorePersonaPresets}
              selectedPersonaPresetId={selectedPersonaPresetId}
              selectedPersonaSnapshot={selectedPersonaSnapshot}
              personaPresetsLoading={personaPresetsLoading}
              personaPresetsMutating={personaPresetsMutating}
              currentAgent={currentAgent}
              selectedTeamId={selectedTeamId}
              canSendMessage={canSendMessage}
              chatInputProps={chatInputProps}
              activeGoal={visibleActiveGoal}
              onClearActiveGoal={onClearActiveGoal}
              onUsePersonaPreset={onUsePersonaPreset}
              onClearPersonaPreset={onClearPersonaPreset}
              onSelectTeam={onSelectTeam}
            />
          )
        ) : (
          <>
            <Virtuoso
              key={messageListSessionKey}
              ref={virtuosoRef}
              className={`dark:divide-stone-800 overflow-x-hidden ${
                shouldHideHistoryMeasurementFrame
                  ? "chat-history-scroll-settling"
                  : ""
              }`}
              data={messages}
              computeItemKey={(_, message) => message.id}
              atBottomStateChange={handleVirtuosoAtBottomChange}
              atBottomThreshold={getAtBottomThresholdPx(isMobileViewport)}
              followOutput={handleVirtuosoFollowOutput}
              rangeChanged={handleVirtuosoRangeChanged}
              components={virtuosoComponents}
              itemContent={virtuosoItemContent}
              initialTopMostItemIndex={getInitialBottomItemLocation(
                messages.length,
              )}
            />
            {shouldHideHistoryMeasurementFrame && (
              <div className="chat-history-settling-overlay">
                <ChatSkeletonMessagesOnly count={8} />
              </div>
            )}
          </>
        )}
      </main>

      <ApprovalPanel
        approvals={approvals}
        onRespond={onRespondApproval}
        isLoading={approvalLoading}
      />

      <RevealPreviewHost
        preview={activePreview}
        onClose={() => handleClosePreview(true)}
        onUserInteraction={handlePreviewInteraction}
      />
      <AttachmentPreviewHost />
      <PersistentToolPanelHost />

      {/* ChatInput at bottom (when messages exist, WelcomePage renders its own) */}
      {messages.length > 0 && (
        <div className="relative">
          <div
            className={`absolute ${FLOATING_SCROLL_BUTTON_OFFSET_CLASS} right-2 z-50 flex flex-col gap-2 sm:right-4`}
          >
            <SessionScheduledTasksButton
              sessionId={sessionId}
              refreshKey={scheduledTasksRefreshKey}
              className="group/btn flex h-9 w-9 items-center justify-center rounded-full border border-[var(--theme-border)] bg-[var(--theme-bg-card)]/90 text-theme-text-secondary transition-all duration-300 hover:-translate-y-0.5 hover:bg-[var(--glass-bg-subtle)] hover:text-theme-text active:scale-95 sm:h-10 sm:w-10"
            />
            <button
              onClick={scrollToTop}
              className="group/btn flex h-9 w-9 items-center justify-center rounded-full border border-[var(--theme-border)] bg-[var(--theme-bg-card)]/90 transition-all duration-300 hover:-translate-y-0.5 active:scale-95 sm:h-10 sm:w-10"
              style={{
                opacity: isNearTop ? 0 : 1,
                transform: isNearTop ? "translateY(6px)" : "translateY(0)",
                pointerEvents: isNearTop ? "none" : "auto",
              }}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 20 20"
                fill="currentColor"
                className="w-4 h-4 sm:w-[18px] sm:h-[18px] text-[var(--theme-text-tertiary)] group-hover/btn:text-[var(--theme-text-secondary)] transition-colors duration-200"
              >
                <path
                  fillRule="evenodd"
                  d="M10 17a.75.75 0 01-.75-.75V5.612l-3.96 4.158a.75.75 0 11-1.08-1.04l5.25-5.5a.75.75 0 011.08 0l5.25 5.5a.75.75 0 11-1.08 1.04l-3.96-4.158V16.25A.75.75 0 0110 17z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
            <button
              onClick={scrollToBottom}
              className={`group/btn flex h-9 w-9 items-center justify-center rounded-full border border-[var(--theme-border)] bg-[var(--theme-bg-card)]/90 transition-all duration-300 hover:-translate-y-0.5 active:scale-95 sm:h-10 sm:w-10 ${
                hasVisibleStreamingMessage ? "scroll-btn-glow" : ""
              }`}
              style={{
                opacity: isNearBottom ? 0 : 1,
                transform: isNearBottom ? "translateY(6px)" : "translateY(0)",
                pointerEvents: isNearBottom ? "none" : "auto",
              }}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 20 20"
                fill="currentColor"
                className="w-4 h-4 sm:w-[18px] sm:h-[18px] text-[var(--theme-text-tertiary)] group-hover/btn:text-[var(--theme-text-secondary)] transition-colors duration-200"
              >
                <path
                  fillRule="evenodd"
                  d="M10 3a.75.75 0 01.75.75v10.638l3.96-4.158a.75.75 0 111.08 1.04l-5.25 5.5a.75.75 0 01-1.08 0l-5.25-5.5a.75.75 0 111.08-1.04l3.96 4.158V3.75A.75.75 0 0110 3z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
          </div>
          <ChatInput
            {...chatInputProps}
            activeGoal={visibleActiveGoal}
            onClearActiveGoal={onClearActiveGoal}
            goalLabel={t("chat.goal.active", "Goal")}
            goalDurationLabel={t("chat.goal.running", "Running")}
            goalClearLabel={t("chat.goal.clear", "Clear goal")}
            showHelpMenu
            helpMenuClassName="hidden sm:block"
          />
        </div>
      )}
    </SessionImageGalleryProvider>
  );
}
