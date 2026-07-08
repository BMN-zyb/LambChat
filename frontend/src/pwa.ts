// 主线程侧的 PWA 生命周期管理：注册 Service Worker、监听「有新版本等待」并
// 通过自定义事件通知 UI、以及在用户确认后激活新 SW 并刷新页面。
// 与 sw.ts 配合：这里负责「发现更新 + 触发跳过等待」，sw.ts 负责「收到消息后 skipWaiting」。
import {
  PWA_SKIP_WAITING_MESSAGE,
  PWA_UPDATE_AVAILABLE_EVENT,
  isPwaUpdateReady,
  shouldRegisterPwa,
} from "./pwaGuards";

export interface LambChatPwaUpdateEventDetail {
  registration: ServiceWorkerRegistration;
}

// 标志位：仅当用户主动确认更新（调用了 activate...）后，controllerchange 才触发刷新，
// 避免首次安装 SW 接管时就误刷新页面。
let reloadWhenControllerChanges = false;

// 向全局派发「PWA 有可用更新」自定义事件，UI 层（如更新提示 toast）据此弹出提示。
function notifyPwaUpdateAvailable(registration: ServiceWorkerRegistration) {
  window.dispatchEvent(
    new CustomEvent<LambChatPwaUpdateEventDetail>(PWA_UPDATE_AVAILABLE_EVENT, {
      detail: { registration },
    }),
  );
}

// 监听更新：若已有 waiting 的 SW 且当前页面已被某个 SW 控制，说明有更新在候场，
// 立即通知；否则监听 updatefound，等新 worker 装完(installed 且已有 controller)再通知。
function watchForPwaUpdates(registration: ServiceWorkerRegistration) {
  if (registration.waiting && navigator.serviceWorker.controller) {
    notifyPwaUpdateAvailable(registration);
  }

  registration.addEventListener("updatefound", () => {
    const worker = registration.installing;
    if (!worker) return;

    worker.addEventListener("statechange", () => {
      if (
        isPwaUpdateReady({
          hasController: Boolean(navigator.serviceWorker.controller),
          workerState: worker.state,
        })
      ) {
        notifyPwaUpdateAvailable(registration);
      }
    });
  });
}

// 激活等待中的更新：由 UI 在用户点击「立即更新」时调用。置位刷新标志，
// 向 waiting 的 SW 发送 SKIP_WAITING 消息触发其激活；随后 sw 激活会引发
// controllerchange，进而触发页面刷新加载新版本。无等待中的 SW 时返回 false。
export function activateWaitingLambChatPwaUpdate(
  registration: ServiceWorkerRegistration,
): boolean {
  if (!registration.waiting) return false;

  reloadWhenControllerChanges = true;
  registration.waiting.postMessage({ type: PWA_SKIP_WAITING_MESSAGE });
  return true;
}

// 注册入口（在 main.tsx 启动时调用）：先用 shouldRegisterPwa 判断是否应注册
// （通常仅生产环境且浏览器支持 SW），满足后在 window load 时注册 /sw.js。
export function registerLambChatPwa(): void {
  const hasServiceWorker =
    typeof navigator !== "undefined" && "serviceWorker" in navigator;

  if (
    !shouldRegisterPwa({
      isProduction: import.meta.env.PROD,
      hasServiceWorker,
    })
  ) {
    return;
  }

  window.addEventListener("load", () => {
    // controllerchange：新 SW 接管页面时触发。仅在用户确认过更新后才刷新，
    // 且用标志位去重，避免重复刷新。
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!reloadWhenControllerChanges) return;
      reloadWhenControllerChanges = false;
      window.location.reload();
    });

    // 注册 SW：scope 根路径、updateViaCache:"none" 确保每次都向服务器校验 sw.js
    // 是否更新；注册成功后开始监听更新，并主动触发一次 update() 检查。
    navigator.serviceWorker
      .register("/sw.js", { scope: "/", updateViaCache: "none" })
      .then((registration) => {
        watchForPwaUpdates(registration);
        return registration.update().catch(() => undefined);
      })
      .catch((error) => {
        console.warn("[PWA] Service worker registration failed:", error);
      });
  });
}
