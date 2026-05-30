import { Wrench, Globe, Clock } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CollapsiblePill, CopyButton, LoadingSpinner } from "../../common";
import type { CollapsibleStatus } from "../../common";
import { ToolResultContent } from "./items/McpBlockPreview";
import { openPersistentToolPanel } from "./items/persistentToolPanelState";

/** Returns the number of seconds elapsed since the tool started, or 0 when not pending. */
function useElapsedSeconds(isPending?: boolean, startedAt?: string): number {
  const [elapsed, setElapsed] = useState(0);
  const startTimeMs = useRef<number | null>(null);
  const raf = useRef<number>(0);

  useEffect(() => {
    if (isPending) {
      // Use the tool's actual start time from the backend, fall back to now
      startTimeMs.current = startedAt
        ? new Date(startedAt).getTime()
        : Date.now();
      setElapsed(0);
      const tick = () => {
        if (startTimeMs.current !== null) {
          setElapsed(Math.floor((Date.now() - startTimeMs.current) / 1000));
          raf.current = requestAnimationFrame(tick);
        }
      };
      raf.current = requestAnimationFrame(tick);
      return () => cancelAnimationFrame(raf.current);
    } else {
      startTimeMs.current = null;
      setElapsed(0);
      return () => cancelAnimationFrame(raf.current);
    }
  }, [isPending, startedAt]);

  return elapsed;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

/** Compute the tool's final duration in seconds from startedAt/completedAt ISO strings. */
function computeDuration(
  startedAt?: string,
  completedAt?: string,
): number | null {
  if (!startedAt || !completedAt) return null;
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 0) return null;
  return Math.round(ms / 1000);
}

/** Format duration for display: <60s → "Xs", >=60s → "Xm Ys". For sub-second use ".Xs". */
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

// Re-export all sub-components
export { ReadFileItem } from "./items/ReadFileItem";
export { EditFileItem } from "./items/EditFileItem";
export { WriteFileItem } from "./items/WriteFileItem";
export { GrepItem } from "./items/GrepItem";
export { LsItem } from "./items/LsItem";
export { GlobItem } from "./items/GlobItem";
export { ExecuteItem } from "./items/ExecuteItem";
export { FileRevealItem } from "./items/FileRevealItem";
export { ProjectRevealItem } from "./items/ProjectRevealItem";

// Collapsible Tool Call Item (compact design)
export function ToolCallItem({
  name,
  args,
  result,
  success,
  isPending,
  cancelled,
  startedAt,
  completedAt,
}: {
  name: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  success?: boolean;
  isPending?: boolean;
  cancelled?: boolean;
  startedAt?: string;
  completedAt?: string;
}) {
  const { t } = useTranslation();
  const hasResult = result !== undefined;
  const elapsedSeconds = useElapsedSeconds(isPending, startedAt);
  const finalDuration = computeDuration(startedAt, completedAt);

  const durationFooter = useMemo(() => {
    if (finalDuration === null) return undefined;
    return (
      <div className="flex items-center gap-1.5 px-4 py-2 text-xs text-stone-400 dark:text-stone-500 border-t border-stone-100 dark:border-stone-800">
        <Clock size={11} className="shrink-0" />
        <span className="tabular-nums">{formatDuration(finalDuration)}</span>
      </div>
    );
  }, [finalDuration]);

  // Parse MCP server name from tool name (format: "server_name:tool_name")
  const colonIdx = name.indexOf(":");
  const isMcpTool = colonIdx > 0;
  const serverName = isMcpTool ? name.substring(0, colonIdx) : null;
  const toolName = isMcpTool ? name.substring(colonIdx + 1) : name;
  const formattedToolName = toolName
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(" ");

  const displayArgs = (() => {
    if (args.partial !== undefined) {
      try {
        return JSON.parse(args.partial as string);
      } catch {
        return { partial: args.partial };
      }
    }
    return args;
  })();

  const hasArgs = Object.keys(displayArgs).length > 0;
  const argsJson = JSON.stringify(displayArgs, null, 2);

  let status: CollapsibleStatus = "idle";
  if (isPending) {
    status = "loading";
  } else if (cancelled) {
    status = "cancelled";
  } else if (success) {
    status = "success";
  } else if (hasResult) {
    status = "error";
  }

  const canExpand = hasArgs || hasResult;

  const panelContent = canExpand && (
    <div className="space-y-3 max-h-full overflow-y-auto p-2 sm:p-4 [&_pre]:!text-sm">
      {hasArgs && (
        <div className="group/args relative p-3 sm:p-4 rounded-lg sm:rounded-xl bg-stone-100 dark:bg-stone-700/50">
          <div className="text-xs uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-2 font-medium">
            {t("chat.message.args")}
          </div>
          <pre className="text-sm text-stone-600 dark:text-stone-300 overflow-x-auto overflow-y-auto min-w-0 font-mono">
            {argsJson}
          </pre>
          <div className="absolute top-2 right-2 opacity-0 group-hover/args:opacity-100 transition-opacity">
            <CopyButton text={argsJson} size={12} />
          </div>
        </div>
      )}

      {hasResult && (
        <div className="group/result relative p-3 sm:p-4 rounded-lg sm:rounded-xl bg-stone-100 dark:bg-stone-700/50">
          <div className="text-xs uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-2 font-medium">
            {t("chat.message.result")}
          </div>
          <ToolResultContent result={result} hideCopyButton />
          <div className="absolute top-2 right-2 opacity-0 group-hover/result:opacity-100 transition-opacity">
            <CopyButton
              text={
                typeof result === "string"
                  ? result
                  : JSON.stringify(result, null, 2)
              }
              size={12}
            />
          </div>
        </div>
      )}

      {isPending && (
        <div className="flex items-center gap-2 text-xs text-amber-600 dark:text-amber-400">
          <LoadingSpinner size="xs" />
          <span>{t("chat.message.running")}</span>
          <span className="tabular-nums">{formatElapsed(elapsedSeconds)}</span>
        </div>
      )}

      {!isPending && finalDuration !== null && (
        <div className="flex items-center gap-1.5 text-xs text-stone-400 dark:text-stone-500">
          <Clock size={11} className="shrink-0" />
          <span className="tabular-nums">{formatDuration(finalDuration)}</span>
        </div>
      )}
    </div>
  );

  return (
    <>
      <CollapsiblePill
        status={status}
        icon={
          isMcpTool ? (
            <Globe size={12} className="shrink-0 opacity-50" />
          ) : (
            <Wrench size={12} className="shrink-0 opacity-50" />
          )
        }
        label={toolName}
        suffix={
          serverName ? (
            <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-white/30 dark:bg-black/20 opacity-70 font-medium truncate max-w-[120px]">
              {serverName}
            </span>
          ) : undefined
        }
        variant="tool"
        expandable={canExpand}
        onPanelOpen={() => {
          if (!canExpand) return;
          openPersistentToolPanel({
            title: formattedToolName,
            icon: isMcpTool ? <Globe size={16} /> : <Wrench size={16} />,
            status,
            subtitle: serverName || undefined,
            children: panelContent,
            footer: durationFooter,
          });
        }}
      >
        {canExpand && (
          <div className="mt-2 ml-4 pl-3 border-l-2 border-stone-200/60 dark:border-stone-700/50 space-y-2 max-h-96 overflow-y-auto min-w-0">
            {hasArgs && (
              <div className="group/args relative p-2 rounded-md bg-stone-50/80 dark:bg-stone-800/50">
                <div className="text-xs uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-1 font-medium">
                  {t("chat.message.args")}
                </div>
                <pre className="text-xs text-stone-600 dark:text-stone-300 overflow-x-auto max-h-40 overflow-y-auto min-w-0">
                  {argsJson}
                </pre>
                <div className="absolute top-1 right-1 opacity-0 group-hover/args:opacity-100 transition-opacity">
                  <CopyButton text={argsJson} size={10} />
                </div>
              </div>
            )}

            {hasResult && (
              <div className="group/result relative p-2 rounded-md bg-stone-50/80 dark:bg-stone-800/50">
                <div className="text-xs uppercase tracking-wider text-stone-400 dark:text-stone-500 mb-1 font-medium">
                  {t("chat.message.result")}
                </div>
                <ToolResultContent result={result} hideCopyButton />
                <div className="absolute top-1 right-1 opacity-0 group-hover/result:opacity-100 transition-opacity">
                  <CopyButton
                    text={
                      typeof result === "string"
                        ? result
                        : JSON.stringify(result, null, 2)
                    }
                    size={10}
                  />
                </div>
              </div>
            )}

            {isPending && (
              <div className="flex items-center gap-2 text-xs text-amber-600 dark:text-amber-400">
                <LoadingSpinner size="xs" />
                <span>{t("chat.message.running")}</span>
                <span className="tabular-nums">
                  {formatElapsed(elapsedSeconds)}
                </span>
              </div>
            )}

            {!isPending && finalDuration !== null && (
              <div className="flex items-center gap-1.5 text-xs text-stone-400 dark:text-stone-500">
                <Clock size={11} className="shrink-0" />
                <span className="tabular-nums">
                  {formatDuration(finalDuration)}
                </span>
              </div>
            )}
          </div>
        )}
      </CollapsiblePill>
    </>
  );
}
