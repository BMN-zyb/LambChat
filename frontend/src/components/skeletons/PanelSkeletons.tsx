import { SkeletonLine } from "./primitives";
import { PanelHeaderSkeleton } from "./PanelHeaderSkeleton";

/* ═══════════════════════════════════════════════════════
   Panel-specific skeletons — updated to match latest layouts
   ═══════════════════════════════════════════════════════ */

/** Skills panel: card grid matching SkillBaseCard (.scb) structure */
export function SkillsPanelSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-4 animate-fade-in">
      <PanelHeaderSkeleton />
      <div className="skill-content-area flex-1 overflow-y-auto py-2 sm:py-4 px-4 lg:px-8 lg:py-8">
        <div className="skill-grid grid auto-grid-cols gap-4">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="scb">
              {/* Banner */}
              <div
                className="h-12 w-full shrink-0 relative"
                style={{
                  background: `linear-gradient(135deg, ${
                    [
                      "var(--theme-primary-light)",
                      "color-mix(in srgb, var(--theme-primary-light) 60%, var(--theme-bg))",
                      "var(--theme-bg-card)",
                    ][i % 3]
                  }, var(--theme-bg-card))`,
                }}
              />
              {/* Card body */}
              <div className="flex flex-1 flex-col -mt-3 pt-5 p-4">
                {/* Icon + name */}
                <div className="flex items-start gap-3">
                  <div className="scb__icon-ring shrink-0 skeleton-line" />
                  <div className="min-w-0 flex-1">
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-3/4" : "w-1/2"}
                      className="!h-4"
                    />
                  </div>
                </div>
                {/* Status pill */}
                <div className="mt-1.5 sm:mt-2">
                  <SkeletonLine
                    width="w-14 sm:w-16"
                    className="!h-4 !rounded-full"
                  />
                </div>
                {/* Description */}
                <div className="mt-3 space-y-1.5">
                  <SkeletonLine width="w-full" className="!h-2.5 sm:!h-3" />
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-5/6" : "w-2/3"}
                    className="!h-2.5 sm:!h-3"
                  />
                </div>
                {/* Tags */}
                <div className="mt-3 flex flex-wrap gap-1.5 sm:gap-2">
                  <SkeletonLine
                    width="w-14 sm:w-16"
                    className="!h-4 sm:!h-5 !rounded-full"
                  />
                  <SkeletonLine
                    width="w-20 sm:w-24"
                    className="!h-4 sm:!h-5 !rounded-full"
                  />
                  <SkeletonLine
                    width="w-12 sm:w-14"
                    className="!h-4 sm:!h-5 !rounded-full"
                  />
                </div>
                <div className="flex-1" />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider px-3 py-3 sm:px-4 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Marketplace panel: card grid matching SkillBaseCard (.scb) structure */
export function MarketplacePanelSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-4 animate-fade-in">
      <PanelHeaderSkeleton />
      <div className="skill-content-area flex-1 overflow-y-auto py-2 sm:py-4 px-4 sm:p-6 lg:px-8 lg:py-8">
        <div className="grid auto-grid-cols gap-5">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="scb">
              {/* Banner */}
              <div
                className="h-12 w-full shrink-0 relative"
                style={{
                  background: `linear-gradient(135deg, ${
                    [
                      "var(--theme-primary-light)",
                      "color-mix(in srgb, var(--theme-primary-light) 60%, var(--theme-bg))",
                      "var(--theme-bg-card)",
                    ][i % 3]
                  }, var(--theme-bg-card))`,
                }}
              />
              {/* Card body */}
              <div className="flex flex-1 flex-col -mt-3 pt-5 p-3 sm:p-4">
                <div className="flex items-start gap-2.5 sm:gap-3">
                  {/* Icon overlapping banner */}
                  <div className="scb__icon-ring shrink-0 skeleton-line" />
                  <div className="min-w-0 flex-1">
                    <SkeletonLine
                      width={i % 3 === 0 ? "w-3/4" : "w-1/2"}
                      className="!h-[15px] sm:!h-[16px]"
                    />
                    <SkeletonLine
                      width="w-16 sm:w-20"
                      className="!h-2.5 sm:!h-3 mt-1"
                    />
                  </div>
                </div>
                <div className="mt-2 space-y-1">
                  <SkeletonLine width="w-full" className="!h-2.5 sm:!h-3" />
                  <SkeletonLine width="w-4/5" className="!h-2.5 sm:!h-3" />
                </div>
                <div className="mt-2.5 sm:mt-3 flex items-center justify-between">
                  <SkeletonLine
                    width="w-12 sm:w-14"
                    className="!h-4 sm:!h-5 !rounded-full"
                  />
                  <div className="flex items-center gap-1.5">
                    <div className="skeleton-line size-7 rounded-lg" />
                    <div className="skeleton-line size-7 rounded-lg" />
                  </div>
                </div>
                <div className="flex-1" />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider px-3 py-3 sm:px-4 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Users panel: table rows (desktop) + cards (mobile) */
export function UsersPanelSkeleton() {
  return (
    <div className="flex h-full flex-col gap-4 animate-fade-in">
      <PanelHeaderSkeleton />
      <div className="flex-1 overflow-y-auto min-h-0 py-2 sm:py-4 px-4">
        {/* Desktop table */}
        <div className="hidden sm:block">
          <div className="glass-card rounded-xl !p-0 overflow-hidden">
            {/* Table header */}
            <div
              className="flex items-center gap-4 px-6 py-3"
              style={{
                backgroundColor:
                  "var(--glass-bg-subtle, color-mix(in srgb, var(--theme-bg) 80%, white))",
              }}
            >
              <SkeletonLine width="w-24 xl:w-28" className="!h-3 !rounded" />
              <SkeletonLine
                width="w-32 xl:w-44"
                className="!h-3 !rounded flex-1"
              />
              <SkeletonLine width="w-20 xl:w-24" className="!h-3 !rounded" />
              <SkeletonLine width="w-16" className="!h-3 !rounded" />
              <SkeletonLine width="w-20 xl:w-28" className="!h-3 !rounded" />
              <SkeletonLine width="w-16 xl:w-20" className="!h-3 !rounded" />
            </div>
            {Array.from({ length: 8 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-4 px-6 py-4"
                style={{
                  borderTop:
                    "1px solid var(--glass-border, var(--theme-border))",
                }}
              >
                <div className="flex items-center gap-3 w-28 xl:w-32 shrink-0">
                  <div className="skeleton-line size-8 rounded-full shrink-0" />
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-16 xl:w-20" : "w-20 xl:w-24"}
                    className="!h-4"
                  />
                </div>
                <SkeletonLine
                  width={i % 3 === 0 ? "w-36 xl:w-52" : "w-44 xl:w-60"}
                  className="!h-3.5 flex-1"
                />
                <div className="flex gap-1 w-20 xl:w-24 shrink-0">
                  <SkeletonLine width="w-14" className="!h-5 !rounded-full" />
                </div>
                <SkeletonLine
                  width="w-16"
                  className="!h-5 !rounded-full shrink-0"
                />
                <SkeletonLine width="w-20" className="!h-3 shrink-0" />
                <div className="flex gap-1 w-16 shrink-0">
                  <div className="skeleton-line size-7 rounded-lg" />
                  <div className="skeleton-line size-7 rounded-lg" />
                </div>
              </div>
            ))}
          </div>
        </div>
        {/* Mobile cards */}
        <div className="space-y-3 sm:hidden">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="glass-card rounded-xl p-4">
              <div className="flex items-start gap-3">
                <div className="skeleton-line size-10 rounded-full shrink-0" />
                <div className="flex-1 min-w-0">
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-24" : "w-20"}
                    className="!h-4"
                  />
                  <SkeletonLine width="w-36" className="!h-3 mt-1" />
                </div>
                <div className="skeleton-line size-7 rounded-lg shrink-0" />
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                <SkeletonLine width="w-14" className="!h-5 !rounded-full" />
                <SkeletonLine width="w-16" className="!h-5 !rounded-full" />
              </div>
              <div className="mt-3 flex items-center justify-between">
                <SkeletonLine width="w-16" className="!h-5 !rounded-full" />
                <SkeletonLine width="w-20" className="!h-3" />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider px-3 py-3 sm:px-6 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Roles panel: single-column card list matching real RolesPanel layout */
export function RolesPanelSkeleton() {
  return (
    <div className="flex h-full flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton />
      <div className="flex-1 overflow-y-auto py-2 sm:py-4 px-4">
        <div className="grid gap-4">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="panel-card">
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-start gap-2 flex-1 min-w-0">
                  {/* Icon box — matches real h-8 w-8 rounded-lg */}
                  <div
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
                    style={{
                      backgroundColor:
                        "var(--glass-bg-subtle, color-mix(in srgb, var(--theme-bg) 80%, white))",
                    }}
                  >
                    <div className="skeleton-line size-[14px] rounded-sm" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-20 sm:w-28" : "w-28 sm:w-36"}
                      className="!h-[15px] sm:!h-[16px]"
                    />
                    <SkeletonLine
                      width="w-3/4"
                      className="!h-2.5 sm:!h-3 mt-1.5 sm:mt-2"
                    />
                    {/* Permission count text */}
                    <SkeletonLine
                      width="w-20"
                      className="!h-2.5 sm:!h-3 mt-0.5 !opacity-50"
                    />
                  </div>
                </div>
                {/* Action buttons — chevron + edit + delete */}
                <div className="flex items-center gap-1.5 sm:gap-2 shrink-0">
                  <div className="skeleton-line size-5 rounded" />
                  <div className="skeleton-line size-7 sm:size-8 rounded-lg" />
                  <div className="skeleton-line size-7 sm:size-8 rounded-lg hidden" />
                </div>
              </div>
              {/* Timestamp row */}
              <div className="mt-3 flex items-center gap-4">
                <SkeletonLine
                  width="w-24 sm:w-28"
                  className="!h-2.5 !opacity-50"
                />
                <SkeletonLine
                  width="w-24 sm:w-28"
                  className="!h-2.5 !opacity-50"
                />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider px-3 py-3 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** MCP panel: card grid matching real MCPServerCard (pps-card) structure */
export function MCPPanelSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton />
      <div className="flex-1 overflow-y-auto py-2 sm:py-4 px-4">
        <div className="grid auto-grid-cols gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="pps-card group flex h-full flex-col overflow-hidden rounded-xl border border-[var(--theme-border)] bg-[var(--theme-bg-card)] shadow-sm"
            >
              {/* Banner */}
              <div
                className="pps-card__banner relative h-12 shrink-0"
                style={{
                  background: `linear-gradient(135deg, ${
                    [
                      "var(--theme-primary-light)",
                      "color-mix(in srgb, var(--theme-primary-light) 60%, var(--theme-bg))",
                      "var(--theme-bg-card)",
                    ][i % 3]
                  }, var(--theme-bg-card))`,
                }}
              >
                {/* Status badges on banner */}
                <div className="absolute bottom-1.5 left-3 flex gap-1">
                  <SkeletonLine
                    width="w-10 sm:w-12"
                    className="!h-3.5 !rounded-full"
                  />
                  <SkeletonLine
                    width="w-8 sm:w-10"
                    className="!h-3.5 !rounded-full"
                  />
                </div>
              </div>
              {/* Card body */}
              <div className="flex flex-1 flex-col -mt-3 pt-5 p-3">
                <div className="flex items-start gap-2.5">
                  <div className="scb__icon-ring shrink-0 skeleton-line" />
                  <div className="min-w-0 flex-1">
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-24 sm:w-36" : "w-20 sm:w-28"}
                      className="!h-[15px] sm:!h-[16px]"
                    />
                    {/* Transport badge */}
                    <SkeletonLine
                      width="w-10 sm:w-12"
                      className="!h-3.5 !rounded-full mt-1"
                    />
                  </div>
                </div>
                {/* URL/command */}
                <div className="mt-2">
                  <SkeletonLine width="w-3/5" className="!h-3 !rounded-md" />
                </div>
                <div className="flex-1" />
              </div>
              {/* Footer */}
              <div className="flex items-center justify-between px-3 py-2 border-t border-[var(--theme-border)]">
                <div className="flex items-center gap-1.5">
                  <div className="skeleton-line size-7 rounded-lg" />
                  <div className="skeleton-line size-7 rounded-lg" />
                </div>
                <div className="skeleton-line w-10 sm:w-12 h-5 !rounded-full" />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider px-3 py-3 sm:px-4 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Feedback panel: stats cards + feedback items */
export function FeedbackPanelSkeleton() {
  return (
    <div className="flex h-full flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />

      {/* Stats section */}
      <div className="grid grid-cols-2 gap-3 px-4 sm:grid-cols-4 sm:gap-4 sm:px-6">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="glass-card rounded-xl p-4">
            <div className="flex items-center gap-2 sm:gap-3">
              <div
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg"
                style={{
                  backgroundColor:
                    "var(--glass-bg-subtle, color-mix(in srgb, var(--theme-bg) 80%, white))",
                }}
              >
                <div className="skeleton-line size-6 rounded-md" />
              </div>
              <div className="min-w-0">
                <SkeletonLine width="w-10 sm:w-12" className="!h-2.5 sm:!h-3" />
                <SkeletonLine
                  width="w-6 sm:w-8"
                  className="!h-5 sm:!h-6 mt-1"
                />
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Feedback list */}
      <div className="flex-1 overflow-y-auto py-2 sm:py-4 px-4 sm:px-6">
        {/* Desktop */}
        <div className="hidden space-y-3 sm:block">
          {Array.from({ length: 7 }).map((_, i) => (
            <div key={i} className="glass-card rounded-xl p-5">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 sm:gap-3 min-w-0">
                  <div className="skeleton-line size-9 sm:size-10 rounded-full shrink-0" />
                  <div className="min-w-0">
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-16 sm:w-20" : "w-20 sm:w-24"}
                      className="!h-3.5 sm:!h-4"
                    />
                    <SkeletonLine
                      width="w-32 sm:w-40"
                      className="!h-2.5 !mt-1 !opacity-50"
                    />
                  </div>
                </div>
                <SkeletonLine
                  width="w-12 sm:w-16"
                  className="!h-5 sm:!h-6 !rounded-full shrink-0"
                />
              </div>
              <SkeletonLine
                width="w-3/4"
                className="!h-2.5 sm:!h-3 mt-2.5 sm:mt-3"
              />
            </div>
          ))}
        </div>
        {/* Mobile */}
        <div className="space-y-3 sm:hidden">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="glass-card rounded-xl p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <div className="skeleton-line size-9 rounded-full shrink-0" />
                  <div className="min-w-0">
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-16" : "w-20"}
                      className="!h-3.5"
                    />
                    <SkeletonLine
                      width="w-28"
                      className="!h-2.5 !mt-1 !opacity-50"
                    />
                  </div>
                </div>
                <SkeletonLine
                  width="w-12"
                  className="!h-5 !rounded-full shrink-0"
                />
              </div>
              <SkeletonLine width="w-3/4" className="!h-2.5 mt-2" />
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider bg-transparent px-4 py-4 sm:px-6 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line w-24 h-3" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Scheduled task panel: header + grid of task cards matching real layout */
export function ScheduledTaskPanelSkeleton() {
  return (
    <div className="glass-shell scheduled-task-panel flex h-full min-h-0 flex-col animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />

      <div className="flex-1 overflow-y-auto px-4 py-3 sm:p-6">
        <div className="grid auto-grid-cols gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="glass-card scheduled-task-card">
              <div className="scheduled-task-card__content">
                {/* Title + status badge */}
                <div className="scheduled-task-card__title-row">
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-40 sm:w-56" : "w-32 sm:w-44"}
                    className="!h-[15px] sm:!h-[15px]"
                  />
                  <SkeletonLine
                    width="w-14 sm:w-16"
                    className="!h-5 !rounded-full shrink-0"
                  />
                </div>

                {/* Description (2-line clamped in real card) */}
                <div className="space-y-1">
                  <SkeletonLine width="w-full" className="!h-2.5 sm:!h-3" />
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-4/5" : "w-2/3"}
                    className="!h-2.5 sm:!h-3"
                  />
                </div>

                {/* Meta pills — matches .scheduled-task-meta flex-wrap with bordered pill items */}
                <div className="scheduled-task-meta">
                  {[0, 1, 2].map((j) => (
                    <SkeletonLine
                      key={j}
                      width={
                        j === 0
                          ? "w-24 sm:w-32"
                          : j === 1
                            ? "w-20 sm:w-28"
                            : "w-16 sm:w-24"
                      }
                      className="!h-[26px] !rounded-full"
                    />
                  ))}
                </div>

                {/* Subtle last-run info — matches .scheduled-task-card__subtle */}
                <div className="scheduled-task-card__subtle flex items-center gap-2">
                  <SkeletonLine width="w-12 sm:w-14" className="!h-3" />
                  <SkeletonLine width="w-20 sm:w-28" className="!h-3" />
                  <SkeletonLine
                    width="w-12 sm:w-14"
                    className="!h-4 !rounded-full shrink-0"
                  />
                </div>
              </div>

              {/* Action buttons — matches .scheduled-task-card__actions (border-top, justify-end) */}
              <div className="scheduled-task-card__actions">
                {[0, 1, 2, 3].map((j) => (
                  <div
                    key={j}
                    className="skeleton-line size-9 rounded-lg"
                  />
                ))}
              </div>
            </div>
          ))}
        </div>

        {/* Pagination placeholder */}
        <div className="glass-divider bg-transparent px-4 py-4 sm:px-6 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line h-3 w-24" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Task session list (drill-down from scheduled task panel): header with subtitle + back button + session cards */
export function TaskSessionListSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} hasSubtitle />
      <div className="flex-1 overflow-y-auto px-4 py-3 sm:p-6">
        <div className="scheduled-task-list">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="glass-card scheduled-task-session-card w-full text-left">
              {/* Left indicator — matches .scheduled-task-session-card__indicator (2.5rem × 2.5rem) */}
              <div className="scheduled-task-session-card__indicator">
                <div className="skeleton-line size-4 rounded" />
              </div>
              {/* Body — matches .scheduled-task-session-card__body (grid, gap: 0.25rem) */}
              <div className="scheduled-task-session-card__body">
                <SkeletonLine
                  width={i % 2 === 0 ? "w-2/3" : "w-1/2"}
                  className="!h-[14px] sm:!h-[15px]"
                />
                <div className="scheduled-task-session-card__meta">
                  <SkeletonLine
                    width="w-16 sm:w-20"
                    className="!h-2.5 !opacity-50"
                  />
                  <SkeletonLine
                    width="w-3"
                    className="!h-2.5 !opacity-30"
                  />
                  <SkeletonLine
                    width="w-20 sm:w-28"
                    className="!h-2.5 !opacity-50"
                  />
                </div>
              </div>
              {/* Trail — matches .scheduled-task-session-card__trail (unread badge + chevron) */}
              <div className="scheduled-task-session-card__trail flex items-center gap-2 shrink-0">
                {i % 3 === 0 && (
                  <div className="skeleton-line size-5 rounded-full" />
                )}
                <div className="skeleton-line size-4 rounded shrink-0" />
              </div>
            </div>
          ))}
        </div>
        {/* Pagination placeholder */}
        <div className="glass-divider bg-transparent px-4 py-4 sm:px-6 mt-2">
          <div className="flex items-center justify-center gap-2">
            <div className="skeleton-line size-8 rounded-lg" />
            <div className="skeleton-line h-3 w-24" />
            <div className="skeleton-line size-8 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Channels page: card grid matching SkillBaseCard (.scb) structure */
export function ChannelsGridSkeleton() {
  return (
    <div className="flex h-full flex-col animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />
      <div className="flex-1 overflow-y-auto py-4">
        <div className="mx-auto max-w-full">
          <div className="grid auto-grid-cols gap-4 p-3 sm:p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="scb">
                {/* Banner */}
                <div
                  className="h-12 w-full shrink-0 relative"
                  style={{
                    background: `linear-gradient(135deg, ${
                      [
                        "var(--theme-primary-light)",
                        "color-mix(in srgb, var(--theme-primary-light) 60%, var(--theme-bg))",
                        "var(--theme-bg-card)",
                      ][i % 3]
                    }, var(--theme-bg-card))`,
                  }}
                />
                {/* Card body */}
                <div className="flex flex-1 flex-col -mt-3 pt-5 p-4">
                  {/* Icon + name */}
                  <div className="flex items-start gap-3">
                    <div className="scb__icon-ring shrink-0 skeleton-line" />
                    <div className="min-w-0 flex-1">
                      <SkeletonLine
                        width={i % 2 === 0 ? "w-3/4" : "w-1/2"}
                        className="!h-4"
                      />
                    </div>
                  </div>
                  {/* Status pill */}
                  <div className="mt-1.5 sm:mt-2">
                    <SkeletonLine
                      width={
                        i % 3 === 0
                          ? "w-16 sm:w-20"
                          : i % 3 === 1
                            ? "w-12"
                            : "w-8"
                      }
                      className="!h-4 !rounded-full"
                    />
                  </div>
                  {/* Description */}
                  <div className="mt-3 space-y-1.5">
                    <SkeletonLine width="w-full" className="!h-2.5 sm:!h-3" />
                    <SkeletonLine
                      width={i % 2 === 0 ? "w-5/6" : "w-2/3"}
                      className="!h-2.5 sm:!h-3"
                    />
                  </div>
                  {/* Tags row */}
                  <div className="mt-3">
                    <SkeletonLine
                      width="w-20 sm:w-24"
                      className="!h-5 !rounded-lg"
                    />
                  </div>
                  <div className="flex-1" />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

/** Channels panel: form-based configuration (status card + config form) */
export function ChannelConfigSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />
      <div className="flex-1 overflow-y-auto py-2 sm:py-4 px-4">
        <div className="space-y-4">
          {/* Status card */}
          <div className="glass-card rounded-xl p-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <SkeletonLine width="w-8" className="!h-3" />
                <SkeletonLine width="w-24 sm:w-32" className="!h-4" />
              </div>
              <SkeletonLine width="w-20 sm:w-24" className="!h-8 !rounded-lg" />
            </div>
          </div>
          {/* Configuration card — form fields */}
          <div className="glass-card rounded-xl p-4">
            <div className="space-y-4">
              {/* Instance name field */}
              <div className="space-y-1.5">
                <SkeletonLine width="w-20" className="!h-3" />
                <div className="skeleton-line h-10 w-full rounded-lg" />
              </div>
              {/* Toggle row */}
              <div className="flex items-center justify-between">
                <SkeletonLine width="w-24" className="!h-3.5" />
                <SkeletonLine width="w-10 h-5" className="!rounded-full" />
              </div>
              {/* Additional fields */}
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="space-y-1.5">
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-28" : "w-16"}
                    className="!h-3"
                  />
                  <div className="skeleton-line h-10 w-full rounded-lg" />
                </div>
              ))}
              {/* Agent selector */}
              <div className="space-y-1.5">
                <SkeletonLine width="w-16" className="!h-3" />
                <div className="skeleton-line h-10 w-full rounded-lg" />
              </div>
            </div>
          </div>
          {/* Help card */}
          <div className="glass-card-subtle rounded-xl p-4">
            <SkeletonLine width="w-16" className="!h-4 !mb-2" />
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-start gap-2 mb-1.5">
                <SkeletonLine
                  width="w-4"
                  className="!h-4 !rounded-full !shrink-0"
                />
                <SkeletonLine
                  width={i === 0 ? "w-3/4" : i === 1 ? "w-2/3" : "w-4/5"}
                  className="!h-3"
                />
              </div>
            ))}
          </div>
        </div>
      </div>
      {/* Footer action buttons */}
      <div className="border-t border-[var(--theme-border)] px-3 py-3 sm:px-4">
        <div className="flex items-center justify-end gap-2">
          <SkeletonLine width="w-16 sm:w-20" className="!h-9 !rounded-lg" />
          <SkeletonLine width="w-20 sm:w-24" className="!h-9 !rounded-lg" />
        </div>
      </div>
    </div>
  );
}

/** Agent panel: single divided container with tab switcher */
export function AgentPanelSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />
      {/* Tab bar — segmented control */}
      <div className="inline-grid grid-cols-2 rounded-lg border border-[var(--glass-border)] bg-[var(--glass-bg-subtle)] p-1 sm:my-3">
        <div className="flex items-center justify-center gap-2 rounded-md px-3 py-2">
          <SkeletonLine width="w-16 sm:w-20" className="!h-4" />
        </div>
        <div className="flex items-center justify-center gap-2 rounded-md px-3 py-2">
          <SkeletonLine width="w-12 sm:w-16" className="!h-4 !opacity-50" />
        </div>
      </div>
      {/* Description text */}
      <div className="px-4 sm:px-6">
        <SkeletonLine
          width="w-3/4"
          className="!h-3 !opacity-60 hidden sm:block"
        />
      </div>
      {/* Agent list — single glass-card with divide-y (matches real layout) */}
      <div className="flex-1 overflow-y-auto px-4 sm:px-6">
        <div className="glass-card rounded-xl divide-y divide-[var(--theme-border)]">
          {Array.from({ length: 7 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center justify-between gap-3 px-4 py-3.5"
            >
              <div className="flex items-center gap-3 min-w-0 flex-1">
                <div
                  className="flex size-10 shrink-0 items-center justify-center rounded-xl ring-1 ring-[var(--theme-border)]"
                  style={{
                    backgroundColor:
                      "var(--glass-bg-subtle, color-mix(in srgb, var(--theme-bg) 80%, white))",
                  }}
                >
                  <div className="skeleton-line size-5 rounded-md" />
                </div>
                <div className="min-w-0 flex-1">
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-20 sm:w-28" : "w-28 sm:w-36"}
                    className="!h-[13px] sm:!h-[14px]"
                  />
                  <SkeletonLine
                    width="w-3/5"
                    className="!h-2.5 sm:!h-3 mt-1 hidden sm:block"
                  />
                </div>
              </div>
              <div className="skeleton-line w-8 sm:w-10 h-4 sm:h-5 rounded-full shrink-0" />
            </div>
          ))}
        </div>
        {/* Save bar */}
        <div className="glass-divider px-4 py-3 mt-3">
          <div className="flex justify-end">
            <SkeletonLine width="w-20 sm:w-24" className="!h-9 !rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Model panel: model config rows with tab switcher */
export function ModelPanelSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 sm:gap-4 animate-fade-in">
      <PanelHeaderSkeleton hasSearch={false} />
      {/* Tab bar — segmented control */}
      <div className="inline-grid grid-cols-2 rounded-lg border border-[var(--glass-border)] bg-[var(--glass-bg-subtle)] p-1 sm:my-3">
        <div className="flex items-center justify-center gap-2 rounded-md px-3 py-2">
          <SkeletonLine width="w-14 sm:w-16" className="!h-4" />
        </div>
        <div className="flex items-center justify-center gap-2 rounded-md px-3 py-2">
          <SkeletonLine width="w-20 sm:w-28" className="!h-4 !opacity-50" />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-3 py-4 sm:px-6 sm:py-5 space-y-3">
        {/* Toolbar — description text + action buttons on right */}
        <div className="flex items-center justify-between gap-3">
          <SkeletonLine
            width="w-48"
            className="!h-3.5 !opacity-60 hidden sm:block"
          />
          <div className="flex items-center gap-1.5 sm:gap-2 ml-auto">
            <div className="skeleton-line h-8 w-16 sm:w-20 rounded-lg" />
            <div className="skeleton-line h-8 w-16 sm:w-20 rounded-lg hidden sm:block" />
            <div className="skeleton-line h-8 w-16 sm:w-20 rounded-lg hidden sm:block" />
            <div className="skeleton-line h-8 w-16 sm:w-20 rounded-lg" />
          </div>
        </div>
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="glass-card rounded-xl">
            {/* Desktop row */}
            <div className="hidden sm:flex items-center justify-between p-4 gap-2">
              <div className="flex items-center gap-2 sm:gap-3 flex-1 min-w-0">
                {/* Drag handle */}
                <div className="skeleton-line size-4 rounded shrink-0 !opacity-30" />
                <div className="skeleton-line size-5 rounded shrink-0" />
                <div className="flex-1 min-w-0">
                  <SkeletonLine
                    width={i % 2 === 0 ? "w-24 sm:w-32" : "w-20 sm:w-28"}
                    className="!h-[13px] sm:!h-[14px]"
                  />
                </div>
              </div>
              <div className="flex items-center gap-1 sm:gap-1.5 shrink-0">
                {/* Expand chevron */}
                <div className="skeleton-line size-5 rounded" />
                <div className="skeleton-line w-8 sm:w-10 h-4 sm:h-5 rounded-full" />
                <div className="skeleton-line size-7 sm:size-8 rounded-lg" />
                <div className="skeleton-line size-7 sm:size-8 rounded-lg" />
              </div>
            </div>
            {/* Mobile row */}
            <div className="block sm:hidden p-3.5">
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <div className="skeleton-line size-4 rounded shrink-0 !opacity-30" />
                <div className="skeleton-line size-5 rounded shrink-0" />
                <SkeletonLine
                  width={i % 2 === 0 ? "w-24" : "w-20"}
                  className="!h-[13px] flex-1"
                />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
