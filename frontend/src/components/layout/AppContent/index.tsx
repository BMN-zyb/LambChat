import { useCallback, useEffect, useRef, useState } from "react";
import { useVersion } from "../../../hooks/useVersion";
import { SIDEBAR_COLLAPSED_STORAGE_KEY } from "../../../hooks/useAuth";
import { authApi } from "../../../services/api";
import { ChatAppContent } from "./ChatAppContent";
import { NonChatAppContent } from "./NonChatAppContent";
import {
  APP_TOAST_SIDEBAR_OFFSET_VAR,
  getAppToastSidebarOffset,
} from "./appToastLayout";
import type { TabType } from "./types";
import { useRightPanelAutoCollapse } from "./useRightPanelAutoCollapse";

// AppContent 的 props：activeTab 决定当前展示哪个内容区（chat 或其他管理面板）
interface AppContentProps {
  activeTab: TabType;
}

// 应用内容区顶层组件（真正的应用外壳装配入口）。
// 根据 activeTab 在「聊天主界面」与「其他标签页面板」之间切换，
// 并统一持有跨内容区共享的外壳状态：左侧栏折叠、移动端侧栏开关、个人资料弹窗。
export function AppContent({ activeTab }: AppContentProps) {
  const { versionInfo } = useVersion();
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  // 左侧栏折叠状态：初始值从 localStorage 恢复，无记录时默认折叠（true）
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
    return saved !== null ? saved === "true" : true;
  });
  const [showProfileModal, setShowProfileModal] = useState(false);
  const autoCollapsePendingRef = useRef(false);

  // 统一的「设置左侧栏折叠」处理器：除更新 state 外，还负责持久化到
  // localStorage 与用户 metadata，并识别用户「手动展开」这一覆盖行为。
  const handleSetSidebarCollapsed = useCallback(
    (collapsed: boolean | ((prev: boolean) => boolean)) => {
      setSidebarCollapsed((prev) => {
        const next =
          typeof collapsed === "function" ? collapsed(prev) : collapsed;

        // 判断是否为「用户主动覆盖自动折叠」：当右侧面板较宽、且本次展开
        // 不是由自动折叠逻辑触发时，派发 override 事件通知自动折叠逻辑停手
        // Detect user override: user expanded left sidebar while right panel
        // is wide and this wasn't triggered by our auto-collapse logic.
        if (
          !autoCollapsePendingRef.current &&
          typeof collapsed !== "function" &&
          next === false &&
          prev === true
        ) {
          const html = document.documentElement;
          let rightWidth = 0;
          if (html.getAttribute("data-sidebar-preview") === "open") {
            rightWidth += parseInt(
              localStorage.getItem("sidebar-preview-width") || "60",
              10,
            );
          }
          if (html.getAttribute("data-editor-sidebar") === "open") {
            rightWidth += parseInt(
              localStorage.getItem("editor-sidebar-width") || "30",
              10,
            );
          }
          if (rightWidth >= 50) {
            window.dispatchEvent(
              new CustomEvent("right-panel-auto-collapse:override"),
            );
          }
        }

        // 持久化折叠状态：本地存一份，同时异步同步到用户 metadata（失败静默忽略）
        localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(next));
        authApi
          .updateMetadata({ sidebarCollapsed: String(next) })
          .catch(() => {});
        return next;
      });
    },
    [],
  );

  // 右侧面板（预览/编辑器）展开到较宽时自动折叠左侧栏腾出空间；
  // 用 autoCollapsePendingRef 标记，避免把这次自动折叠误判为用户主动操作
  useRightPanelAutoCollapse((collapsed) => {
    autoCollapsePendingRef.current = true;
    handleSetSidebarCollapsed(collapsed);
  });

  useEffect(() => {
    if (autoCollapsePendingRef.current) {
      queueMicrotask(() => {
        autoCollapsePendingRef.current = false;
      });
    }
  });

  // 监听外部来源（如侧栏组件自身）派发的折叠变更事件，保持状态同步
  useEffect(() => {
    const handler = (e: Event) => {
      const collapsed = (e as CustomEvent).detail as boolean;
      setSidebarCollapsed(collapsed);
    };
    window.addEventListener("sidebar-collapsed-changed", handler);
    return () =>
      window.removeEventListener("sidebar-collapsed-changed", handler);
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return undefined;

    const rootStyle = document.documentElement.style;
    rootStyle.setProperty(
      APP_TOAST_SIDEBAR_OFFSET_VAR,
      getAppToastSidebarOffset({ sidebarCollapsed }),
    );

    return () => {
      rootStyle.removeProperty(APP_TOAST_SIDEBAR_OFFSET_VAR);
    };
  }, [sidebarCollapsed]);

  const handleCloseProfileModal = useCallback(
    () => setShowProfileModal(false),
    [],
  );
  const handleShowProfile = useCallback(() => setShowProfileModal(true), []);

  // activeTab 为 chat 时渲染聊天主界面装配组件；否则渲染其他标签页外壳
  if (activeTab === "chat") {
    return (
      <ChatAppContent
        showProfileModal={showProfileModal}
        onCloseProfileModal={handleCloseProfileModal}
        versionInfo={versionInfo}
        sidebarCollapsed={sidebarCollapsed}
        setSidebarCollapsed={handleSetSidebarCollapsed}
        mobileSidebarOpen={mobileSidebarOpen}
        setMobileSidebarOpen={setMobileSidebarOpen}
        onShowProfile={handleShowProfile}
      />
    );
  }

  return (
    <NonChatAppContent
      activeTab={activeTab}
      showProfileModal={showProfileModal}
      onCloseProfileModal={handleCloseProfileModal}
      versionInfo={versionInfo}
      sidebarCollapsed={sidebarCollapsed}
      setSidebarCollapsed={handleSetSidebarCollapsed}
      mobileSidebarOpen={mobileSidebarOpen}
      setMobileSidebarOpen={setMobileSidebarOpen}
      onShowProfile={handleShowProfile}
    />
  );
}
