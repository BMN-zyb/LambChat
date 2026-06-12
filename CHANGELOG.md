# Changelog

## v2.5.1 (2026-06-13)

### ✨ New Features

- **Mobile Support** — Capacitor integration for Android/iOS, native safe area support, file proxy streaming, pptx preview
- **Desktop Auto-Update** — One-click auto-update for Tauri (desktop) and Capacitor (mobile)
- **Native Notifications** — Push notification support for Android and Tauri
- **Memory Components** — Memory recall and store components with detailed UI
- **Scheduled Tasks** — Scheduler module with permission management and execution (#155)
- **Session Sidebar** — Full refactor with actions, effects hooks, channel delivery, and tool items (#156, #158)
- **Subagent Panel** — Footer with timestamp rendering, status indication styles
- **Shared UI Components** — Input component with leading icons, collapsible DetailSection, panel header improvements
- **Persona Preview** — Source view toggle, button style enhancements
- **Mark All Read** — Batch read with project and scheduled task filtering
- **Feishu** — Add recommendation_input to message handler
- **Image URL Conversion** — Support image_url to base64 in model profiles and middleware
- **File Preview** — Link interception logic, Virtuoso follow output
- **Distributed Runtime** — Validation and improved secret management
- **New Tool Items** — Sandbox MCP, Scheduled Tasks, Teams

### 🐛 Bug Fixes

- Fix 404 on shared page
- Fix chat scroll bottom lock
- Dedupe scheduled task fire slots and memory backend reset tasks
- Accept future awaitables in background scheduling and cleanup hooks
- Release background task references on shutdown
- Fix team list count awaitable handling
- Enhance ZIP member path validation
- Bound GitHub skill preview downloads
- Harden packaged app and CI release builds
- Share packaged frontend build correctly

### ♻️ Refactors

- Streamline skill handling by removing unused effectiveSkills computation
- Implement file streaming proxy for native app environments
- Optimize scheduler and improve OAuth reliability
- Improve panel dropdown accessibility and mobile responsiveness
- Consolidate panel header mobile density and overflow menu
- Enhance file type acceptance for images, videos, and audio

### 🧪 Tests

- Unit test for effective skills in chat skill selector
- Test stable skill list params prevent refetch loops
- Virtuoso follow output tests
- File preview link interception tests
- Subagent panel footer and style tests

### 📖 Documentation

- Update README and README_CN with scheduled tasks and task runtime details

### 🔧 Desktop

- Auto-clean app data (localStorage, webview cache) on version upgrade — ensures fresh state after install

---

## v2.5.0 (2026-06-04)

### ✨ New Features

- **Team Collaboration** — Full team management with CRUD API, role-based subagent dispatch, Team Builder UI, team picker modal, and agent collaboration pipeline
- **Multimodal Vision Support** — Image inlining via data URLs, multimodal model integration, optimized backend performance for vision tasks
- **Excalidraw Preview** — Full preview support with dark fullscreen viewer, card thumbnails, blob-URL rendering, and direct image loading
- **Image Generation & Editing** — Image generation with size normalization, image editing tool, standardized filename generation
- **Active Goal System** — Rubric-guided execution, goal tracking for Feishu agent execution
- **Agent Catalog** — Skill preferences, recommended questions, persona preset auto-switching
- **Distributed Architecture** — Consistent-hash node assignment, distributed connection checks
- **i18n** — Replace hardcoded strings with translation keys across frontend
- **Feishu Enhancements** — Media handling, deep linking support, enhanced channel registration
- **Tool Error Handling** — Robust tool error handling and refined chat UI
- **MCP Sidebar** — Expandable tool items, image generation in internal registry
- **Chat Toolbar** — Refactored toolbar with code text selection and native copy
- **Image Preview** — RevealPreviewHost image preview support
- **PWA Improvements** — Toast styles, icons, and provider labels
- **Client-side Pagination** — Selectors with accurate skill counts
- **Configurable Limits** — MCP, session, and event merger limits

### 🐛 Bug Fixes

- Fix Excalidraw thumbnail rendering for proper object-contain scaling
- Fix API request body replay logic in middleware
- Fix frontend race conditions in agent loading
- Fix Feishu lease release on cancelled startup
- Fix UI auto-preview on mobile devices
- Fix help menu visibility and message duplication
- Fix file reveal artifact deduplication in chat messages
- Fix team_id wiring through entire task submission pipeline
- Fix team builder stale references and default_member_id resolution
- Fix conditional S3 file deletion in session manager
- Fix shared content event upper bound removal

### ♻️ Refactors

- Enhance type safety, async task handling, and sandbox protocol standardization
- Optimize startup and task recovery with concurrency
- Improve robustness of backend protocols and sandbox tools
- Replace asyncio.to_thread with custom run_blocking_io utility
- Improve resource lifecycle management and agent routing logic
- Enhance arq worker executor resolution and concurrency cleanup
- Refactor team-based skill and persona constraints
- Optimize recommendation generation and system stability
- Refactor chat UI — tab bars as segmented controls, mobile UX, tool feedback
- Optimize postgres checkpointer and model configuration
- Improve api_key resolution and agent event handling

### 🎨 UI/UX

- Redesign tab bars as segmented controls
- Improve mobile UX and component usability
- Refine team list page with minimalist cards
- Update app icons
- Improve welcome page layout

### 🧪 Tests

- Comprehensive tests for various tools and configurations
- Regression tests for team and agent features
- Test cases for session cancellation, memory, storage limits
- Tests for Feishu handler, sender, registration, and manager
- Extensive API route tests and infrastructure tests

### 📦 Build

- Docker: optimize dependency installation and update entrypoint

### 📝 Documentation

- Overhaul README documentation and product presentation
- Add team UX theme styling design

---

## v2.4.1

Previous release.
