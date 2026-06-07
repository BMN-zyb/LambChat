import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, Filter } from "lucide-react";
import type { ScheduledTaskStatus as ScheduledTaskStatusType } from "../../../types/scheduledTask";

const STATUS_OPTIONS: Array<{
  value: ScheduledTaskStatusType | "";
  labelKey: string;
}> = [
  { value: "", labelKey: "scheduledTask.allStatuses" },
  { value: "active", labelKey: "scheduledTask.active" },
  { value: "paused", labelKey: "scheduledTask.paused" },
];

export function StatusFilter({
  value,
  onChange,
}: {
  value: ScheduledTaskStatusType | undefined;
  onChange: (value: ScheduledTaskStatusType | undefined) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
        setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const activeLabel =
    STATUS_OPTIONS.find((o) => o.value === (value ?? ""))?.labelKey ??
    STATUS_OPTIONS[0].labelKey;

  const handleSelect = (optionValue: ScheduledTaskStatusType | "") => {
    onChange(optionValue || undefined);
    setOpen(false);
  };

  return (
    <div ref={ref} className="relative shrink-0" data-filter-menu>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
        className={`btn-secondary panel-filter-trigger h-10 px-3 ${
          value ? "border-[var(--theme-primary)] text-[var(--theme-text)]" : ""
        }`}
      >
        <Filter size={16} />
        <span className="hidden sm:inline panel-filter-trigger__label">
          {t(activeLabel)}
        </span>
        <ChevronDown
          size={16}
          className={`text-[var(--theme-text-secondary)] transition-transform ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {open && (
        <div
          className="panel-filter-menu absolute right-0 top-[calc(100%+0.375rem)] z-20 w-40 rounded-xl border py-2"
          role="menu"
        >
          {STATUS_OPTIONS.map((option) => {
            const isActive = option.value === (value ?? "");
            return (
              <button
                key={option.value || "all"}
                type="button"
                role="menuitemradio"
                aria-checked={isActive}
                onClick={() => handleSelect(option.value)}
                className={`flex w-full items-center gap-2 px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-[var(--theme-primary-light)] text-[var(--theme-text)]"
                    : "text-[var(--theme-text-secondary)] hover:bg-[var(--glass-bg)]"
                }`}
              >
                <span className="min-w-0 flex-1 text-left">
                  {t(option.labelKey)}
                </span>
                {isActive && (
                  <Check size={14} />
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
