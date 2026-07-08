// Service Worker 的「请求分类」纯函数集合（与 sw.ts 解耦以便单测）：
// 判定某个请求应被 SW 如何处理——直接放行(bypass)、按页面导航缓存(navigation)、
// 还是按静态资源缓存(static-asset)。核心是把后端 API 与流式响应排除在缓存之外。

// 后端接口路径前缀白名单：命中这些前缀的请求一律 bypass（不缓存），
// 交给网络直连，避免把动态数据/鉴权响应缓存下来。
export const PWA_BACKEND_PREFIXES = [
  "/api",
  "/ws",
  "/health",
  "/tools",
  "/human",
  "/services",
  "/default",
  "/data_pipeline",
  "/simple_workflow",
] as const;

// 静态资源扩展名匹配：命中则按静态资源缓存策略处理。
const STATIC_ASSET_PATTERN =
  /\.(?:css|js|mjs|png|jpg|jpeg|svg|webp|avif|ico|woff|woff2|ttf|otf|json|wasm)$/i;

export type PwaRequestKind = "bypass" | "navigation" | "static-asset";

interface PwaRequestInput {
  method: string;
  mode?: RequestMode | string | null;
  url: string;
  scopeOrigin: string;
  accept?: string | null;
}

// 是否为后端路径：精确等于某前缀或以「前缀/」开头。
export function isBackendPath(pathname: string): boolean {
  return PWA_BACKEND_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

// 是否为 SSE/流式请求：Accept 含 text/event-stream 或路径含 "stream"。
// 这类长连接必须绕过缓存，否则会破坏聊天流式输出。
export function isEventStreamRequest({
  accept,
  pathname,
}: {
  accept?: string | null;
  pathname: string;
}): boolean {
  return (
    Boolean(accept?.includes("text/event-stream")) ||
    pathname.includes("stream")
  );
}

// 是否为页面导航请求：request.mode 为 "navigate" 或 Accept 含 text/html。
export function isNavigationRequest({
  accept,
  mode,
}: {
  accept?: string | null;
  mode?: RequestMode | string | null;
}): boolean {
  return mode === "navigate" || Boolean(accept?.includes("text/html"));
}

// 是否为静态资源路径（按扩展名匹配）。
export function isStaticAssetPath(pathname: string): boolean {
  return STATIC_ASSET_PATTERN.test(pathname);
}

// 请求分类主入口：给定请求信息返回处理类型。判定顺序（任一命中即 bypass）：
//   URL 解析失败 / 非 GET / 跨域(非本 scope) / 后端路径 / 流式请求 -> bypass；
//   否则若是导航请求 -> navigation；是静态资源 -> static-asset；其余 -> bypass。
// sw.ts 依据该结果选择 NetworkFirst / StaleWhileRevalidate 或不缓存。
export function getPwaRequestKind({
  method,
  mode,
  url,
  scopeOrigin,
  accept,
}: PwaRequestInput): PwaRequestKind {
  let parsedUrl: URL;
  try {
    parsedUrl = new URL(url);
  } catch {
    return "bypass";
  }

  if (
    method.toUpperCase() !== "GET" ||
    parsedUrl.origin !== scopeOrigin ||
    isBackendPath(parsedUrl.pathname) ||
    isEventStreamRequest({ accept, pathname: parsedUrl.pathname })
  ) {
    return "bypass";
  }

  if (isNavigationRequest({ accept, mode })) {
    return "navigation";
  }

  if (isStaticAssetPath(parsedUrl.pathname)) {
    return "static-asset";
  }

  return "bypass";
}
