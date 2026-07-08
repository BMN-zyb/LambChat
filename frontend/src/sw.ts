/// <reference lib="webworker" />

// Service Worker（基于 Workbox）：实现 PWA 离线能力、静态资源缓存、字体缓存、
// Web Push 推送通知与点击跳转。由 Vite 的 injectManifest 注入预缓存清单
// (__WB_MANIFEST)。注意：后端接口/流式请求会被 pwaRouting 判定为 bypass，不走缓存。
import { CacheableResponsePlugin } from "workbox-cacheable-response";
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import {
  CacheFirst,
  NetworkFirst,
  StaleWhileRevalidate,
} from "workbox-strategies";
import { isPwaSkipWaitingMessage } from "./pwaGuards";
import { getPwaRequestKind } from "./pwaRouting";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<unknown>;
};

// 各类缓存桶的名称（带版本号 v2，升级版本即可让旧缓存整体失效重建）
const APP_SHELL_CACHE = "lambchat-app-shell-v2";
const STATIC_CACHE = "lambchat-static-v2";
const FONT_STYLES_CACHE = "lambchat-font-styles-v2";
const FONT_FILES_CACHE = "lambchat-font-files-v2";
const OFFLINE_URL = "/offline.html";

// 清理旧版本遗留缓存；预缓存构建产物清单；clientsClaim 让新 SW 激活后立即接管
// 现有页面（无需刷新即可控制页面）。
cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);
clientsClaim();

// 收到主线程发来的 SKIP_WAITING 消息时立即跳过等待并激活新版本，
// 配合 pwa.ts 的「有更新可用」提示实现点击即更新。
self.addEventListener("message", (event) => {
  if (!isPwaSkipWaitingMessage(event.data)) return;

  event.waitUntil(self.skipWaiting());
});

// 页面导航请求策略：网络优先(NetworkFirst)，4 秒超时后回退缓存，
// 只缓存 200 响应。保证在线时拿最新 HTML、离线/弱网时用缓存的应用外壳兜底。
const navigationStrategy = new NetworkFirst({
  cacheName: APP_SHELL_CACHE,
  networkTimeoutSeconds: 4,
  plugins: [
    new CacheableResponsePlugin({
      statuses: [200],
    }),
  ],
});

// 离线兜底页：优先返回专门的离线页，其次 index.html，都没有时返回 503 纯文本。
async function getOfflineFallback(): Promise<Response> {
  const cachedFallback =
    (await caches.match(OFFLINE_URL)) || (await caches.match("/index.html"));

  return (
    cachedFallback ||
    new Response("LambChat is offline.", {
      status: 503,
      statusText: "Service Unavailable",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    })
  );
}

// 路由一：页面导航请求 -> 走网络优先策略，失败则返回离线兜底页。
// 请求类型由 pwaRouting.getPwaRequestKind 统一判定（后端/流式请求会被判为 bypass）。
registerRoute(
  ({ request }) =>
    getPwaRequestKind({
      method: request.method,
      mode: request.mode,
      url: request.url,
      scopeOrigin: self.location.origin,
      accept: request.headers.get("accept"),
    }) === "navigation",
  async (options) => {
    try {
      return (await navigationStrategy.handle(options)) || getOfflineFallback();
    } catch {
      return getOfflineFallback();
    }
  },
);

// 路由二：静态资源(js/css/图片等) -> StaleWhileRevalidate：先返回缓存以求快，
// 同时后台拉取更新缓存；限制最多 220 条、最长缓存 30 天。
registerRoute(
  ({ request }) =>
    getPwaRequestKind({
      method: request.method,
      mode: request.mode,
      url: request.url,
      scopeOrigin: self.location.origin,
      accept: request.headers.get("accept"),
    }) === "static-asset",
  new StaleWhileRevalidate({
    cacheName: STATIC_CACHE,
    plugins: [
      new CacheableResponsePlugin({
        statuses: [0, 200],
      }),
      new ExpirationPlugin({
        maxEntries: 220,
        maxAgeSeconds: 60 * 60 * 24 * 30,
      }),
    ],
  }),
);

// 路由三：Google Fonts 样式表 -> StaleWhileRevalidate（样式表可能变动，边用边更新）
registerRoute(
  ({ url }) => url.origin === "https://fonts.googleapis.com",
  new StaleWhileRevalidate({
    cacheName: FONT_STYLES_CACHE,
    plugins: [
      new CacheableResponsePlugin({
        statuses: [0, 200],
      }),
      new ExpirationPlugin({
        maxEntries: 12,
        maxAgeSeconds: 60 * 60 * 24 * 30,
      }),
    ],
  }),
);

// 路由四：Google Fonts 字体文件 -> CacheFirst（字体文件基本不变，缓存优先，最长 1 年）
registerRoute(
  ({ url }) => url.origin === "https://fonts.gstatic.com",
  new CacheFirst({
    cacheName: FONT_FILES_CACHE,
    plugins: [
      new CacheableResponsePlugin({
        statuses: [0, 200],
      }),
      new ExpirationPlugin({
        maxEntries: 24,
        maxAgeSeconds: 60 * 60 * 24 * 365,
      }),
    ],
  }),
);

// Web Push：收到推送时解析 payload（JSON，失败则退化为纯文本），组装标题/正文/
// 图标，并把跳转 URL 放进 notification.data 供点击时使用；最后弹出系统通知。
self.addEventListener("push", (event) => {
  if (!self.registration?.showNotification) return;

  let payload: {
    title?: string;
    body?: string;
    message?: string;
    icon?: string;
    badge?: string;
    url?: string;
  } = {};

  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data?.text() };
  }

  const title = payload.title || "LambChat";
  const options: NotificationOptions = {
    body: payload.body || payload.message || "You have a new LambChat update.",
    icon: payload.icon || "/icons/icon-192.png",
    badge: payload.badge || "/icons/icon-192.png",
    data: {
      url: payload.url || "/chat",
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// 通知点击：关闭通知后，若已有同源窗口则聚焦并导航到目标 URL，避免重复开新标签；
// 没有则新开窗口。targetUrl 取自推送时写入的 data.url，缺省回到 /chat。
self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const targetUrl = new URL(
    event.notification.data?.url || "/chat",
    self.location.origin,
  );

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        const existingClient = clients.find(
          (client): client is WindowClient =>
            "focus" in client &&
            "navigate" in client &&
            new URL(client.url).origin === targetUrl.origin,
        );

        if (existingClient) {
          existingClient.focus();
          return existingClient.navigate(targetUrl.href);
        }

        return self.clients.openWindow(targetUrl.href);
      }),
  );
});
