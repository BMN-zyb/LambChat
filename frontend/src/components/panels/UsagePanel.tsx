/**
 * Usage Details Panel — Token consumption tracking
 *
 * Theme-adaptive design. Responsive: mobile → tablet → desktop
 */

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Zap,
  ArrowDownToLine,
  ArrowUpFromLine,
  Clock,
  Bot,
  Activity,
  RefreshCw,
  DatabaseZap,
  LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import toast from "react-hot-toast";
import { PanelHeader } from "../common/PanelHeader";
import { PanelFilterSelect } from "../common";
import { Pagination } from "../common/Pagination";
import { UsagePanelSkeleton } from "../skeletons";
import { usageApi } from "../../services/api/usage";
import { useAuth } from "../../hooks/useAuth";
import { formatDateTimeShort, formatDuration } from "../../utils/datetime";
import { Permission } from "../../types";
import type { UsageLog, UsageStats } from "../../types/usage";

// ── Helpers ──────────────────────────────────────────────

function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedValue(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debouncedValue;
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toString();
}

function fmtDur(seconds: number): string {
  if (seconds <= 0) return "-";
  return formatDuration(seconds * 1000);
}

// ── Stat Metric ──────────────────────────────────────────

function StatMetric({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string | number;
}) {
  return (
    <div className="group relative flex min-w-0 flex-col gap-1 rounded-lg px-3 py-2.5 transition-colors duration-150 hover:bg-[var(--usage-surface-hover)]">
      <div className="flex items-center gap-2">
        <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-[var(--usage-icon-bg)] text-[var(--theme-primary)]">
          <Icon size={13} strokeWidth={2} />
        </div>
        <p className="truncate text-[13px] font-medium leading-none text-theme-text-tertiary font-serif">
          {label}
        </p>
      </div>
      <p className="mt-0.5 pl-8 truncate text-lg font-bold leading-none tracking-tight text-theme-text tabular-nums">
        {value}
      </p>
    </div>
  );
}

function LogsSectionHeader({
  shown,
  total,
  page,
  pageSize,
  title,
}: {
  shown: number;
  total: number;
  page: number;
  pageSize: number;
  title: string;
  subtitle: string;
}) {
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(start + shown - 1, total);

  return (
    <div className="mb-3 flex items-center justify-between gap-4 sm:mb-4">
      <h2 className="text-sm font-semibold text-theme-text">{title}</h2>
      {total > 0 && (
        <span className="shrink-0 text-[11px] tabular-nums text-theme-text-tertiary">
          {start}–{end} / {total}
        </span>
      )}
    </div>
  );
}

// ── Status Pill ──────────────────────────────────────────

function StatusPill({ status }: { status: string }) {
  const ok = status === "completed";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium tabular-nums ${
        ok
          ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "bg-red-500/10 text-red-500 dark:text-red-400"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          ok ? "bg-emerald-500" : "bg-red-500"
        }`}
      />
      {ok ? "OK" : "Err"}
    </span>
  );
}

// ── Desktop Row ─────────────────────────────────────────

function DesktopRow({
  log,
  isAdmin,
  showCache,
}: {
  log: UsageLog;
  index: number;
  isAdmin: boolean;
  showCache: boolean;
}) {
  return (
    <tr className="group border-b border-stone-200/60 dark:border-white/[0.06] transition-colors duration-150 hover:bg-[var(--glass-bg-subtle)]/50">
      <td className="whitespace-nowrap px-4 py-2.5 text-sm tabular-nums text-theme-text-secondary">
        {log.started_at ? formatDateTimeShort(log.started_at) : "-"}
      </td>
      {isAdmin && (
        <td className="whitespace-nowrap px-4 py-2.5">
          <div className="flex items-center gap-2">
            <div className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--usage-icon-bg)] text-[10px] font-bold text-[var(--theme-primary)]">
              {(log.username || "-").charAt(0).toUpperCase()}
            </div>
            <span className="text-sm text-theme-text">
              {log.username || "-"}
            </span>
          </div>
        </td>
      )}
      <td className="whitespace-nowrap px-4 py-2.5">
        <code className="rounded bg-[var(--usage-code-bg)] px-2 py-0.5 text-xs font-medium text-theme-text tabular-nums">
          {log.model || "-"}
        </code>
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-sm text-theme-text-tertiary font-serif">
        {log.agent_name || "-"}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-right text-sm tabular-nums text-theme-text-secondary">
        {fmt(log.input_tokens)}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-right text-sm tabular-nums text-theme-text-secondary">
        {fmt(log.output_tokens)}
      </td>
      {showCache && (
        <td className="whitespace-nowrap px-4 py-2.5 text-right text-sm tabular-nums text-theme-text-tertiary">
          {log.cache_read_tokens > 0 ? (
            fmt(log.cache_read_tokens)
          ) : (
            <span className="opacity-15">-</span>
          )}
        </td>
      )}
      <td className="whitespace-nowrap px-4 py-2.5 text-right text-sm tabular-nums font-semibold text-[var(--theme-primary)]">
        {fmt(log.total_tokens)}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-right text-sm tabular-nums text-theme-text-tertiary">
        {fmtDur(log.duration)}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-center">
        <StatusPill status={log.status} />
      </td>
    </tr>
  );
}

// ── Mobile Card ──────────────────────────────────────────

function MobileCard({
  log,
  isAdmin,
  t,
}: {
  log: UsageLog;
  isAdmin: boolean;
  t: TFunction;
}) {
  const hasCache = log.cache_read_tokens > 0;

  return (
    <div className="usage-surface group overflow-hidden rounded-lg transition-colors duration-150 hover:border-[var(--usage-border-hover)]">
      <div className="px-3 py-2.5">
        {/* Header: Model + Status */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-[var(--usage-icon-bg)] text-[var(--theme-primary)]">
              <Bot size={12} />
            </div>
            <div className="min-w-0">
              <code className="block truncate text-xs font-semibold text-theme-text tabular-nums">
                {log.model || "-"}
              </code>
              {log.agent_name && (
                <p className="truncate text-[10px] text-theme-text-tertiary">
                  {log.agent_name}
                </p>
              )}
            </div>
          </div>
          <StatusPill status={log.status} />
        </div>

        {/* Token metrics */}
        <div className="mt-2 grid grid-cols-3 gap-1.5 rounded-md bg-[var(--usage-inset-bg)] px-2 py-1.5">
          <div className="min-w-0 flex-1">
            <span className="block text-[9px] font-medium text-theme-text-tertiary">
              {t("usage.inTokens")}
            </span>
            <span className="mt-0.5 block truncate text-xs font-semibold tabular-nums text-theme-text-secondary">
              {fmt(log.input_tokens)}
            </span>
          </div>
          <div className="min-w-0 flex-1">
            <span className="block text-[9px] font-medium text-theme-text-tertiary">
              {t("usage.outTokens")}
            </span>
            <span className="mt-0.5 block truncate text-xs font-semibold tabular-nums text-theme-text-secondary">
              {fmt(log.output_tokens)}
            </span>
          </div>
          <div className="min-w-0 flex-1">
            <span className="block text-[9px] font-medium text-theme-text-tertiary">
              {t("usage.totalTokens")}
            </span>
            <span className="mt-0.5 block truncate text-xs font-semibold tabular-nums text-[var(--theme-primary)]">
              {fmt(log.total_tokens)}
            </span>
          </div>
        </div>

        {/* Cache read */}
        {hasCache && (
          <div className="mt-1.5 flex items-center gap-2 rounded-md bg-[var(--usage-inset-bg)] px-2 py-1">
            <span className="text-[10px] text-theme-text-tertiary">
              {t("usage.cacheRead")}
            </span>
            <span className="ml-auto text-[10px] font-semibold tabular-nums text-theme-text-secondary">
              {fmt(log.cache_read_tokens)}
            </span>
          </div>
        )}

        {/* Footer */}
        <div className="mt-2 flex items-center justify-between text-[11px] text-theme-text-tertiary">
          <span className="tabular-nums">
            {log.started_at ? formatDateTimeShort(log.started_at) : "-"}
          </span>
          <div className="flex items-center gap-2">
            {isAdmin && log.username && (
              <span className="text-theme-text-secondary">{log.username}</span>
            )}
            {log.duration > 0 && (
              <span className="tabular-nums">{fmtDur(log.duration)}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Component ─────────────────────────────────────

export function UsagePanel() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const isAdmin = hasPermission(Permission.USAGE_ADMIN);

  const [logs, setLogs] = useState<UsageLog[]>([]);
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [total, setTotal] = useState(0);

  const [skip, setSkip] = useState(0);
  const pageSize = 20;

  const [searchQuery, setSearchQuery] = useState("");
  const [period, setPeriod] = useState<string>("all");
  const debouncedSearch = useDebounce(searchQuery, 300);
  const handleSearchQueryChange = useCallback((query: string) => {
    setSkip(0);
    setSearchQuery(query);
  }, []);
  const handlePeriodChange = useCallback((nextPeriod: string) => {
    setSkip(0);
    setPeriod(nextPeriod);
  }, []);

  const computeDateRange = useCallback((p: string): { start_date?: string } => {
    const now = new Date();
    if (p === "today") {
      return {
        start_date: new Date(
          now.getFullYear(),
          now.getMonth(),
          now.getDate(),
        ).toISOString(),
      };
    }
    if (p === "week")
      return { start_date: new Date(now.getTime() - 7 * 864e5).toISOString() };
    if (p === "month")
      return { start_date: new Date(now.getTime() - 30 * 864e5).toISOString() };
    return {};
  }, []);

  // Keep a live ref to t so fetchData doesn't depend on it — prevents
  // potential infinite loop if useTranslation() produces unstable references.
  const tRef = useRef(t);
  tRef.current = t;

  const fetchData = useCallback(async () => {
    setIsLoading(true);
    try {
      const dateRange = computeDateRange(period);
      const response = await usageApi.list({
        skip,
        limit: pageSize,
        search: isAdmin && debouncedSearch ? debouncedSearch : undefined,
        ...dateRange,
      });
      setLogs(response.items);
      setTotal(response.total);
      setStats(response.stats);
    } catch (err) {
      toast.error((err as Error).message || tRef.current("usage.loadFailed"));
    } finally {
      setIsLoading(false);
    }
  }, [skip, period, debouncedSearch, isAdmin, computeDateRange]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const isInitialLoading =
    isLoading && logs.length === 0 && searchQuery.trim().length === 0;

  const page = Math.floor(skip / pageSize) + 1;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const hasAnyCache = useMemo(
    () => logs.some((l) => l.cache_read_tokens > 0),
    [logs],
  );

  const cacheHitLabel = useMemo(() => {
    if (!stats || stats.total_input_tokens <= 0) return "0%";
    return `${Math.round(
      (stats.total_cache_read_tokens / stats.total_input_tokens) * 100,
    )}%`;
  }, [stats]);

  useEffect(() => {
    if (!isLoading && total > 0 && page > totalPages) {
      setSkip((totalPages - 1) * pageSize);
    }
  }, [isLoading, page, total, totalPages]);

  // ── Header controls ──
  const periodFilter = (
    <PanelFilterSelect
      value={period}
      onChange={handlePeriodChange}
      active={period !== "all"}
      options={[
        { value: "all", label: t("usage.allPeriods") },
        { value: "today", label: t("usage.today") },
        { value: "week", label: t("usage.thisWeek") },
        { value: "month", label: t("usage.thisMonth") },
      ]}
    />
  );

  const refreshButton = (
    <button
      type="button"
      className="inline-flex h-10 w-10 items-center justify-center rounded-lg border border-[var(--glass-border)] bg-[var(--glass-bg-card)] text-theme-text-secondary transition-colors hover:border-[var(--glass-border-hover)] hover:bg-[var(--glass-bg-hover)] hover:text-theme-text disabled:cursor-not-allowed disabled:opacity-50"
      onClick={fetchData}
      disabled={isLoading}
      aria-label={t("usage.refresh", "刷新")}
      title={t("usage.refresh", "刷新")}
    >
      <RefreshCw size={16} className={isLoading ? "animate-spin" : undefined} />
    </button>
  );

  if (isInitialLoading) {
    return <UsagePanelSkeleton />;
  }

  return (
    <div className="glass-shell usage-panel flex h-full min-h-0 flex-col overflow-y-auto">
      {/* Header */}
      <PanelHeader
        title={t("usage.title")}
        subtitle={t("usage.subtitle")}
        icon={<Activity size={22} className="text-theme-text-secondary" />}
        searchValue={isAdmin ? searchQuery : undefined}
        onSearchChange={isAdmin ? handleSearchQueryChange : undefined}
        searchPlaceholder={t("usage.searchPlaceholder")}
        searchAccessory={isAdmin ? periodFilter : undefined}
        searchActions={isAdmin ? refreshButton : undefined}
        actions={
          !isAdmin ? (
            <>
              {periodFilter}
              {refreshButton}
            </>
          ) : undefined
        }
      />

      {/* Stat Metrics */}
      {stats && (
        <div className="px-4 py-2 sm:px-6 sm:py-3">
          <div className="usage-surface grid grid-cols-2 gap-1 rounded-lg p-1.5 sm:grid-cols-3 lg:grid-cols-5">
            <StatMetric
              icon={Zap}
              label={t("usage.totalRequests")}
              value={fmt(stats.total_requests)}
            />
            <StatMetric
              icon={ArrowDownToLine}
              label={t("usage.inputTokens")}
              value={fmt(stats.total_input_tokens)}
            />
            <StatMetric
              icon={ArrowUpFromLine}
              label={t("usage.outputTokens")}
              value={fmt(stats.total_output_tokens)}
            />
            <StatMetric
              icon={Clock}
              label={t("usage.totalDuration")}
              value={fmtDur(stats.total_duration)}
            />
            <StatMetric
              icon={DatabaseZap}
              label={t("usage.cacheHitRate", "缓存命中")}
              value={cacheHitLabel}
            />
          </div>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 px-4 py-2 sm:px-6 sm:py-4">
        {isLoading && logs.length > 0 && (
          <div className="pointer-events-none mb-2 flex justify-center">
            <div className="mt-2 inline-flex items-center gap-2 rounded-full bg-[var(--glass-bg-card)] px-3 py-1.5 text-xs text-theme-text-tertiary shadow-sm">
              <RefreshCw size={11} className="animate-spin" />
              {t("usage.refreshing", "刷新中")}
            </div>
          </div>
        )}
        {logs.length === 0 ? (
          <div className="flex min-h-[260px] flex-col items-center justify-center py-16 text-center sm:h-full sm:py-24">
            <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-2xl bg-[var(--glass-bg-subtle)] sm:h-16 sm:w-16">
              <Activity size={24} className="text-theme-text-tertiary/30" />
            </div>
            <p className="text-sm text-theme-text-secondary/60">
              {t("usage.noUsage")}
            </p>
            <p className="mt-1 text-xs text-theme-text-tertiary/40">
              {t("usage.noUsageHint")}
            </p>
          </div>
        ) : (
          <>
            <LogsSectionHeader
              shown={logs.length}
              total={total}
              page={page}
              pageSize={pageSize}
              title={t("usage.logDetails", "日志明细")}
              subtitle={t(
                "usage.statsScopeHint",
                "顶部统计为当前筛选下全部数据",
              )}
            />

            {/* ── Desktop Table ── */}
            <div className="hidden lg:block">
              <div className="usage-surface overflow-x-auto rounded-lg">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-stone-200/80 dark:border-white/10">
                      <th className="whitespace-nowrap px-4 py-2 text-left text-[13px] font-medium text-theme-text-tertiary font-serif">
                        {t("usage.time")}
                      </th>
                      {isAdmin && (
                        <th className="whitespace-nowrap px-4 py-2 text-left text-[13px] font-medium text-theme-text-tertiary font-serif">
                          {t("usage.user")}
                        </th>
                      )}
                      <th className="whitespace-nowrap px-4 py-2 text-left text-[13px] font-medium text-theme-text-tertiary font-serif">
                        {t("usage.model")}
                      </th>
                      <th className="whitespace-nowrap px-4 py-2 text-left text-[13px] font-medium text-theme-text-tertiary font-serif">
                        {t("usage.agent")}
                      </th>
                      <th className="whitespace-nowrap px-4 py-2 text-right text-[11px] font-medium text-theme-text-tertiary">
                        {t("usage.inTokens")}
                      </th>
                      <th className="whitespace-nowrap px-4 py-2 text-right text-[11px] font-medium text-theme-text-tertiary">
                        {t("usage.outTokens")}
                      </th>
                      {hasAnyCache && (
                        <th className="whitespace-nowrap px-4 py-2 text-right text-[11px] font-medium text-theme-text-tertiary">
                          {t("usage.cacheRead")}
                        </th>
                      )}
                      <th className="whitespace-nowrap px-4 py-2 text-right text-[11px] font-medium text-theme-text-tertiary">
                        {t("usage.totalTokens")}
                      </th>
                      <th className="whitespace-nowrap px-4 py-2 text-right text-[11px] font-medium text-theme-text-tertiary">
                        {t("usage.duration")}
                      </th>
                      <th className="whitespace-nowrap px-4 py-2 text-center text-[11px] font-medium text-theme-text-tertiary">
                        {t("usage.status")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((log, i) => (
                      <DesktopRow
                        key={log.trace_id}
                        log={log}
                        index={i}
                        isAdmin={isAdmin}
                        showCache={hasAnyCache}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* ── Tablet compact ── */}
            <div className="hidden sm:block lg:hidden">
              <div className="space-y-1.5">
                {logs.map((log) => (
                  <div
                    key={log.trace_id}
                    className="usage-surface group flex items-center gap-3 rounded-lg px-3.5 py-2.5 transition-colors duration-150 hover:border-[var(--usage-border-hover)]"
                  >
                    <div className="w-24 shrink-0">
                      <p className="text-xs tabular-nums text-theme-text-secondary">
                        {log.started_at
                          ? formatDateTimeShort(log.started_at)
                          : "-"}
                      </p>
                      <p className="text-[10px] tabular-nums text-theme-text-tertiary">
                        {fmtDur(log.duration)}
                      </p>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <code className="truncate text-xs font-medium text-theme-text tabular-nums">
                          {log.model || "-"}
                        </code>
                        <StatusPill status={log.status} />
                      </div>
                      {log.agent_name && (
                        <p className="truncate text-[10px] text-theme-text-tertiary">
                          {log.agent_name}
                        </p>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-xs tabular-nums shrink-0">
                      <span className="text-theme-text-secondary">
                        {fmt(log.input_tokens)}
                      </span>
                      <span className="text-theme-text-secondary">
                        {fmt(log.output_tokens)}
                      </span>
                      <span className="font-semibold text-[var(--theme-primary)]">
                        {fmt(log.total_tokens)}
                      </span>
                    </div>
                    {isAdmin && log.username && (
                      <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[var(--usage-icon-bg)] text-[9px] font-bold text-[var(--theme-primary)]">
                        {log.username.charAt(0).toUpperCase()}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>

            {/* ── Mobile cards ── */}
            <div className="space-y-2 sm:hidden pb-4">
              {logs.map((log) => (
                <MobileCard
                  key={log.trace_id}
                  log={log}
                  isAdmin={isAdmin}
                  t={t}
                />
              ))}
            </div>
          </>
        )}
      </div>

      {/* Pagination */}
      {total > pageSize && (
        <div className="glass-divider px-4 py-3 sm:px-6">
          <Pagination
            page={page}
            pageSize={pageSize}
            total={total}
            itemLabel={t("usage.logItems", "条日志")}
            onChange={(p) => setSkip((p - 1) * pageSize)}
          />
        </div>
      )}
    </div>
  );
}

export default UsagePanel;
