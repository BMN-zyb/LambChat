import { SkeletonLine } from "./primitives";

/**
 * Matches PanelHeader layout:
 *   - Icon box: size-10/lg:size-11 rounded-lg subtle-bg + ring
 *   - Title (text-base/lg:text-lg) + optional subtitle
 *   - Desktop action buttons
 *   - Mobile menu button (when !hasSearch)
 *   - Optional search row
 */
export function PanelHeaderSkeleton({
  hasSearch = true,
  hasSubtitle = false,
}: {
  hasSearch?: boolean;
  hasSubtitle?: boolean;
}) {
  return (
    <div className="panel-header">
      <div className="panel-header__top flex flex-wrap items-center justify-between gap-3 lg:gap-4">
        {/* Identity — icon box + title */}
        <div className="panel-header__identity flex min-w-0 items-center gap-3 lg:gap-4">
          {/* Icon box — matches real PanelHeader: size-10 lg:size-11 rounded-lg subtle-bg + ring-1 */}
          <div
            className="flex size-10 flex-shrink-0 items-center justify-center rounded-lg ring-1 lg:size-11"
            style={{
              backgroundColor: "var(--theme-bg-subtle, color-mix(in srgb, var(--theme-bg) 80%, white))",
              borderColor: "var(--theme-border)",
            }}
          >
            <div className="skeleton-line size-5 rounded-md" />
          </div>
          <div className="min-w-0">
            <SkeletonLine
              width="w-28 sm:w-36 xl:w-48"
              className="!h-4 sm:!h-[18px]"
            />
            {hasSubtitle && (
              <SkeletonLine
                width="w-40 sm:w-52 xl:w-64"
                className="!h-3 sm:!h-[14px] mt-0.5 !opacity-60"
              />
            )}
          </div>
        </div>

        {/* Desktop action buttons */}
        <div className="panel-header__desktop-actions flex flex-nowrap flex-shrink-0 items-center gap-1.5 sm:gap-2">
          <div className="skeleton-line h-10 w-24 sm:w-28 rounded-lg" />
          <div className="skeleton-line h-10 w-24 sm:w-28 rounded-lg hidden sm:block" />
        </div>

        {/* Mobile menu button (shown when no search row, matching real PanelHeader mobile-menu pattern) */}
        {!hasSearch && (
          <div className="skeleton-line size-9 rounded-lg sm:hidden" />
        )}
      </div>

      {/* Search row */}
      {hasSearch && (
        <div className="panel-header__search-row mt-2 flex items-center gap-2 sm:mt-3 lg:mt-4">
          <div className="skeleton-line h-10 flex-1 rounded-lg" />
        </div>
      )}
    </div>
  );
}
