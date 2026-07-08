/**
 * Token management utilities
 *
 * 纯存储与解析工具（无网络请求）：token 存 localStorage（跨标签页/重启保留），
 * 登录后回跳路径存 sessionStorage（仅当前会话）。刷新流程见 tokenManager.ts。
 */

const TOKEN_KEY = "access_token";
const REFRESH_TOKEN_KEY = "refresh_token";
const REDIRECT_PATH_KEY = "redirect_after_login";

// 判断路径是否适合作为「登录后回跳目标」：排除首页 "/" 与鉴权相关页 "/auth/*"，
// 避免登录成功又跳回登录/首页造成循环或无意义跳转。
export function isSafeRedirectPath(path: string): boolean {
  return path !== "/" && !path.startsWith("/auth/");
}

/**
 * 获取存储的 access token
 */
export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * 获取存储的 refresh token
 */
export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

/**
 * 保存 tokens
 */
export function setTokens(access_token: string, refresh_token?: string): void {
  localStorage.setItem(TOKEN_KEY, access_token);
  if (refresh_token) {
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh_token);
  }
}

/**
 * 清除 tokens
 */
export function clearTokens(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

/**
 * 检查是否已登录
 */
export function isAuthenticated(): boolean {
  return !!getAccessToken();
}

/**
 * 解码 JWT token（不验证签名，仅用于读取内容）
 * 仅解析 payload 段：base64url -> base64 -> UTF-8 -> JSON。
 * 安全提示：不校验签名，只可用于读取 exp 等非敏感信息做前端判断，不能作为鉴权依据。
 */
export function decodeToken(token: string): Record<string, unknown> | null {
  try {
    const base64Url = token.split(".")[1];
    const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
    const jsonPayload = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join(""),
    );
    return JSON.parse(jsonPayload);
  } catch {
    return null;
  }
}

/**
 * 检查 token 是否过期
 * 解不出 payload 或无 exp 一律视为已过期（保守，宁可触发刷新）；
 * exp 是秒级时间戳，需 *1000 与毫秒的 Date.now() 比较。
 */
export function isTokenExpired(token: string): boolean {
  const payload = decodeToken(token);
  if (!payload || !payload.exp) return true;
  return (payload.exp as number) * 1000 < Date.now();
}

/**
 * 获取登录后重定向路径
 * 读取时再次用 isSafeRedirectPath 校验；不安全则顺手清除并返回 null，做双重保险。
 */
export function getRedirectPath(): string | null {
  const redirectPath = sessionStorage.getItem(REDIRECT_PATH_KEY);
  if (!redirectPath) return null;

  if (!isSafeRedirectPath(redirectPath)) {
    sessionStorage.removeItem(REDIRECT_PATH_KEY);
    return null;
  }

  return redirectPath;
}

/**
 * 清除重定向路径
 */
export function clearRedirectPath(): void {
  sessionStorage.removeItem(REDIRECT_PATH_KEY);
}
