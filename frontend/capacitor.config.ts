import type { CapacitorConfig } from "@capacitor/cli";

// Capacitor 配置：将前端打包为原生 iOS/Android App 的壳。
// appId 为原生包名/Bundle ID；webDir 指向 Vite 构建产物 dist（原生端加载的静态资源目录）。
const config: CapacitorConfig = {
  appId: "com.lambchat.app",
  appName: "LambChat",
  webDir: "dist",
  bundledWebRuntime: false,
  android: {
    // 禁止混合内容：HTTPS 页面内不允许加载 HTTP 资源，提升安全性。
    allowMixedContent: false,
  },
  ios: {
    // 自动处理安全区/滚动内边距（状态栏、刘海等），避免内容被系统 UI 遮挡。
    contentInset: "automatic",
  },
};

export default config;
