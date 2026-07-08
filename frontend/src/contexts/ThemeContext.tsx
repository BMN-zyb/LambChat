// 主题 Context：管理明/暗主题。三处同步来源——本地存储、系统偏好、后端用户元数据，
// 并把主题应用到 document（切换 class/属性）。通过 useTheme 消费。
import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useState,
  type ReactNode,
} from "react";
import { authApi } from "../services/api";
import {
  applyThemeToDocument,
  getInitialThemePreference,
  isTheme,
  THEME_STORAGE_KEY,
  type Theme,
} from "../utils/themeDom";

interface ThemeContextType {
  theme: Theme;
  toggleTheme: () => void;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

interface ThemeProviderProps {
  children: ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  // 初始主题：从本地存储/系统偏好推导（getInitialThemePreference），避免首屏闪烁。
  const [theme, setThemeState] = useState<Theme>(getInitialThemePreference);

  // 用 useLayoutEffect（而非 useEffect）在浏览器绘制前把主题写入 document，
  // 防止先渲染旧主题再切换造成的闪屏(FOUC)。
  useLayoutEffect(() => {
    applyThemeToDocument(theme);
  }, [theme]);

  // 主题变化时：写入本地存储持久化，并「非阻塞」地同步到后端用户元数据（失败静默）。
  useEffect(() => {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
    // Sync to backend (non-blocking)
    authApi.updateMetadata({ theme }).catch(() => {});
  }, [theme]);

  // Listen for system preference changes
  // 监听系统深浅色偏好变化：仅当用户从未显式设置过主题(localStorage 为空)时才跟随系统，
  // 尊重用户的手动选择。
  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = (e: MediaQueryListEvent) => {
      const stored = localStorage.getItem(THEME_STORAGE_KEY);
      // Only auto-switch if user hasn't explicitly set a preference
      if (!stored) {
        setThemeState(e.matches ? "dark" : "light");
      }
    };

    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, []);

  // Listen for external theme changes (e.g. from auth login restoring backend preferences)
  // 监听外部主题变更事件：例如登录后用后端保存的偏好覆盖当前主题，
  // 通过自定义事件 "theme:external-change" 解耦，避免直接依赖鉴权模块。
  useEffect(() => {
    const handleExternalThemeChange = (e: Event) => {
      const newTheme = (e as CustomEvent<string>).detail;
      if (isTheme(newTheme)) {
        setThemeState(newTheme);
      }
    };
    window.addEventListener("theme:external-change", handleExternalThemeChange);
    return () =>
      window.removeEventListener(
        "theme:external-change",
        handleExternalThemeChange,
      );
  }, []);

  const toggleTheme = () => {
    setThemeState((prev) => (prev === "light" ? "dark" : "light"));
  };

  const setTheme = (newTheme: Theme) => {
    setThemeState(newTheme);
  };

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme(): ThemeContextType {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
}
