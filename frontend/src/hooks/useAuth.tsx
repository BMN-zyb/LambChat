/**
 * 认证上下文和 Hook
 * 提供全局认证状态管理
 *
 * 职责：集中管理登录态（user/token）、动态权限、以及登录/注册/OAuth/登出等操作，
 * 并向全应用暴露权限判断方法（hasPermission / hasAny / hasAll）。
 * 与 services/api 的分工：本文件只管「React 层状态」，token 的实际读写/刷新在 api 层；
 * 二者通过 "auth:logout" 等 window 事件解耦联动。
 */

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import {
  authApi,
  buildOAuthLoginUrl,
  getAccessToken,
  getRefreshToken,
  isAuthenticated,
  isTokenExpired,
  getRedirectPath,
  clearRedirectPath,
} from "../services/api";
import {
  applyUserMetadataPreferences,
  SIDEBAR_COLLAPSED_STORAGE_KEY,
} from "./userMetadataPreferences";
import { Permission } from "../types";
import type { User, UserCreate, LoginRequest, AuthState } from "../types";
import i18n from "../i18n";

export { SIDEBAR_COLLAPSED_STORAGE_KEY };

/** Apply user metadata preferences from backend */
function applyUserMetadata(metadata?: User["metadata"]) {
  applyUserMetadataPreferences({
    metadata,
    localStorage,
    changeLanguage: (language) => {
      i18n.changeLanguage(language);
    },
    dispatchEvent: (event) => {
      window.dispatchEvent(event);
    },
  });
}

// 认证上下文类型
interface AuthContextType extends AuthState {
  login: (
    credentials: LoginRequest,
    turnstileToken?: string,
  ) => Promise<string | null>;
  register: (
    userData: UserCreate,
    turnstileToken?: string,
  ) => Promise<{ requiresVerification: boolean; email: string }>;
  loginWithOAuth: (provider: string) => Promise<void>;
  handleOAuthCallback: (
    provider: string,
    code: string,
    state: string,
  ) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
  hasPermission: (permission: Permission) => boolean;
  hasAnyPermission: (permissions: Permission[]) => boolean;
  hasAllPermissions: (permissions: Permission[]) => boolean;
}

// 创建认证上下文
const AuthContext = createContext<AuthContextType | null>(null);

// Auth Provider 组件
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(getAccessToken());
  const [isLoading, setIsLoading] = useState(true);
  // 存储从 API 获取的动态权限
  // 后端返回的权限是字符串，写入前会用 Object.values(Permission) 过滤，
  // 只保留前端已知的合法枚举值，避免脏数据污染权限判断。
  const [dynamicPermissions, setDynamicPermissions] = useState<Permission[]>(
    [],
  );

  // 权限列表：从 API 动态获取
  const permissions = dynamicPermissions;

  // 初始化：检查现有 token 并获取用户信息
  // 刷新策略：access token 未过期则直接用；已过期则看 refresh token 是否可用——
  // 可用则调 refreshToken 换新，不可用/刷新失败则登出并要求重新登录。
  // 这是「进入应用时」的一次性校验；请求过程中的 401 自动刷新由 api 层的 authFetch 处理。
  useEffect(() => {
    const initAuth = async () => {
      const accessToken = getAccessToken();

      if (!accessToken) {
        setIsLoading(false);
        return;
      }

      let validToken = accessToken;

      // 检查 token 是否过期，尝试用 refresh token 刷新
      if (isTokenExpired(accessToken)) {
        const refreshToken = getRefreshToken();
        if (refreshToken && !isTokenExpired(refreshToken)) {
          try {
            const tokenResponse = await authApi.refreshToken();
            validToken = tokenResponse.access_token;
          } catch {
            // 刷新失败，需要重新登录
            authApi.logout();
            setToken(null);
            setUser(null);
            setIsLoading(false);
            return;
          }
        } else {
          // refresh token 也不存在或已过期，需要重新登录
          authApi.logout();
          setToken(null);
          setUser(null);
          setIsLoading(false);
          return;
        }
      }

      setToken(validToken);

      // 尝试获取用户信息（带重试）
      try {
        const currentUser = await authApi.getCurrentUser();
        setUser(currentUser);
        applyUserMetadata(currentUser.metadata);
        // 更新动态权限
        if (currentUser.permissions) {
          setDynamicPermissions(
            currentUser.permissions.filter((p): p is Permission =>
              Object.values(Permission).includes(p as Permission),
            ),
          );
        }
      } catch (err) {
        // Only treat 401 as auth failure — network errors / server restarts
        // should NOT clear auth state during development.
        // authFetch already handles 401 by calling redirectToLogin internally,
        // so this catch only fires for non-401 errors.
        console.warn("[useAuth] Failed to fetch current user:", err);
      }

      setIsLoading(false);
    };

    initAuth();
  }, []);

  // 监听登出事件
  // api 层在 refresh token 也失效时会派发 "auth:logout" 全局事件，
  // 这里据此清空本地登录态，实现「底层鉴权失败 -> 上层 UI 登出」的联动。
  useEffect(() => {
    const handleLogout = () => {
      setToken(null);
      setUser(null);
    };

    window.addEventListener("auth:logout", handleLogout);
    return () => window.removeEventListener("auth:logout", handleLogout);
  }, []);

  // 登录
  const login = useCallback(
    async (credentials: LoginRequest, turnstileToken?: string) => {
      setIsLoading(true);
      try {
        await authApi.login(credentials, turnstileToken);
        const accessToken = getAccessToken();
        setToken(accessToken);

        // 获取用户信息
        try {
          const currentUser = await authApi.getCurrentUser();
          setUser(currentUser);
          applyUserMetadata(currentUser.metadata);
          // 更新动态权限
          if (currentUser.permissions) {
            setDynamicPermissions(
              currentUser.permissions.filter((p): p is Permission =>
                Object.values(Permission).includes(p as Permission),
              ),
            );
          }
        } catch {
          // 获取用户信息失败，清除登录状态
          authApi.logout();
          setToken(null);
          setIsLoading(false);
          return null;
        }

        // 登录成功后，跳转到之前的页面
        const redirectPath = getRedirectPath();
        if (redirectPath) {
          clearRedirectPath();
        }
        return redirectPath ?? null;
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  // 注册
  const register = useCallback(
    async (
      userData: UserCreate,
      turnstileToken?: string,
    ): Promise<{ requiresVerification: boolean; email: string }> => {
      setIsLoading(true);
      try {
        const response = await authApi.register(userData, turnstileToken);
        return {
          requiresVerification: response.requires_verification,
          email: userData.email,
        };
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  // OAuth 登录 - 直接导航到后端 OAuth 端点，由服务端重定向到提供商
  const loginWithOAuth = useCallback(async (provider: string) => {
    window.location.href = buildOAuthLoginUrl(provider);
  }, []);

  // 处理 OAuth 回调
  const handleOAuthCallback = useCallback(
    async (provider: string, code: string, state: string) => {
      setIsLoading(true);
      try {
        await authApi.handleOAuthCallback(provider, code, state);
        const accessToken = getAccessToken();
        setToken(accessToken);

        // 获取用户信息
        try {
          const currentUser = await authApi.getCurrentUser();
          setUser(currentUser);
          applyUserMetadata(currentUser.metadata);
          // 更新动态权限
          if (currentUser.permissions) {
            setDynamicPermissions(
              currentUser.permissions.filter((p): p is Permission =>
                Object.values(Permission).includes(p as Permission),
              ),
            );
          }
        } catch {
          // 忽略用户信息获取失败
        }
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  // 登出
  const logout = useCallback(() => {
    authApi.logout();
    setToken(null);
    setUser(null);
  }, []);

  // 刷新用户信息（同时更新动态权限）
  const refreshUser = useCallback(async () => {
    if (!isAuthenticated()) return;

    const accessToken = getAccessToken();
    setToken(accessToken);

    try {
      const currentUser = await authApi.getCurrentUser();
      setUser(currentUser);
      applyUserMetadata(currentUser.metadata);
      // 更新动态权限
      if (currentUser.permissions) {
        setDynamicPermissions(
          currentUser.permissions.filter((p): p is Permission =>
            Object.values(Permission).includes(p as Permission),
          ),
        );
      }
    } catch (error) {
      console.error("Failed to refresh user info:", error);
    }
  }, []);

  // 检查是否拥有某个权限
  const hasPermission = useCallback(
    (permission: Permission): boolean => {
      return permissions.includes(permission);
    },
    [permissions],
  );

  // 检查是否拥有任意一个权限
  const hasAnyPermission = useCallback(
    (perms: Permission[]): boolean => {
      return perms.some((p) => permissions.includes(p));
    },
    [permissions],
  );

  // 检查是否拥有所有权限
  const hasAllPermissions = useCallback(
    (perms: Permission[]): boolean => {
      return perms.every((p) => permissions.includes(p));
    },
    [permissions],
  );

  // 对外暴露的上下文值。注意 isAuthenticated 要求 token 与 user 同时存在，
  // 仅有 token 但用户信息拉取失败时不算已登录，避免半登录态。
  const value: AuthContextType = {
    user,
    token,
    isAuthenticated: !!token && !!user,
    isLoading,
    permissions,
    login,
    register,
    loginWithOAuth,
    handleOAuthCallback,
    logout,
    refreshUser,
    hasPermission,
    hasAnyPermission,
    hasAllPermissions,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// useAuth Hook
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

// 默认导出
// eslint-disable-next-line react-refresh/only-export-components
export default useAuth;
