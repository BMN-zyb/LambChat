<div align="center">

# 🐑 LambChat

**An open-source AI Agent platform for building, running, and sharing agents that actually do the work**

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)]()
[![React](https://img.shields.io/badge/React-19-green.svg)]()
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-orange.svg)]()
[![deepagents](https://img.shields.io/badge/deepagents-Latest-purple.svg)]()
[![MongoDB](https://img.shields.io/badge/MongoDB-Latest-green.svg)]()
[![Redis](https://img.shields.io/badge/Redis-Latest-red.svg)]()
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) · [简体中文](README_CN.md) · [Documentation](https://yanyutin753.github.io/LambChat/en/) · [Contributing](CONTRIBUTING.md)

<br>

<img src="docs/images/readme/hero.webp" alt="LambChat AI Agent Platform" width="920">

</div>

---

## 🌟 Why LambChat

LambChat is built for teams who want more than a chatbot UI. It gives you a complete AI Agent system with model management, MCP connectivity, skills, storage, sharing, approvals, and deployment-ready backend/frontend infrastructure in one project.

| Agent Runtime | Tools and MCP | Skills and Memory | Production Infra |
|---|---|---|---|
| Deep agent graphs, streaming output, sub-agents, thinking mode, scheduled runs, and human approval. | System and user MCP, encrypted secrets, tool cache, upload/reveal tools, and sandbox execution. | Skill marketplace, GitHub sync, persona presets, model routing, and MongoDB-backed memory. | FastAPI, React 19, auth/RBAC, tracing, health checks, arq tasks, realtime sync, and deployment assets. |

## 📸 Product Preview

| Chat and execution | Skills marketplace | Operations console |
|:---:|:---:|:---:|
| <img src="docs/images/best-practice/chat-response.webp" width="300" alt="Streaming agent response"><br>**Streaming agent work** | <img src="docs/images/best-practice/marketplace-page.webp" width="300" alt="Skill marketplace"><br>**Reusable skills** | <img src="docs/images/best-practice/mcp-page.webp" width="300" alt="MCP configuration"><br>**MCP and tools** |
| <img src="docs/images/best-practice/files-page.webp" width="300" alt="File library"><br>**Rich file library** | <img src="docs/images/best-practice/models-page.webp" width="300" alt="Model configuration"><br>**Model routing** | <img src="docs/images/best-practice/mobile-view.webp" width="190" alt="Mobile responsive view"><br>**Responsive UI** |

<details>
<summary><b>View the full screenshot gallery</b></summary>

| | | |
|:---:|:---:|:---:|
| <img src="docs/images/best-practice/login-page.webp" width="280" alt="Login"><br>**Login** | <img src="docs/images/best-practice/register-page.webp" width="280" alt="Register"><br>**Register** | <img src="docs/images/best-practice/reset-request-page.webp" width="280" alt="Password Reset"><br>**Password Reset** |
| <img src="docs/images/best-practice/verify-email-page.webp" width="280" alt="Email Verification"><br>**Email Verification** | <img src="docs/images/best-practice/registration-pending-page.webp" width="280" alt="Registration Pending"><br>**Registration Pending** | <img src="docs/images/best-practice/chat-home.webp" width="280" alt="Chat"><br>**Chat** |
| <img src="docs/images/best-practice/chat-response.webp" width="280" alt="Streaming"><br>**Streaming** | <img src="docs/images/best-practice/share-dialog.webp" width="280" alt="Share"><br>**Share** | <img src="docs/images/best-practice/skills-page.webp" width="280" alt="Skills"><br>**Skills** |
| <img src="docs/images/best-practice/marketplace-page.webp" width="280" alt="Marketplace"><br>**Marketplace** | <img src="docs/images/best-practice/mcp-page.webp" width="280" alt="MCP"><br>**MCP Config** | <img src="docs/images/best-practice/agents-page.webp" width="280" alt="Agents"><br>**Agents** |
| <img src="docs/images/best-practice/models-page.webp" width="280" alt="Models"><br>**Models** | <img src="docs/images/best-practice/channels-page.webp" width="280" alt="Channels"><br>**Channels** | <img src="docs/images/best-practice/files-page.webp" width="280" alt="Files"><br>**Files** |
| <img src="docs/images/best-practice/persona-page.webp" width="280" alt="Persona"><br>**Persona** | <img src="docs/images/best-practice/memory-page.webp" width="280" alt="Memory"><br>**Memory** | <img src="docs/images/best-practice/notifications-page.webp" width="280" alt="Notifications"><br>**Notifications** |
| <img src="docs/images/best-practice/settings-page.webp" width="280" alt="Settings"><br>**Settings** | <img src="docs/images/best-practice/feedback-page.webp" width="280" alt="Feedback"><br>**Feedback** | <img src="docs/images/best-practice/shared-page.webp" width="280" alt="Shared"><br>**Shared Session** |
| <img src="docs/images/best-practice/roles-page.webp" width="280" alt="Roles"><br>**Roles** | <img src="docs/images/best-practice/users-page.webp" width="280" alt="Users"><br>**Users** | <img src="docs/images/best-practice/tablet-view.webp" width="280" alt="Tablet"><br>**Tablet** |

</details>

## 🎬 Use Cases

| # | Case | Description | Demo |
|---|------|-------------|------|
| 1 | Supply Chain PDF Report | Generates a polished PDF efficiency report with charts, benchmark comparisons, and delivery, inventory, fulfillment, and logistics analysis from a single prompt. | [View Session](https://lambchat.com/shared/w0WA7GtMCyca) |
| 2 | Godfather Fan Website | Builds a responsive English promo site for *The Godfather* trilogy with a cinematic visual direction, marquee hero section, generated images, and multi-device polish. | [View Session](https://lambchat.com/shared/9XlmaDANCjO9) |
| 3 | Story Breakdown from Image | Understands visual input, identifies the stories shown in an image, and produces detailed plot-by-plot explanations with multimodal reasoning. | [View Session](https://lambchat.com/shared/MZX-eNnOoilN) |
| 4 | EV Market Trend Analysis | Turns recent 2025-2026 electric vehicle data into a structured market analysis covering growth, regional performance, and key industry takeaways. | [View Session](https://lambchat.com/shared/5XUeuDEyd2CY) |
| 5 | 🎮 Batch Game UI Icon Generation | Feed it one reference image and it analyzes the art style, generates **48 game UI icons** across 9 categories (candies, buttons, labels, currencies, characters, skills, navigation, frames, specials), auto-organizes them into folders, and **saves the workflow as a reusable skill** for next time. | [View Session](https://lambchat.com/shared/BFkDxT2J4pR0) |
| 6 | 🥧 E-Commerce Product Image Suite | Enter a product keyword (e.g., "durian") and a target platform (Taobao / JD / Pinduoduo), and the Agent runs the full pipeline: audience analysis → visual strategy → auto-generate main image, lifestyle scene, detail shots, and combo images — a complete set of product-ready visuals in one shot. | [View Session](https://lambchat.com/shared/Hx8mPq3R5nW1) |

## 🏗️ Architecture

<p align="center"><img src="docs/images/best-practice/architecture.webp" width="600" alt="Architecture"></p>

## ✨ Features

<details>
<summary><b>🤖 Agent Runtime</b></summary>

- **deepagents Architecture** — Compiled graph runtime with fine-grained state management
- **Multi-Agent Types** — Core, fast, and search agents
- **Plugin System** — `@register_agent("id")` decorator for custom agents
- **Streaming Output** — Native SSE support
- **Sub-agents** — Multi-level delegation
- **Thinking Mode** — Extended thinking for Anthropic models
- **Scheduled Tasks** — Cron, interval, date, and manual task triggers with persisted scheduler state
- **Human-in-the-Loop** — Approval system with countdown timer, auto-extension, and urgent-state styling
- **Persona Presets** — Reusable persona configuration with permissions and runtime binding

</details>

<details>
<summary><b>🧠 Model, Memory, and Skills</b></summary>

- **Multi-Provider Models** — OpenAI, Anthropic, Google Gemini, and Kimi
- **Full CRUD** — Create, edit, delete, reorder, and batch import models via UI
- **Channel Routing** — Route the same model through different channels with `model_id`
- **Role-based Access** — `MODEL_ADMIN` permission and per-role model visibility
- **Cross-session Memory** — Native MongoDB-backed memory system
- **Dual Skills Storage** — File system plus MongoDB backup
- **GitHub Sync** — Import custom skills from GitHub
- **Skill Marketplace** — Browse, install, publish, and manage skills in bulk

</details>

<details>
<summary><b>🔌 Tools, MCP, and Execution</b></summary>

- **System + User MCP** — Global and per-user MCP configuration
- **Encrypted Storage** — API keys encrypted at rest
- **Dynamic Tool Caching** — Cache MCP tools with manual refresh
- **Multiple Transports** — SSE and HTTP
- **Permission Control** — Transport-level access policies
- **Sandbox Integration** — Daytona and E2B execution support
- **Built-in Tools** — File reveal, project reveal, upload URL, env vars, audio transcription, persona preset tools, and more

</details>

<details>
<summary><b>📁 Product Features</b></summary>

- **File Library** — Browse revealed files with code preview, favorites, and project-based filtering
- **Rich Previews** — PDF, Word, Excel, PPT, Markdown, Mermaid, Excalidraw, images, and video playback
- **Project Folders** — Organize sessions into projects with drag-and-drop
- **Session Sharing** — Generate public share links for conversations
- **Feedback** — Thumbs rating, text comments, session linking, and run-level stats
- **Notifications** — In-app notification storage and delivery hooks

</details>

<details>
<summary><b>🔐 Infra, Realtime, and Frontend</b></summary>

- **Realtime** — Redis + MongoDB dual-write, WebSocket, auto-reconnect, and shared-session updates
- **Task Runtime** — Local or Redis-backed arq execution with embedded worker lifecycle
- **Security** — JWT, RBAC, bcrypt, OAuth (Google/GitHub/Apple), email verification, CAPTCHA, and sandbox controls
- **Observability** — LangSmith tracing, structured logging, health checks, and distributed memory diagnostics
- **Channels** — Native Feishu integration plus an extensible multi-channel architecture
- **Frontend Stack** — React 19, Vite 6, TailwindCSS 3.4, PWA workers, Capacitor mobile builds, Tauri desktop shell, rich rendering, and responsive layouts
- **i18n** — English, Chinese, Japanese, Korean, and Russian

</details>

## ⚙️ Configuration

Multiple setting categories can be configured through the UI or environment variables:

| Category | Description |
|----------|-------------|
| Frontend | Default agent, welcome suggestions, UI preferences |
| Agent | Debug mode, logging level |
| Model | Multi-provider model management, per-model config, channel routing |
| Session | Session management, message history, SSE cache |
| Database | MongoDB connection, optional PostgreSQL |
| Storage | Persistent storage, S3/OSS/MinIO/COS |
| Security | Encryption and security policies |
| Sandbox | Code sandbox settings (Daytona / E2B) |
| Skills | Skill system config |
| Tools | Tool system settings |
| Tracing | LangSmith and tracing |
| User | User management, registration, default role |
| Memory | Memory system (native) |
| Scheduler | Dynamic scheduled tasks and runtime registration |
| Task Runtime | Local execution or arq queue settings |

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- uv
- Node.js 18+
- pnpm 10+
- MongoDB
- Redis

### Docker

```bash
git clone https://github.com/Yanyutin753/LambChat.git
cd LambChat

cd deploy
cp .env.example .env   # Edit with your config
docker compose up -d
```

Open **http://localhost:8000**.

### Local Development

Install dependencies:

```bash
cp .env.example .env   # Edit with your config
make install-all
```

Run the backend and frontend together:

```bash
make dev-all
```

Backend: **http://127.0.0.1:8000**
Frontend dev server: **http://127.0.0.1:3001**

You can also run them separately:

```bash
make dev            # FastAPI backend: uv run python main.py
make frontend-dev   # Vite frontend
```

<details>
<summary><b>📝 Required Configuration</b></summary>

Edit the `.env` file with the following recommended settings:

```bash
# Recommended: Set a stable JWT secret (auto-generated on each restart if unset, invalidating existing sessions)
JWT_SECRET_KEY=your-stable-secret-key

# Recommended: Set MCP encryption salt (auto-generated on each restart if unset, invalidating saved MCP configs)
MCP_ENCRYPTION_SALT=your-stable-encryption-salt

# Optional: Configure MongoDB connection
MONGODB_URL=mongodb://localhost:27017
MONGODB_DB=agent_state
MONGODB_USERNAME=admin
MONGODB_PASSWORD=your-mongo-password

# Optional: Configure Redis connection
REDIS_URL=redis://localhost:6379/0
REDIS_PASSWORD=your-redis-password

# Optional: enable scheduled tasks
ENABLE_SCHEDULED_TASK=true

# Optional: choose task execution backend
TASK_BACKEND=arq  # local or arq
```

::: tip
LLM models are configured through the **Model Config UI** after deployment — no environment variables needed.
:::

</details>

### Code Quality

```bash
make format       # Format (ruff format)
make lint         # Lint (ruff check)
make typecheck    # Type check (mypy)
make test         # Backend tests (pytest)
make check-all    # Run all checks (lint + typecheck + test)
```

### Frontend, Mobile, and Docs

```bash
cd frontend
pnpm run build             # TypeScript + Vite build
pnpm run packaged:build    # Build packaged frontend assets
pnpm run mobile:sync       # Build and sync Capacitor projects
pnpm run package:desktop   # Package desktop app assets

cd ..
pnpm run docs:dev          # VitePress docs site
pnpm run docs:build
```

### Project Structure

```text
.
├── main.py                  # Uvicorn entrypoint for src.api.main:app
├── src/
│   ├── agents/              # Core, fast, search, and team agent graphs
│   ├── api/                 # FastAPI app, middleware, and route modules
│   │   └── routes/          # Chat, auth, MCP, skills, files, scheduler, teams, etc.
│   ├── infra/               # Runtime services: auth, llm, mcp, scheduler, task, storage, memory
│   └── kernel/              # Settings, schemas, config definitions, and shared types
├── frontend/
│   ├── src/                 # React app source
│   │   ├── components/      # Chat, panels, pages, auth, skill, MCP, team, file UI
│   │   ├── services/        # API clients and browser service integrations
│   │   ├── stores/          # Frontend state stores
│   │   ├── i18n/            # Locale files and tests
│   │   └── workers/         # Browser/PWA workers
│   ├── android/             # Capacitor Android project
│   ├── ios/                 # Capacitor iOS project
│   ├── src-tauri/           # Tauri desktop shell
│   └── scripts/             # Frontend build, packaging, and i18n scripts
├── docs/                    # VitePress documentation
├── deploy/                  # Docker Compose deployment
├── k8s/                     # Kubernetes manifests
├── nginx/                   # Reverse proxy config
├── scripts/                 # Sandbox and maintenance utilities
└── tests/                   # Backend, API, infra, agent, and unit tests
```

## ⭐ Star History

<a href="https://star-history.com/#Yanyutin753/LambChat&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Yanyutin753/LambChat&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Yanyutin753/LambChat&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Yanyutin753/LambChat&type=Date" />
 </picture>
</a>

## 📄 License

[MIT](LICENSE) — Project name "LambChat" and its logo may not be changed or removed.

---

<div align="center">

<sub><strong>LambChat</strong> is built for people who want AI agents that can actually do the work.</sub>

<br>

<strong>Created by <a href="https://github.com/Yanyutin753">Clivia</a></strong>

<br>

<a href="https://github.com/Yanyutin753">GitHub</a> · <a href="mailto:3254822118@qq.com">Email</a> · <a href="README_CN.md">中文 README</a>

<br><br>

<img src=".github/images/wechat-qr.webp" width="160" alt="WeChat QR Code">

<br>

<sub>WeChat for deployment help, product feedback, and collaboration</sub>

</div>

<sub><a href="https://linux.do/">LINUX DO - A New Ideal Community</a></sub>
