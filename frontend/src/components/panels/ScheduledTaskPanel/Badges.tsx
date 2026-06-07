import { useTranslation } from "react-i18next";

/** Status badge for task status display */
export function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();

  const styles: Record<string, string> = {
    active:
      "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400",
    paused:
      "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-400",
  };

  const dotStyles: Record<string, string> = {
    active: "bg-emerald-500",
    paused: "bg-stone-400",
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${
        styles[status] || styles.paused
      }`}
    >
      <span
        className={`inline-block h-1.5 w-1.5 rounded-full ${
          dotStyles[status] || dotStyles.paused
        }`}
      />
      {t(`scheduledTask.${status}`)}
    </span>
  );
}

/** Run status badge */
export function RunStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    success:
      "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400",
    failed: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
    running:
      "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
    pending:
      "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-400",
    timeout:
      "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400",
    skipped:
      "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-400",
  };

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        styles[status] || styles.pending
      }`}
    >
      {status}
    </span>
  );
}
