import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { SessionSidebar } from "../../panels/SessionSidebar";
import { AppShell } from "./AppShell";
import { TabContent } from "./TabContent";
import type { TabType } from "./types";

// NonChatAppContent 的 props：与 ChatAppContent 相同的外壳共享状态，activeTab 限定为非 chat 标签
export interface NonChatAppContentProps {
  activeTab: Exclude<TabType, "chat">;
  showProfileModal: boolean;
  onCloseProfileModal: () => void;
  versionInfo: import("../../../types").VersionInfo | null;
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  mobileSidebarOpen: boolean;
  setMobileSidebarOpen: (open: boolean) => void;
  onShowProfile: () => void;
}

// 非聊天标签页的外壳装配：用 AppShell 包裹会话侧栏与 TabContent（各管理面板）。
// 侧栏里的「选择/新建会话」会导航回 /chat 路由，从而切回聊天标签。
export function NonChatAppContent({
  activeTab,
  showProfileModal,
  onCloseProfileModal,
  versionInfo,
  sidebarCollapsed,
  setSidebarCollapsed,
  mobileSidebarOpen,
  setMobileSidebarOpen,
  onShowProfile,
}: NonChatAppContentProps) {
  const navigate = useNavigate();

  // 在非聊天页选中某会话：关闭移动侧栏并跳转到该会话的聊天路由
  const handleSelectSession = useCallback(
    (id: string) => {
      setMobileSidebarOpen(false);
      navigate(`/chat/${id}`);
    },
    [navigate, setMobileSidebarOpen],
  );
  // 新建会话：关闭移动侧栏并跳转到 /chat
  const handleNewSession = useCallback(() => {
    setMobileSidebarOpen(false);
    navigate("/chat");
  }, [navigate, setMobileSidebarOpen]);
  const handleMobileClose = useCallback(
    () => setMobileSidebarOpen(false),
    [setMobileSidebarOpen],
  );

  // 渲染：外壳 + 会话侧栏 + 当前标签对应的面板内容（TabContent）
  return (
    <AppShell
      activeTab={activeTab}
      showProfileModal={showProfileModal}
      onCloseProfileModal={onCloseProfileModal}
      versionInfo={versionInfo}
      setMobileSidebarOpen={setMobileSidebarOpen}
      currentProjectId={null}
      projectManager={{ projects: [] }}
      onNewSession={handleNewSession}
      onShowProfile={onShowProfile}
      sidebar={
        <SessionSidebar
          currentSessionId={null}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
          mobileOpen={mobileSidebarOpen}
          onMobileClose={handleMobileClose}
          isCollapsed={sidebarCollapsed}
          onToggleCollapsed={setSidebarCollapsed}
          onShowProfile={onShowProfile}
        />
      }
    >
      <TabContent activeTab={activeTab} />
    </AppShell>
  );
}
