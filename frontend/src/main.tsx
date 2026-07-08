// 应用入口文件：挂载 React 根节点、注入全局样式与 i18n、注册 PWA、
// 并按「路由 -> 鉴权 -> 全局设置 -> 应用」的顺序组装最外层 Provider 树。
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "katex/dist/katex.min.css";
import "./i18n";
import App from "./App.tsx";
import "./styles/tailwind.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/animations.css";
import "./styles/components.css";
import "./styles/auth.css";
import "./styles/chat.css";
import "./styles/skill.css";
import "./styles/glass.css";
import "./styles/card-base.css";
import "./styles/marketplace.css";
import "./styles/persona.css";
import "./styles/team.css";
import "./styles/welcome.css";
import "./styles/approval.css";
import "./styles/landing.css";
import "./styles/syntax-highlight.css";
import "./styles/markdown.css";
import "./styles/pwa.css";
import "./styles/utilities.css";
import { AuthProvider } from "./hooks/useAuth";
import { SettingsProvider } from "./contexts/SettingsContext";
import { installMobileViewportResetHandlers } from "./utils/mobile";
import { registerLambChatPwa } from "./pwa";

// 修复移动端与浏览器通知交互后视口被放大、无法自动复位的问题
// Fix mobile viewport zoom issue after notification interaction
// This prevents the page from staying zoomed in after clicking browser notifications
installMobileViewportResetHandlers();

// 注册 PWA Service Worker（仅生产环境生效），启用离线缓存与更新提示
registerLambChatPwa();

// 开发时临时禁用 StrictMode 避免 SSE 双重连接问题
// 生产环境可以重新启用
// Provider 由外到内的顺序有讲究：
//   BrowserRouter（路由上下文，供内部所有 useNavigate/useParams 使用）
//   -> AuthProvider（登录态/JWT，鉴权守卫依赖它）
//   -> SettingsProvider（全局用户设置）
//   -> App（业务根组件）
createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <AuthProvider>
      <SettingsProvider>
        <App />
      </SettingsProvider>
    </AuthProvider>
  </BrowserRouter>,
);
