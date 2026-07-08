// Vite 构建/开发配置：React 插件、PWA(injectManifest 自定义 SW)、Node polyfill 别名、
// 生产去除 console、vendor 分包(manualChunks)、以及开发期代理到后端 8000（聊天流 24h 超时）。
import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// 可用的 Agent ID（需与后端保持一致）：下方据此为每个 /{agent_id} 生成 dev proxy 规则。
// Available agents (sync with backend)
const AGENT_IDS = ["default", "api", "data_pipeline", "simple_workflow"];
const ICONS_DIR = path.resolve(__dirname, "public/icons");

function getStaticIconContentType(filePath: string): string {
  if (filePath.endsWith(".svg")) return "image/svg+xml";
  if (filePath.endsWith(".png")) return "image/png";
  if (filePath.endsWith(".jpg") || filePath.endsWith(".jpeg")) {
    return "image/jpeg";
  }
  if (filePath.endsWith(".webp")) return "image/webp";
  if (filePath.endsWith(".ico")) return "image/x-icon";
  return "application/octet-stream";
}

// 自定义 dev 中间件插件：开发期直接以「一年不可变」强缓存响应 /icons/ 下的静态图标，
// 避免频繁重复请求。仅处理 GET/HEAD，且做了路径穿越(..)防护，只读真实存在的文件。
const cacheStableIconsPlugin = {
  name: "cache-stable-icons",
  configureServer(server: {
    middlewares: {
      use: (
        handler: (
          req: { method?: string; url?: string },
          res: {
            statusCode?: number;
            setHeader: (name: string, value: string) => void;
            end: (body: Buffer) => void;
          },
          next: () => void,
        ) => void,
      ) => void;
    };
  }) {
    server.middlewares.use((req, res, next) => {
      if (req.method !== "GET" && req.method !== "HEAD") {
        next();
        return;
      }

      const requestPath = req.url?.split("?")[0];
      if (!requestPath?.startsWith("/icons/")) {
        next();
        return;
      }

      const relativePath = requestPath.slice("/icons/".length);
      if (
        !relativePath ||
        relativePath.includes("..") ||
        relativePath.includes("\\")
      ) {
        next();
        return;
      }

      const filePath = path.join(ICONS_DIR, relativePath);
      if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
        next();
        return;
      }

      const fileBuffer = fs.readFileSync(filePath);
      res.statusCode = 200;
      res.setHeader("Content-Type", getStaticIconContentType(filePath));
      res.setHeader("Content-Length", String(fileBuffer.length));
      res.setHeader("Cache-Control", "public, max-age=31536000, immutable");
      if (req.method === "HEAD") {
        res.end(Buffer.alloc(0));
        return;
      }
      res.end(fileBuffer);
    });
  },
};

export default defineConfig({
  plugins: [
    react(),
    // PWA：采用 injectManifest 策略——用我们自己写的 src/sw.ts 作为 Service Worker 源，
    // Vite 仅把预缓存清单(__WB_MANIFEST)注入进去。manifest:false 表示不生成 webmanifest，
    // injectRegister:false 因为注册在 pwa.ts 中手动做；devOptions.enabled:false 关闭开发期 SW。
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectRegister: false,
      manifest: false,
      injectManifest: {
        globPatterns: [
          "**/*.{js,css,html,ico,png,svg,webp,avif,woff,woff2,json}",
        ],
        maximumFileSizeToCacheInBytes: 8 * 1024 * 1024,
      },
      includeManifestIcons: false,
      devOptions: {
        enabled: false,
      },
    }),
    cacheStableIconsPlugin,
  ],
  resolve: {
    // 把部分 Node 内置模块/库映射到浏览器可用实现（polyfill），供依赖它们的库在前端运行。
    alias: [
      {
        find: /^opentype\.js$/,
        replacement: path.resolve(
          __dirname,
          "node_modules/opentype.js/dist/opentype.js",
        ),
      },
      {
        find: /^stream$/,
        replacement: path.resolve(__dirname, "node_modules/stream-browserify"),
      },
      {
        find: /^events$/,
        replacement: path.resolve(__dirname, "node_modules/events"),
      },
      {
        find: /^util$/,
        replacement: path.resolve(__dirname, "node_modules/util"),
      },
      {
        find: /^process$/,
        replacement: path.resolve(__dirname, "node_modules/process/browser"),
      },
    ],
  },
  esbuild: {
    // 生产构建时移除所有 console.* 与 debugger，减小体积并避免泄露调试信息。
    drop: process.env.NODE_ENV === "production" ? ["console", "debugger"] : [],
  },
  build: {
    rollupOptions: {
      output: {
        // 手动分包：把体积大、变动少的第三方库按功能拆成独立 vendor chunk，
        // 提升浏览器缓存命中率（业务代码更新时这些 chunk 无需重新下载）。
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-codemirror": [
            "@uiw/react-codemirror",
            "@codemirror/lang-css",
            "@codemirror/lang-html",
            "@codemirror/lang-javascript",
            "@codemirror/lang-json",
            "@codemirror/lang-markdown",
            "@codemirror/lang-python",
            "@codemirror/lang-sql",
            "@codemirror/lang-yaml",
          ],
          "vendor-markdown": [
            "react-markdown",
            "remark-gfm",
            "remark-breaks",
            "remark-math",
            "rehype-katex",
            "rehype-highlight",
          ],
          "vendor-sandpack": ["@codesandbox/sandpack-react"],
          "vendor-mermaid": ["mermaid"],
          "vendor-katex": ["katex"],
          "vendor-i18n": ["i18next", "react-i18next"],
        },
      },
    },
  },
  server: {
    host: true, // 监听所有地址 (0.0.0.0)，允许 127.0.0.1 和 localhost 访问
    port: 3001,
    // 开发代理：把后端相关路径转发到本地后端 http://127.0.0.1:8000，规避跨域。
    // 关键点：聊天流式接口(SSE/WebSocket)需开启 ws 并把超时拉到 24 小时，
    // 否则长连接会被默认超时中断；普通 API 用 5 分钟超时即可。
    proxy: {
      // Long-running chat event stream
      // 会话流式端点：正则精确匹配 /api/chat/sessions/{id}/stream，24h 超时承接长连接。
      "^/api/chat/sessions/[^/]+/stream$": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
        ws: true,
        timeout: 86400000, // 24 hours timeout for long-running chat streams
        proxyTimeout: 86400000, // 24 hours proxy timeout
      },
      // API routes (including /api/chat for SSE)
      // 通用 /api 代理：configure 里把原始 Host 透传为 X-Forwarded-Host，
      // 供后端拼接正确的 OAuth redirect_uri（否则回调地址会指向后端而非前端）。
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
        ws: true, // Enable WebSocket/SSE support for streaming
        timeout: 300000, // 5 minutes timeout for regular API requests
        proxyTimeout: 300000, // 5 minutes proxy timeout
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq, req) => {
            // 保留原始 host 到 X-Forwarded-Host 头，用于 OAuth redirect_uri
            const host = req.headers.host;
            if (host) {
              proxyReq.setHeader("X-Forwarded-Host", host);
            }
          });
        },
      },
      // Agent routes (/{agent_id}/chat, /{agent_id}/stream, /{agent_id}/skills)
      // 为每个 Agent ID 动态生成一条代理规则（同样 24h 超时以支持流式）。
      ...Object.fromEntries(
        AGENT_IDS.map((id) => [
          `/${id}`,
          {
            target: "http://127.0.0.1:8000",
            changeOrigin: true,
            secure: false,
            ws: true, // Enable WebSocket/SSE support for streaming
            timeout: 86400000, // 24 hours timeout for long-running chat streams
            proxyTimeout: 86400000, // 24 hours proxy timeout
          },
        ]),
      ),
      "/tools": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
      },
      "/human": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
      },
      "/health": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
        ws: true,
      },
      "/services": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
