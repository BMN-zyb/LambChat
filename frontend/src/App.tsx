// 应用根组件与路由表：集中定义全部页面路由、鉴权/权限守卫、全局 Toast、
// PWA 更新提示与自动更新弹窗。
// 核心范式：所有受保护的业务页面都复用同一个 <AppContent activeTab="xxx">，
// 由 activeTab 决定当前展示哪个功能页，因此下面每个 XxxPage 都只是设置 SEO 后
// 渲染带不同 activeTab 的 AppContent。
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  Routes,
  Route,
  useParams,
  useNavigate,
  Navigate,
} from "react-router-dom";
import { Toaster, ToastBar, toast } from "react-hot-toast";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ProtectedRoute } from "./components/auth/ProtectedRoute";
import { ChatPageSkeleton, FilesPageSkeleton } from "./components/skeletons";
import { ThemeProvider } from "./contexts/ThemeContext";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { SelectionActionPopover } from "./components/common/SelectionActionPopover.tsx";
import { useSEO } from "./hooks/usePageTitle";
import { GITHUB_URL } from "./constants";
import { Permission } from "./types";
import { sessionApi } from "./services/api";
import {
  getCachedSessionTitle,
  listenSessionTitleUpdated,
} from "./utils/sessionTitleEvents";
import { APP_TOASTER_CLASS_NAME } from "./components/layout/AppContent/appToastLayout";
import { PwaStatusToasts } from "./components/pwa/PwaStatusToasts";
import { appNotificationService } from "./services/notifications/appNotificationService";
import { UpdateDialog } from "./components/update/UpdateDialog";
import { useAutoUpdate } from "./hooks/useAutoUpdate";

// 以下页面组件全部用 React.lazy 懒加载：配合 Suspense 做路由级代码分割，
// 首屏只加载必要 chunk，其余页面在导航到时按需拉取，减小初始包体积。
const SharedPage = lazy(() =>
  import("./components/share/SharedPage").then((m) => ({
    default: m.SharedPage,
  })),
);
const OAuthCallback = lazy(() =>
  import("./components/auth/OAuthCallback").then((m) => ({
    default: m.OAuthCallback,
  })),
);
const ForgotPassword = lazy(() =>
  import("./components/auth/ForgotPassword").then((m) => ({
    default: m.ForgotPassword,
  })),
);
const ResetPassword = lazy(() =>
  import("./components/auth/ResetPassword").then((m) => ({
    default: m.ResetPassword,
  })),
);
const VerifyEmail = lazy(() =>
  import("./components/auth/VerifyEmail").then((m) => ({
    default: m.VerifyEmail,
  })),
);
const RegistrationPending = lazy(() =>
  import("./components/auth/RegistrationPending").then((m) => ({
    default: m.RegistrationPending,
  })),
);
const LandingPage = lazy(() =>
  import("./components/landing/LandingPage").then((m) => ({
    default: m.LandingPage,
  })),
);
const AuthPage = lazy(() =>
  import("./components/auth/AuthPage").then((m) => ({ default: m.AuthPage })),
);
const AppContent = lazy(() =>
  import("./components/layout/AppContent/index").then((m) => ({
    default: m.AppContent,
  })),
);
const NotFoundPage = lazy(() =>
  import("./components/common/NotFoundPage").then((m) => ({
    default: m.NotFoundPage,
  })),
);

// 聊天页 SEO 组件：不渲染任何 UI（返回 null），仅负责根据当前会话动态
// 设置页面标题/描述。标题优先取会话名，取不到则用默认文案。
// 之所以拆成独立组件，是为了把「按 sessionId 拉取会话名」的副作用与聊天主体解耦。
function ChatPageSEO() {
  const { sessionId } = useParams<{ sessionId?: string }>();
  const [sessionName, setSessionName] = useState<string | null>(null);
  const prevSessionIdRef = useRef<string | null>(null);

  // Fetch session name when sessionId changes
  // 副作用一：sessionId 变化时拉取会话名。用 prevSessionIdRef 判断是否真的
  // 切换到了「不同」会话，避免同一会话重复清空标题造成闪烁。
  useEffect(() => {
    if (!sessionId) {
      setSessionName(null);
      prevSessionIdRef.current = null;
      return;
    }

    // Reset only when switching to a different session
    if (sessionId !== prevSessionIdRef.current) {
      setSessionName(null);
      prevSessionIdRef.current = sessionId;
    }

    const fetchSessionName = async () => {
      try {
        const session = await sessionApi.get(sessionId);
        if (session?.name) {
          setSessionName(session.name);
        }
      } catch (err) {
        console.warn("[ChatPage] Failed to fetch session:", err);
      }
    };

    fetchSessionName();
  }, [sessionId]);

  // React immediately when generateTitle finishes in the active chat session.
  // 副作用二：先读本地缓存的标题即时展示，再订阅「会话标题已更新」事件，
  // 使后端自动生成标题（generateTitle）完成后能立刻反映到页面标题，无需刷新。
  useEffect(() => {
    if (!sessionId) return;

    const cachedTitle = getCachedSessionTitle(sessionId);
    if (cachedTitle) {
      setSessionName(cachedTitle);
    }

    return listenSessionTitleUpdated((detail) => {
      if (detail.sessionId === sessionId) {
        setSessionName(detail.title);
      }
    });
  }, [sessionId]);

  // Poll for session name after initial load (handles race with generate-title)
  // 副作用三：兜底轮询。若首屏加载后仍拿不到会话名（与后端异步生成标题存在
  // 竞态），延迟 3 秒再查一次，成功则更新。
  useEffect(() => {
    if (!sessionId || sessionName) return;

    const delay = setTimeout(() => {
      sessionApi
        .get(sessionId)
        .then((session) => {
          if (session?.name) setSessionName(session.name);
        })
        .catch(() => {});
    }, 3000);

    return () => clearTimeout(delay);
  }, [sessionId, sessionName]);

  // Use session name if available, otherwise use default "nav.chat"
  useSEO({
    title: sessionName || "seo.chat.title",
    description: "seo.chat.description",
    path: sessionId ? `/chat/${sessionId}` : "/chat",
  });

  return null;
}

// Chat Page Component
// 聊天页：渲染 SEO 组件 + AppContent。key="chat" 保证从其它 tab 切回时
// AppContent 以聊天态重新挂载。
function ChatPage() {
  return (
    <>
      <ChatPageSEO />
      <AppContent key="chat" activeTab="chat" />
    </>
  );
}

// Simple page components that set the page title and render AppContent
// 下面这一组页面组件是同一套模板：设置各自 SEO，再渲染携带不同 activeTab 的
// AppContent。activeTab 就是 AppContent 内部切换功能视图（技能/市场/用户/设置…）
// 的开关，key 用于强制不同 tab 之间重新挂载、隔离状态。
function SkillsPage() {
  useSEO({
    title: "seo.skills.title",
    description: "seo.skills.description",
    path: "/skills",
  });
  return <AppContent key="skills" activeTab="skills" />;
}

function MarketplacePage() {
  useSEO({
    title: "seo.marketplace.title",
    description: "seo.marketplace.description",
    path: "/marketplace",
  });
  return <AppContent key="marketplace" activeTab="marketplace" />;
}

function UsersPage() {
  useSEO({
    title: "seo.users.title",
    description: "seo.users.description",
    path: "/users",
  });
  return <AppContent key="users" activeTab="users" />;
}

function RolesPage() {
  useSEO({
    title: "seo.roles.title",
    description: "seo.roles.description",
    path: "/roles",
  });
  return <AppContent key="roles" activeTab="roles" />;
}

function SettingsPage() {
  useSEO({
    title: "seo.settings.title",
    description: "seo.settings.description",
    path: "/settings",
  });
  return <AppContent key="settings" activeTab="settings" />;
}

function MCPPage() {
  useSEO({
    title: "seo.mcp.title",
    description: "seo.mcp.description",
    path: "/mcp",
  });
  return <AppContent key="mcp" activeTab="mcp" />;
}

function FeedbackPage() {
  useSEO({
    title: "seo.feedback.title",
    description: "seo.feedback.description",
    path: "/feedback",
  });
  return <AppContent key="feedback" activeTab="feedback" />;
}

function ChannelsPage() {
  useSEO({
    title: "seo.channels.title",
    description: "seo.channels.description",
    path: "/channels",
  });
  return <AppContent key="channels" activeTab="channels" />;
}

function AgentsPage() {
  useSEO({
    title: "seo.agents.title",
    description: "seo.agents.description",
    path: "/agents",
  });
  return <AppContent key="agents" activeTab="agents" />;
}

function FilesPage() {
  useSEO({
    title: "seo.files.title",
    description: "seo.files.description",
    path: "/files",
  });
  return <AppContent key="files" activeTab="files" />;
}

function TeamPage() {
  useSEO({
    title: "seo.team.title",
    description: "seo.team.description",
    path: "/team",
  });
  return <AppContent key="team" activeTab="team" />;
}

function PersonaPage() {
  useSEO({
    title: "seo.persona.title",
    description: "seo.persona.description",
    path: "/persona",
  });
  return <AppContent key="persona" activeTab="persona" />;
}

function NotificationsPage() {
  useSEO({
    title: "seo.notifications.title",
    description: "seo.notifications.description",
    path: "/notifications",
  });
  return <AppContent key="notifications" activeTab="notifications" />;
}

function MemoryPage() {
  useSEO({
    title: "seo.memory.title",
    description: "seo.memory.description",
    path: "/memory",
  });
  return <AppContent key="memory" activeTab="memory" />;
}

function ScheduledTasksPage() {
  useSEO({
    title: "seo.scheduledTasks.title",
    description: "seo.scheduledTasks.description",
    path: "/scheduled-tasks",
  });
  return <AppContent key="scheduled-tasks" activeTab="scheduled-tasks" />;
}

function UsagePage() {
  useSEO({
    title: "seo.usage.title",
    description: "seo.usage.description",
    path: "/usage",
  });
  return <AppContent key="usage" activeTab="usage" />;
}

function GitHubPage() {
  useSEO({
    title: "LambChat GitHub",
    description: "seo.landing.description",
    path: "/github",
    omitSuffix: true,
  });

  useEffect(() => {
    window.location.replace(GITHUB_URL);
  }, []);

  return null;
}

// Auth page wrapper - redirects to /chat after successful login/register
// 登录/注册页包装：登录成功后跳转。若鉴权守卫携带了原始目标路径
// （redirectPath），登录后回跳到该路径，否则默认进入 /chat；replace 避免
// 用户点返回又回到登录页。
function AuthPageWrapper({
  initialMode,
}: {
  initialMode?: "login" | "register";
}) {
  const navigate = useNavigate();
  useSEO({
    title: initialMode === "register" ? "auth.register" : "auth.login",
    path: initialMode === "register" ? "/auth/register" : "/auth/login",
    noindex: true,
  });
  return (
    <AuthPage
      initialMode={initialMode}
      onSuccess={(redirectPath) =>
        navigate(redirectPath ?? "/chat", { replace: true })
      }
    />
  );
}

// Main App Component
function App() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  // Auto-update for desktop and mobile
  // 桌面端(Tauri)与移动端(Capacitor)的自动更新：获取更新状态与操作方法。
  const {
    state: updateState,
    showDialog: showUpdateDialog,
    setShowDialog: setShowUpdateDialog,
    startUpdate,
    skipUpdate,
  } = useAutoUpdate();
  // 运行时探测宿主平台：优先识别 Tauri（桌面壳），再识别 Capacitor（iOS/Android），
  // 都不是则视为普通 Web。用于给更新弹窗提供正确的平台文案与升级方式。
  const updatePlatform = (() => {
    if (typeof window === "undefined") return "web";
    const win = window as unknown as Record<string, unknown>;
    if (win.__TAURI__ || win.__TAURI_INTERNALS__) return "tauri";
    if (typeof win.Capacitor !== "undefined") {
      const cap = win.Capacitor as Record<string, unknown>;
      const p = typeof cap.getPlatform === "function" ? cap.getPlatform() : "";
      if (p === "ios") return "ios";
      if (p === "android") return "android";
    }
    return "web";
  })();

  // 把「点击通知后要跳转到哪个路由」的能力注入通知服务：通知服务本身在 React
  // 树之外，无法直接用 useNavigate，故在此把 navigate 注册进去，并初始化原生端
  // 通知点击处理；卸载时解绑，避免内存泄漏。
  useEffect(() => {
    appNotificationService.setNavigator((route) => {
      navigate(route, { replace: false });
    });
    appNotificationService.initializeNativeClickHandlers();
    return () => appNotificationService.setNavigator(null);
  }, [navigate]);

  return (
    <ThemeProvider>
      <ErrorBoundary>
        <Toaster
          position="top-center"
          containerClassName={APP_TOASTER_CLASS_NAME}
          containerStyle={{
            top: "calc(56px + var(--app-safe-area-top, 0px))",
          }}
          toastOptions={{
            duration: 4000,
            style: {
              background: "#333",
              color: "#fff",
              borderRadius: "8px",
              padding: "12px 16px",
              minWidth: "280px",
            },
            success: {
              duration: 3000,
              iconTheme: {
                primary: "#22c55e",
                secondary: "#fff",
              },
            },
            error: {
              duration: 5000,
              iconTheme: {
                primary: "#ef4444",
                secondary: "#fff",
              },
            },
          }}
        >
          {(currentToast) => {
            if (currentToast.type === "custom") {
              return <ToastBar toast={currentToast} />;
            }

            return (
              <ToastBar toast={currentToast}>
                {({ icon, message }) => (
                  <div className="flex w-full items-center gap-3 text-left">
                    <span className="flex shrink-0 items-center">{icon}</span>
                    <div className="min-w-0 flex-1 leading-snug">{message}</div>
                    <button
                      type="button"
                      className="-mr-1 inline-flex size-7 shrink-0 items-center justify-center rounded-full text-white/60 transition-colors hover:bg-white/10 hover:text-white focus:outline-none focus:ring-2 focus:ring-white/30"
                      aria-label={t("common.dismiss", "关闭")}
                      onClick={(event) => {
                        event.stopPropagation();
                        toast.dismiss(currentToast.id);
                      }}
                    >
                      <X size={14} aria-hidden="true" />
                    </button>
                  </div>
                )}
              </ToastBar>
            );
          }}
        </Toaster>
        <PwaStatusToasts />
        {showUpdateDialog && updateState.available && (
          <UpdateDialog
            state={updateState}
            isOpen={showUpdateDialog}
            onUpgrade={startUpdate}
            onSkip={skipUpdate}
            onDismiss={() => setShowUpdateDialog(false)}
            platform={updatePlatform as "tauri" | "android" | "ios"}
          />
        )}
        <SelectionActionPopover />
        {/* 路由表：公开路由（落地页/登录/OAuth 回调/找回密码/分享页）可直接访问；
            业务路由统一用 <ProtectedRoute> 包裹做登录校验，部分再通过 permissions
            做细粒度权限守卫——无权限时按 redirectTo 跳转并可弹 toast 提示。
            外层 Suspense 承接懒加载页面的加载态。 */}
        <Suspense fallback={<ChatPageSkeleton />}>
          <Routes>
            <Route path="/" element={<LandingPage />} />
            <Route path="/interface" element={<LandingPage />} />
            <Route path="/features" element={<LandingPage />} />
            <Route path="/architecture" element={<LandingPage />} />
            <Route path="/dashboard" element={<LandingPage />} />
            <Route path="/responsive" element={<LandingPage />} />
            <Route path="/github" element={<GitHubPage />} />
            {/* Auth routes */}
            <Route path="/auth/login" element={<AuthPageWrapper />} />
            <Route
              path="/auth/register"
              element={<AuthPageWrapper initialMode="register" />}
            />
            <Route
              path="/chat/:sessionId?"
              element={
                <ProtectedRoute>
                  <ChatPage />
                </ProtectedRoute>
              }
            />
            {/* 带权限的受保护路由示例：除登录外还要求 permissions 指定的权限，
                校验不通过则重定向到 redirectTo 并弹出 toastMessage 提示。
                下方 marketplace/mcp/users/roles/settings 等均沿用此模式。 */}
            <Route
              path="/skills"
              element={
                <ProtectedRoute
                  permissions={[
                    Permission.SKILL_READ,
                    Permission.MARKETPLACE_READ,
                  ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <SkillsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/marketplace"
              element={
                <ProtectedRoute
                  permissions={[
                    Permission.SKILL_READ,
                    Permission.MARKETPLACE_READ,
                  ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <MarketplacePage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/mcp"
              element={
                <ProtectedRoute
                  permissions={[Permission.MCP_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <MCPPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/users"
              element={
                <ProtectedRoute
                  permissions={[Permission.USER_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <UsersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/roles"
              element={
                <ProtectedRoute
                  permissions={[Permission.ROLE_MANAGE]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <RolesPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/settings"
              element={
                <ProtectedRoute
                  permissions={[Permission.SETTINGS_MANAGE]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <SettingsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/feedback"
              element={
                <ProtectedRoute
                  permissions={[Permission.FEEDBACK_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <FeedbackPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/channels/:channelType?/:instanceId?"
              element={
                <ProtectedRoute
                  permissions={[Permission.CHANNEL_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <ChannelsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/agents"
              element={
                <ProtectedRoute>
                  <AgentsPage />
                </ProtectedRoute>
              }
            />
            {/* 旧路径 /models 已并入 /agents，永久重定向以兼容历史链接 */}
            <Route path="/models" element={<Navigate to="/agents" replace />} />
            <Route
              path="/team"
              element={
                <ProtectedRoute permissions={[Permission.TEAM_READ]}>
                  <TeamPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/persona"
              element={
                <ProtectedRoute>
                  <PersonaPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/files"
              element={
                <ProtectedRoute loadingComponent={<FilesPageSkeleton />}>
                  <FilesPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/notifications"
              element={
                <ProtectedRoute
                  permissions={[Permission.NOTIFICATION_MANAGE]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <NotificationsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/memory"
              element={
                <ProtectedRoute>
                  <MemoryPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/scheduled-tasks"
              element={
                <ProtectedRoute
                  permissions={[Permission.SCHEDULED_TASK_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <ScheduledTasksPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/usage"
              element={
                <ProtectedRoute
                  permissions={[Permission.USAGE_READ]}
                  redirectTo="/chat"
                  showToast
                  toastMessage={t("errors.noPermission")}
                >
                  <UsagePage />
                </ProtectedRoute>
              }
            />
            {/* OAuth callback page - handles OAuth redirect from backend */}
            <Route path="/auth/callback" element={<OAuthCallback />} />
            {/* Password reset pages - no auth required */}
            <Route path="/auth/reset-request" element={<ForgotPassword />} />
            <Route path="/auth/reset-password" element={<ResetPassword />} />
            {/* Email verification page - no auth required */}
            <Route path="/auth/verify-email" element={<VerifyEmail />} />
            {/* Registration pending verification page - no auth required */}
            <Route path="/auth/pending" element={<RegistrationPending />} />
            {/* Public shared session page - no auth required */}
            <Route
              path="/shared/:shareId"
              element={
                <Suspense fallback={null}>
                  <SharedPage />
                </Suspense>
              }
            />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </ThemeProvider>
  );
}

export default App;
