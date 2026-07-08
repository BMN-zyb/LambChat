/**
 * API configuration and URL utilities
 *
 * 集中处理「后端基址」与各类 URL 构造。核心难点：区分 Web / 原生壳(Capacitor/Tauri)
 * 运行环境（isNativeAppRuntime），因为原生端没有同源可依赖，需要显式拼接绝对地址。
 */

// 后端基址：从构建期环境变量 VITE_API_BASE 读取。为空时表示与前端同源（走相对路径/dev proxy）。
const configuredApiBase =
  (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env
    ?.VITE_API_BASE || "";

// 去掉基址结尾多余的斜杠，避免拼接出 //。
function normalizeApiBase(apiBase: string): string {
  return apiBase.replace(/\/+$/, "");
}

const API_BASE = normalizeApiBase(configuredApiBase);
export { API_BASE };

export interface BrowserLocationLike {
  protocol: string;
  host: string;
  hostname?: string;
}

interface NativeRuntimeGlobalLike {
  Capacitor?: { isNativePlatform?: () => boolean };
  __TAURI__?: unknown;
  __TAURI_INTERNALS__?: unknown;
}

// 构造 REST 接口 URL：已是绝对地址则原样返回；否则规范化 path 并拼到 base 上。
// base 为空时返回相对路径（同源部署或 dev proxy 场景）。
export function buildApiUrl(path: string, apiBase: string = API_BASE): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }

  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedBase = normalizeApiBase(apiBase);
  return normalizedBase ? `${normalizedBase}${normalizedPath}` : normalizedPath;
}

// 构造 WebSocket URL：把 http(s) 协议映射为 ws(s)。有 base 用 base；否则回退到
// 当前页面 location（Web 场景），据其协议决定 ws/wss。用于聊天等实时连接。
export function buildWebSocketUrl(
  path: string = "/ws",
  apiBase: string = API_BASE,
  locationLike?: BrowserLocationLike,
): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedBase = normalizeApiBase(apiBase);

  if (normalizedBase) {
    const url = new URL(normalizedPath, normalizedBase);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  const location =
    locationLike || (typeof window !== "undefined" ? window.location : null);
  if (!location) {
    return normalizedPath;
  }

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}${normalizedPath}`;
}

/**
 * 获取完整 URL（用于处理后端返回的相对路径）
 * @param url - 可能是相对路径或完整 URL
 * @returns 完整 URL
 */
export function getFullUrl(
  url: string | undefined | null,
  apiBase: string = API_BASE,
): string | undefined {
  if (!url) return undefined;
  // 如果已经是完整 URL（http:// 或 https://），直接返回
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  if (apiBase) {
    return buildApiUrl(url, apiBase);
  }
  // 如果是相对路径，拼接 base URL（优先使用当前 origin，否则使用 API_BASE）
  const baseUrl = typeof window !== "undefined" ? window.location.origin : "";
  return baseUrl + url;
}

// 判断是否运行在原生 App 壳内（Capacitor iOS/Android 或 Tauri 桌面）。
// 判据（任一命中即为原生端）：
//   1) Capacitor.isNativePlatform() 为真；
//   2) 存在 __TAURI__ / __TAURI_INTERNALS__ 全局；
//   3) 页面协议为 capacitor:/ionic:/tauri:，或 hostname 为 tauri.localhost。
// 参数可注入 location/global 以便测试。原生端下需据此改走绝对地址、上传代理等逻辑。
export function isNativeAppRuntime(
  locationLike?: Partial<BrowserLocationLike> | null,
  globalLike?: NativeRuntimeGlobalLike | null,
): boolean {
  const location =
    locationLike || (typeof window !== "undefined" ? window.location : null);
  const globalObject =
    globalLike ||
    (typeof globalThis !== "undefined"
      ? (globalThis as NativeRuntimeGlobalLike)
      : null);

  if (globalObject?.Capacitor?.isNativePlatform?.()) {
    return true;
  }
  if (globalObject?.__TAURI__ || globalObject?.__TAURI_INTERNALS__) {
    return true;
  }

  const protocol = location?.protocol?.toLowerCase() || "";
  const hostname = location?.hostname?.toLowerCase() || "";
  return (
    protocol === "capacitor:" ||
    protocol === "ionic:" ||
    protocol === "tauri:" ||
    hostname === "tauri.localhost"
  );
}

// 对对象存储的 key 逐段做 URL 编码，但保留路径分隔符 "/" 不被编码。
function encodeUploadObjectKey(key: string): string {
  return key.split("/").map(encodeURIComponent).join("/");
}

// 上传文件 URL 转为「带 proxy=true 的代理地址」：原生端(或 force)时，直连对象存储
// 可能受 CORS/鉴权限制，故对 /api/upload/file/ 路径追加 proxy 参数，改由后端代理回源。
// 非原生且未 force 时保持原 URL 不变。
export function buildUploadProxyUrl(
  url: string | undefined | null,
  apiBase: string = API_BASE,
  options: {
    force?: boolean;
    locationLike?: Partial<BrowserLocationLike> | null;
    globalLike?: NativeRuntimeGlobalLike | null;
  } = {},
): string | undefined {
  const fullUrl = getFullUrl(url, apiBase) || url || undefined;
  if (!fullUrl) return undefined;
  if (
    !options.force &&
    !isNativeAppRuntime(options.locationLike, options.globalLike)
  ) {
    return fullUrl;
  }

  const fallbackBase =
    typeof window !== "undefined" ? window.location.origin : "http://localhost";

  try {
    const parsed = new URL(fullUrl, fallbackBase);
    if (!parsed.pathname.startsWith("/api/upload/file/")) {
      return fullUrl;
    }
    parsed.searchParams.set("proxy", "true");
    return parsed.toString();
  } catch {
    return fullUrl;
  }
}

// 由对象存储 key 直接构造上传文件访问 URL（先编码 key 拼出 /api/upload/file/ 路径）；
// force 时再套一层代理地址转换。
export function buildUploadProxyUrlFromKey(
  key: string | undefined | null,
  apiBase: string = API_BASE,
  options: Parameters<typeof buildUploadProxyUrl>[2] = {},
): string | undefined {
  if (!key) return undefined;
  const url = buildApiUrl(
    `/api/upload/file/${encodeUploadObjectKey(key)}`,
    apiBase,
  );
  return options.force ? buildUploadProxyUrl(url, apiBase, options) : url;
}
