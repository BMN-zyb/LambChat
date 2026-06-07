# Scheduled Task Panel Polish Design

Date: 2026-06-07

## Goal

Polish the PR's scheduled-task UI so it matches the existing management-panel style used across LambChat. The work stays focused on scheduled-task surfaces and the new visible i18n copy that appears in those surfaces.

## Scope

- Scheduled task main panel.
- Task create/edit modal.
- Run history modal.
- Task session drill-down list.
- Per-conversation scheduled-task floating panel.
- Sidebar scheduled-task entry.
- Newly exposed scheduled-task Chinese copy that currently renders as placeholder text.

Out of scope:

- Broad redesigns of unrelated panels.
- New shared component libraries beyond small local helpers or class names.
- Behavioral changes to scheduled-task APIs, routing, permissions, pagination, or mutation refresh semantics.

## Design Direction

Use the existing management-panel language instead of inventing a new visual system:

- Keep `PanelHeader` for panel identity and primary actions.
- Keep the `glass-shell`, `glass-card`, and `glass-divider` surface model.
- Move repeated scheduled-task styling into focused local CSS classes where it reduces class duplication.
- Use stable icon-button dimensions for action clusters.
- Keep cards dense, scannable, and work-focused.
- Preserve the current neutral stone palette, with semantic accent colors only for success, warning, running, and destructive states.

## UI Details

The main task list will use a more consistent card hierarchy:

- Title, status badge, and optional description at the top.
- Trigger, agent, model, and run-count metadata grouped into compact rows or chips.
- Last-run information presented consistently with run status.
- Action buttons aligned in a stable right-side cluster on desktop and wrapped safely on smaller screens.

The create/edit modal will unify:

- Header and footer spacing.
- Form input, select, textarea, and JSON textarea treatments.
- Trigger-type segmented controls.
- Toggle rows.
- Primary and secondary buttons.

The run-history modal will unify:

- Modal chrome with the task form modal.
- Run-list selected state.
- Empty, loading, and no-conversation states.
- Conversation message spacing and bubble treatment.

The conversation scheduled-task panel will use the same compact card and action-button language as the main list while staying narrow-panel friendly.

The sidebar scheduled-task entry will keep current behavior, but spacing, status dot, unread badge, loading, and nested session affordances should visually align with other sidebar items.

## i18n

Replace scheduled-task Chinese placeholder strings with natural Chinese copy:

- `conversationTasks`: 会话定时任务
- `details`: 详情
- `noConversationTasks`: 当前会话暂无 Agent 创建的定时任务

Other newly exposed placeholder keys outside this scheduled-task surface remain untouched unless they appear in the polished surfaces.

## Accessibility And Responsiveness

- Icon-only buttons keep `title` text and stable hit targets.
- Text should truncate or wrap without overlapping action buttons.
- Modal content must remain scrollable within the viewport.
- Mobile layouts should not depend on hero-scale type or wide horizontal rows.

## Verification

- Run the frontend lint or typecheck/build command available in the repo.
- If practical, start the Vite dev server and inspect the scheduled task route manually.
- Confirm no `【待翻译】scheduledTask.*` text remains in the affected Chinese scheduled-task UI.
- Confirm the existing language package edits are preserved and not reverted.
