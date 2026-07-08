/**
 * Authenticated fetch wrapper with token refresh support
 *
 * 全站发起后端请求的统一底层：自动附带 JWT、按语言设置 Accept-Language、
 * 主动/被动刷新 token、统一解析后端错误并本地化。业务的各领域客户端都建立在它之上。
 */

import i18n from "i18next";
import { getRefreshToken } from "./token";
import {
  getValidAccessToken,
  redirectToLogin,
  refreshAccessToken,
  clearAuthState,
} from "./tokenManager";
import { translateBackendError } from "../../utils/backendErrors";

// ============================================
// 带认证的 fetch 封装
// ============================================

interface FetchOptions extends RequestInit {
  // skipAuth: 跳过鉴权（不加 Authorization，也不做 401 刷新），用于登录等公开接口
  // _retry: 内部标志，标记这是 401 刷新后的重试请求，防止无限递归重试
  skipAuth?: boolean;
  _retry?: boolean;
}

/**
 * 带认证的 fetch 封装
 * 自动添加 Authorization header（使用 getValidAccessToken 主动刷新过期 token）
 * 处理 401 响应
 */
export async function authFetch<T>(
  url: string,
  options: FetchOptions = {},
): Promise<T> {
  const {
    skipAuth = false,
    headers = {},
    _retry = false,
    ...restOptions
  } = options;

  // 组装请求头：FormData 时不手动设置 Content-Type，交由浏览器自动带上正确的
  // multipart 边界(boundary)；其余请求默认 JSON。Accept-Language 跟随当前 i18n 语言。
  // 传入的 headers 放最后，允许调用方覆盖默认值。
  const finalHeaders: HeadersInit = {
    ...(restOptions.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    "Accept-Language": i18n.language || "en",
    ...headers,
  };

  // Use getValidAccessToken to proactively refresh expired tokens,
  // avoiding unnecessary 401 round-trips.
  if (!skipAuth) {
    const token = await getValidAccessToken();
    if (token) {
      (finalHeaders as Record<string, string>)["Authorization"] =
        `Bearer ${token}`;
    }
  }

  const response = await fetch(url, {
    ...restOptions,
    headers: finalHeaders,
  });

  // 检查当前用户是否被修改（需要重新登录）
  // 后端可在权限/账号变更时下发该响应头，强制前端清除登录态并重登，确保权限即时生效。
  if (!skipAuth && response.headers.get("X-Force-Relogin") === "true") {
    clearAuthState();
    throw new Error("用户权限已变更，请重新登录");
  }

  // 处理 401 未授权响应
  // 被动刷新兜底：即便上面主动刷新过，token 仍可能在服务端被判失效。
  // 此时若有 refresh token 且当前不是重试请求(_retry=false)，先刷新再带上 _retry 重发一次；
  // 刷新失败或本就是重试则跳转登录。_retry 保证最多只重试一次，避免死循环。
  if (response.status === 401 && !skipAuth) {
    const refreshToken = getRefreshToken();

    if (refreshToken && !_retry) {
      try {
        await refreshAccessToken();
      } catch (error) {
        redirectToLogin();
        throw error;
      }
      return authFetch<T>(url, { ...options, skipAuth: false, _retry: true });
    }

    redirectToLogin();
    throw new Error("Unauthorized");
  }

  // 其余非 2xx：尽力解析后端错误体（detail 可能是字符串或含 message 的对象），
  // 再经 translateBackendError 本地化后抛出，供上层展示。
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    // 处理 detail 为对象或字符串的情况
    let errorMessage: string;
    if (typeof errorData.detail === "object" && errorData.detail !== null) {
      // 如果 detail 是对象，提取 message 字段
      errorMessage =
        errorData.detail.message || JSON.stringify(errorData.detail);
    } else {
      errorMessage =
        errorData.detail || `Request failed: ${response.statusText}`;
    }
    throw new Error(translateBackendError(errorMessage, i18n.t.bind(i18n)));
  }

  // 处理空响应
  // 注意：当响应体为空时返回 null，调用者应处理 T | null 的情况
  // 对于必须返回非空值的场景，API 应确保返回空对象 {} 而不是空响应
  const text = await response.text();
  if (!text) {
    return null as T;
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    console.warn("[authFetch] Failed to parse response as JSON:", text);
    return null as T;
  }
}
