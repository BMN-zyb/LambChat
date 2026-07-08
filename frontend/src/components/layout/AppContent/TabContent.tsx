import { Suspense, lazy, type ReactNode } from "react";
import {
  SkillsPanelSkeleton,
  MarketplacePanelSkeleton,
  UsersPanelSkeleton,
  RolesPanelSkeleton,
  MCPPanelSkeleton,
  FeedbackPanelSkeleton,
  ScheduledTaskPanelSkeleton,
  ChannelsGridSkeleton,
  AgentModelPanelSkeleton,
  UsagePanelSkeleton,
} from "../../skeletons";
import { PanelLoadingState } from "../../common/PanelLoadingState";
import type { TabType } from "./types";

// 各管理面板按需懒加载（lazy + Suspense）：首屏只加载聊天所需代码，切到对应标签才拉取
const SkillsHubPanel = lazy(() =>
  import("../../panels/SkillsHubPanel").then((m) => ({
    default: m.SkillsHubPanel,
  })),
);
const UsersPanel = lazy(() =>
  import("../../panels/UsersPanel").then((m) => ({ default: m.UsersPanel })),
);
const RolesPanel = lazy(() =>
  import("../../panels/RolesPanel").then((m) => ({ default: m.RolesPanel })),
);
const SettingsPanel = lazy(() =>
  import("../../panels/SettingsPanel").then((m) => ({
    default: m.SettingsPanel,
  })),
);
const AgentModelPanel = lazy(() =>
  import("../../panels/AgentModelPanel").then((m) => ({
    default: m.AgentModelPanel,
  })),
);
const MCPPanel = lazy(() =>
  import("../../panels/MCPPanel").then((m) => ({ default: m.MCPPanel })),
);
const FeedbackPanel = lazy(() =>
  import("../../panels/FeedbackPanel").then((m) => ({
    default: m.FeedbackPanel,
  })),
);
const ChannelsPage = lazy(() =>
  import("../../pages/ChannelsPage").then((m) => ({ default: m.ChannelsPage })),
);
const RevealedFilesPage = lazy(() =>
  import("../../fileLibrary/RevealedFilesPanel").then((m) => ({
    default: m.RevealedFilesPanel,
  })),
);
const NotificationPanel = lazy(() =>
  import("../../panels/NotificationPanel").then((m) => ({
    default: m.NotificationPanel,
  })),
);
const MemoryPanel = lazy(() =>
  import("../../panels/MemoryPanel").then((m) => ({
    default: m.MemoryPanel,
  })),
);
const ScheduledTaskPanel = lazy(() =>
  import("../../panels/ScheduledTaskPanel").then((m) => ({
    default: m.ScheduledTaskPanel,
  })),
);
const PersonaPlazaPanel = lazy(() =>
  import("../../persona/PersonaPlazaPanel").then((m) => ({
    default: m.PersonaPlazaPanel,
  })),
);
const TeamBuilderPanel = lazy(() =>
  import("../../team/TeamBuilderWrapper").then((m) => ({
    default: m.TeamBuilderWrapper,
  })),
);
const UsagePanel = lazy(() =>
  import("../../panels/UsagePanel").then((m) => ({
    default: m.UsagePanel,
  })),
);

// 标签 → 面板组件的映射表（TabContent 据此选择渲染哪个懒加载面板）
const panelMap: Record<
  string,
  React.LazyExoticComponent<React.ComponentType>
> = {
  skills: SkillsHubPanel,
  marketplace: SkillsHubPanel,
  users: UsersPanel,
  roles: RolesPanel,
  settings: SettingsPanel,
  mcp: MCPPanel,
  feedback: FeedbackPanel,
  channels: ChannelsPage,
  agents: AgentModelPanel,
  files: RevealedFilesPage,
  persona: PersonaPlazaPanel,
  team: TeamBuilderPanel,
  notifications: NotificationPanel,
  memory: MemoryPanel,
  "scheduled-tasks": ScheduledTaskPanel,
  usage: UsagePanel,
};

// 标签 → 加载骨架的映射表（Suspense 回退时展示，缺省时用通用 PanelLoadingState）
const skeletonMap: Partial<Record<TabType, ReactNode>> = {
  skills: <SkillsPanelSkeleton />,
  marketplace: <MarketplacePanelSkeleton />,
  users: <UsersPanelSkeleton />,
  roles: <RolesPanelSkeleton />,
  mcp: <MCPPanelSkeleton />,
  feedback: <FeedbackPanelSkeleton />,
  "scheduled-tasks": <ScheduledTaskPanelSkeleton />,
  channels: <ChannelsGridSkeleton />,
  agents: <AgentModelPanelSkeleton />,
  usage: <UsagePanelSkeleton />,
};

// 根据 activeTab 渲染对应管理面板：chat 返回 null（由 ChatView 负责），未知标签也返回 null；
// 否则用 Suspense 包裹懒加载面板，并在加载期间展示对应骨架。
export function TabContent({ activeTab }: { activeTab: TabType }) {
  if (activeTab === "chat") return null;

  const Panel = panelMap[activeTab];
  if (!Panel) return null;

  return (
    <main className="flex-1 overflow-hidden bg-[var(--theme-bg)]">
      <div className="mx-auto w-full h-full flex flex-col overflow-hidden lg:max-w-[80rem] xl:max-w-[96rem] 2xl:max-w-[120rem] sm:px-4">
        <Suspense fallback={skeletonMap[activeTab] ?? <PanelLoadingState />}>
          <Panel />
        </Suspense>
      </div>
    </main>
  );
}
