// Token 刷新编排层：提供「主动获取有效 token」「并发去重的刷新」「登出/跳登录」等能力。
// 与 token.ts 分工：token.ts 只做存储与 JWT 解析，本文件负责刷新流程与副作用（事件、跳转）。
import { API_BASE } from "./config";
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  isSafeRedirectPath,
  isTokenExpired,
  setTokens,
} from "./token";
import i18n from "../../i18n";

// 单例的「进行中刷新 Promise」：用于并发去重，保证同一时刻只发一个刷新请求。
let refreshPromise: Promise<string> | null = null;

export interface RefreshedTokens {
  access_token: string;
  refresh_token?: string;
}

// 派发全局登出事件，通知 useAuth 等清空 React 层登录态（模块间解耦）。
function notifyLogout(): void {
  window.dispatchEvent(new CustomEvent("auth:logout"));
}

// 清除本地 token 并广播登出。
export function clearAuthState(): void {
  clearTokens();
  notifyLogout();
}

// 跳转登录：先把当前「安全的」路径存入 sessionStorage，登录成功后可回跳；随后清除登录态。
export function redirectToLogin(): void {
  const currentPath = window.location.pathname + window.location.search;
  if (isSafeRedirectPath(currentPath)) {
    sessionStorage.setItem("redirect_after_login", currentPath);
  }
  clearAuthState();
}

/**
 * Get a valid (non-expired) access token.
 *
 * Returns `null` when no token exists — the caller decides what to do.
 * When the access token is expired, attempts a silent refresh.
 * Does NOT call redirectToLogin — callers handle redirect themselves.
 *
 * 主动刷新：authFetch 发请求前调用它拿有效 token，尽量避免打到 401 再刷新的往返。
 * access 未过期直接用；过期且 refresh 仍有效则静默刷新；否则返回 null 交由调用方处理。
 */
export async function getValidAccessToken(): Promise<string | null> {
  const accessToken = getAccessToken();
  if (!accessToken) {
    return null;
  }

  if (!isTokenExpired(accessToken)) {
    return accessToken;
  }

  // Access token expired — try refresh
  const refreshToken = getRefreshToken();
  if (!refreshToken || isTokenExpired(refreshToken)) {
    return null;
  }

  try {
    return await refreshAccessToken();
  } catch {
    return null;
  }
}

/**
 * Refresh tokens with deduplication to avoid concurrent refresh requests.
 *
 * Uses a ref-counted approach: the promise is cleared only after all
 * concurrent callers have awaited it, preventing race conditions where
 * a third caller starts a duplicate refresh.
 *
 * 并发去重的刷新：多处同时发现 token 过期时，只有第一个真正发刷新请求，
 * 其余复用同一个 refreshPromise，避免用同一个（可能一次性）refresh token 重复刷新。
 */
export async function refreshTokens(): Promise<RefreshedTokens> {
  if (refreshPromise) {
    // Wait for the in-flight refresh — do NOT return early with just access_token.
    // The caller may need the refresh_token too.
    // 复用进行中的刷新：等它完成后再补齐 refresh_token 一并返回（调用方可能需要）。
    const access_token = await refreshPromise;
    return {
      access_token,
      refresh_token: getRefreshToken() ?? undefined,
    };
  }

  refreshPromise = (async () => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) {
      throw new Error("No refresh token available");
    }

    // 直接用原生 fetch（不走 authFetch），避免刷新请求本身再触发 401->刷新 的递归。
    const response = await fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept-Language": i18n.language || "en",
      },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!response.ok) {
      throw new Error("Token refresh failed");
    }

    const tokenResponse = (await response.json()) as RefreshedTokens;
    setTokens(tokenResponse.access_token, tokenResponse.refresh_token);
    return tokenResponse.access_token;
  })();

  try {
    const access_token = await refreshPromise;
    return {
      access_token,
      refresh_token: getRefreshToken() ?? undefined,
    };
  } finally {
    // Use microtask delay so that callers still awaiting the same promise
    // in the `if (refreshPromise)` branch finish before we clear it.
    // 用微任务延迟置空 refreshPromise：确保正在 if(refreshPromise) 分支里 await 的
    // 其它并发调用先拿到结果，再清空，从而杜绝「刚清空又有人发起重复刷新」的竞态。
    Promise.resolve().then(() => {
      refreshPromise = null;
    });
  }
}

// 便捷封装：只关心 access_token 的调用方用它（内部仍走去重的 refreshTokens）。
export async function refreshAccessToken(): Promise<string> {
  const { access_token } = await refreshTokens();
  return access_token;
}
