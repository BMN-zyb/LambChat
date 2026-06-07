import { useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { CalendarClock, Pencil, Plus, Timer } from "lucide-react";
import { EditorSidebar } from "../../common/EditorSidebar";
import type { ScheduledTask, ScheduledTaskCreate, TriggerType } from "../../../types/scheduledTask";
import type { AgentInfo } from "../../../types/agent";
import type { AvailableModel } from "../../../contexts/SettingsContext";
import { buildScheduledTaskInputPayload, getAgentOptionsFromScheduledTaskPayload } from "../scheduledTaskPayload";
import { toDateTimeLocalValue } from "./utils";

/** Create/Edit form sidebar */
export function TaskFormModal({
  task,
  agents,
  availableModels,
  defaultAgentId,
  defaultModelId,
  defaultModelValue,
  onSave,
  onClose,
}: {
  task: ScheduledTask | null;
  agents: AgentInfo[];
  availableModels: AvailableModel[] | null;
  defaultAgentId?: string;
  defaultModelId?: string;
  defaultModelValue?: string;
  onSave: (data: ScheduledTaskCreate) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const isEdit = !!task;
  const taskAgentOptions = getAgentOptionsFromScheduledTaskPayload(
    task?.input_payload,
  );
  const initialModelId =
    (typeof taskAgentOptions.model_id === "string"
      ? taskAgentOptions.model_id
      : "") ||
    defaultModelId ||
    "";
  const initialModelValue =
    (typeof taskAgentOptions.model === "string" ? taskAgentOptions.model : "") ||
    defaultModelValue ||
    "";

  const [name, setName] = useState(task?.name ?? "");
  const [description, setDescription] = useState(task?.description ?? "");
  const [agentId, setAgentId] = useState(task?.agent_id ?? defaultAgentId ?? "");
  const [modelId, setModelId] = useState(initialModelId);
  const [modelValue, setModelValue] = useState(initialModelValue);
  const [triggerType, setTriggerType] = useState<TriggerType>(
    task?.trigger_type ?? "interval",
  );
  const [intervalSeconds, setIntervalSeconds] = useState(
    task?.trigger_type === "interval"
      ? String((task?.trigger_config as { seconds?: number })?.seconds ?? 300)
      : "300",
  );
  const [runDate, setRunDate] = useState(
    task?.trigger_type === "date"
      ? toDateTimeLocalValue((task?.trigger_config as { run_date?: string })?.run_date)
      : toDateTimeLocalValue(null),
  );
  const [cronHour, setCronHour] = useState(
    task?.trigger_type === "cron"
      ? String((task?.trigger_config as { hour?: string })?.hour ?? "0")
      : "0",
  );
  const [cronMinute, setCronMinute] = useState(
    task?.trigger_type === "cron"
      ? String((task?.trigger_config as { minute?: string })?.minute ?? "0")
      : "0",
  );
  const [cronSecond, setCronSecond] = useState(
    task?.trigger_type === "cron"
      ? String((task?.trigger_config as { second?: string })?.second ?? "0")
      : "0",
  );
  const [cronDay, setCronDay] = useState(
    task?.trigger_type === "cron"
      ? String((task?.trigger_config as { day?: string })?.day ?? "")
      : "",
  );
  const [cronMonth, setCronMonth] = useState(
    task?.trigger_type === "cron"
      ? String((task?.trigger_config as { month?: string })?.month ?? "")
      : "",
  );
  const [cronDayOfWeek, setCronDayOfWeek] = useState(
    task?.trigger_type === "cron"
      ? String(
          (task?.trigger_config as { day_of_week?: string })?.day_of_week ?? "",
        )
      : "",
  );
  const [inputPayload, setInputPayload] = useState(
    task ? JSON.stringify(task.input_payload ?? {}, null, 2) : "{}",
  );
  const [enabled, setEnabled] = useState(task?.enabled ?? true);
  const [runOnStart, setRunOnStart] = useState(task?.run_on_start ?? false);
  const [maxRetries, setMaxRetries] = useState(
    String(task?.max_retries ?? 0),
  );
  const [timeoutSeconds, setTimeoutSeconds] = useState(
    String(task?.timeout_seconds ?? 600),
  );
  const [isSaving, setIsSaving] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error(t("scheduledTask.nameRequired"));
      return;
    }
    if (!agentId) {
      toast.error(t("scheduledTask.agentRequired"));
      return;
    }

    // Validate JSON
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(inputPayload || "{}");
    } catch {
      setJsonError(t("scheduledTask.invalidJson"));
      return;
    }
    setJsonError(null);

    // Build trigger config
    let triggerConfig: Record<string, unknown>;
    if (triggerType === "interval") {
      triggerConfig = { seconds: Math.max(1, parseInt(intervalSeconds) || 300) };
    } else if (triggerType === "date") {
      if (!runDate) {
        toast.error(t("scheduledTask.runDateRequired"));
        return;
      }
      const date = new Date(runDate);
      if (Number.isNaN(date.getTime())) {
        toast.error(t("scheduledTask.runDateRequired"));
        return;
      }
      triggerConfig = { run_date: date.toISOString() };
    } else {
      triggerConfig = {
        hour: cronHour || "0",
        minute: cronMinute || "0",
        second: cronSecond || "0",
        ...(cronDay ? { day: cronDay } : {}),
        ...(cronMonth ? { month: cronMonth } : {}),
        ...(cronDayOfWeek ? { day_of_week: cronDayOfWeek } : {}),
      };
    }

    setIsSaving(true);
    try {
      const nextPayload = buildScheduledTaskInputPayload(payload, {
        modelId,
        modelValue,
        availableModels,
      });
      await onSave({
        name: name.trim(),
        agent_id: agentId,
        trigger_type: triggerType,
        trigger_config: triggerConfig,
        input_payload: nextPayload,
        description: description.trim() || null,
        enabled,
        run_on_start: triggerType === "date" ? false : runOnStart,
        max_retries: Math.max(0, parseInt(maxRetries) || 0),
        timeout_seconds: Math.max(10, parseInt(timeoutSeconds) || 600),
      });
    } finally {
      setIsSaving(false);
    }
  };

  const inputClass = "scheduled-task-input";

  return (
    <EditorSidebar
      open={true}
      onClose={onClose}
      title={isEdit ? t("scheduledTask.edit") : t("scheduledTask.create")}
      icon={isEdit ? <Pencil size={16} /> : <Plus size={16} />}
      footer={
        <div className="flex gap-3">
          <button
            onClick={onClose}
            className="scheduled-task-button scheduled-task-button--secondary flex-1"
          >
            {t("scheduledTask.cancel")}
          </button>
          <button
            onClick={handleSave}
            disabled={isSaving}
            className="scheduled-task-button scheduled-task-button--primary flex-1"
          >
            {isSaving ? (
              <span className="inline-flex items-center gap-2">
                <svg
                  className="h-4 w-4 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                {t("common.saving") || "Saving..."}
              </span>
            ) : (
              t("scheduledTask.save")
            )}
          </button>
        </div>
      }
    >
      <div className="es-form" style={{ gap: 0 }}>
        <div className="space-y-5">
        {/* Name */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.name")} *
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
            placeholder={t("scheduledTask.namePlaceholder")}
          />
        </div>

        {/* Description */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.description")}
          </label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className={`${inputClass} resize-y`}
            placeholder={t("scheduledTask.descriptionPlaceholder")}
          />
        </div>

        {/* Agent selector */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.agent")} *
          </label>
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            className={inputClass}
          >
            <option value="">{t("scheduledTask.agentPlaceholder")}</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {t(agent.name)}
              </option>
            ))}
          </select>
        </div>

        {/* Model selector */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.model")}
          </label>
          <select
            value={modelId}
            onChange={(e) => {
              const nextModel = availableModels?.find(
                (model) => model.id === e.target.value,
              );
              setModelId(e.target.value);
              setModelValue(nextModel?.value || "");
            }}
            className={inputClass}
            disabled={!availableModels || availableModels.length === 0}
          >
            <option value="">{t("scheduledTask.modelPlaceholder")}</option>
            {(availableModels || []).map((model) => (
              <option key={model.id} value={model.id}>
                {model.label || model.value}
              </option>
            ))}
          </select>
        </div>

        {/* Trigger type */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.triggerType")}
          </label>
          <div className="scheduled-task-segmented">
            {(["date", "interval", "cron"] as const).map((tt) => (
              <button
                key={tt}
                type="button"
                onClick={() => setTriggerType(tt)}
                className={`scheduled-task-segment ${
                  triggerType === tt
                    ? "scheduled-task-segment--active"
                    : ""
                }`}
              >
                {tt === "interval" ? (
                  <Timer size={16} />
                ) : (
                  <CalendarClock size={16} />
                )}
                {t(`scheduledTask.${tt}`)}
              </button>
            ))}
          </div>
        </div>

        {/* Trigger config */}
        {triggerType === "interval" ? (
          <div className="scheduled-task-form-field">
            <label className="scheduled-task-label">
              {t("scheduledTask.intervalSeconds")} *
            </label>
            <input
              type="number"
              min={1}
              value={intervalSeconds}
              onChange={(e) => setIntervalSeconds(e.target.value)}
              className={inputClass}
            />
          </div>
        ) : triggerType === "date" ? (
          <div className="scheduled-task-form-field">
            <label className="scheduled-task-label">
              {t("scheduledTask.runDate")} *
            </label>
            <input
              type="datetime-local"
              value={runDate}
              onChange={(e) => setRunDate(e.target.value)}
              className={inputClass}
            />
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {(
              [
                { key: "cronHour", label: t("scheduledTask.cronHour"), value: cronHour, set: setCronHour },
                { key: "cronMinute", label: t("scheduledTask.cronMinute"), value: cronMinute, set: setCronMinute },
                { key: "cronSecond", label: t("scheduledTask.cronSecond"), value: cronSecond, set: setCronSecond },
                { key: "cronDay", label: t("scheduledTask.cronDay"), value: cronDay, set: setCronDay },
                { key: "cronMonth", label: t("scheduledTask.cronMonth"), value: cronMonth, set: setCronMonth },
                { key: "cronDayOfWeek", label: t("scheduledTask.cronDayOfWeek"), value: cronDayOfWeek, set: setCronDayOfWeek },
              ] as const
            ).map(({ key, label, value, set }) => (
              <div key={key} className="scheduled-task-form-field">
                <label className="scheduled-task-label text-xs">
                  {label}
                </label>
                <input
                  type="text"
                  value={value}
                  onChange={(e) => set(e.target.value)}
                  className={inputClass}
                  placeholder="*"
                />
              </div>
            ))}
          </div>
        )}

        {/* Input payload */}
        <div className="scheduled-task-form-field">
          <label className="scheduled-task-label">
            {t("scheduledTask.inputPayload")}
          </label>
          <textarea
            value={inputPayload}
            onChange={(e) => {
              setInputPayload(e.target.value);
              setJsonError(null);
            }}
            rows={4}
            className={`${inputClass} resize-y font-mono text-xs`}
            placeholder="{}"
          />
          {jsonError && (
            <p className="mt-1 text-xs text-red-500">{jsonError}</p>
          )}
        </div>

        {/* Toggles */}
        <div className="space-y-3">
          {/* Enabled toggle */}
          <div className="scheduled-task-toggle-row">
            <p className="text-sm font-medium text-theme-text-secondary">
              {t("scheduledTask.enabled")}
            </p>
            <button
              type="button"
              onClick={() => setEnabled(!enabled)}
              className={`scheduled-task-toggle ${
                enabled ? "scheduled-task-toggle--active" : ""
              }`}
            >
              <span
                className="scheduled-task-toggle__thumb"
              />
            </button>
          </div>

          {triggerType !== "date" && (
            <div className="scheduled-task-toggle-row">
              <p className="text-sm font-medium text-theme-text-secondary">
                {t("scheduledTask.runOnStart")}
              </p>
              <button
                type="button"
                onClick={() => setRunOnStart(!runOnStart)}
                className={`scheduled-task-toggle ${
                  runOnStart ? "scheduled-task-toggle--active" : ""
                }`}
              >
                <span
                  className="scheduled-task-toggle__thumb"
                />
              </button>
            </div>
          )}
        </div>

        {/* Number inputs */}
        <div className="grid grid-cols-2 gap-4">
          <div className="scheduled-task-form-field">
            <label className="scheduled-task-label">
              {t("scheduledTask.maxRetries")}
            </label>
            <input
              type="number"
              min={0}
              max={10}
              value={maxRetries}
              onChange={(e) => setMaxRetries(e.target.value)}
              className={inputClass}
            />
          </div>
          <div className="scheduled-task-form-field">
            <label className="scheduled-task-label">
              {t("scheduledTask.timeoutSeconds")}
            </label>
            <input
              type="number"
              min={10}
              max={3600}
              value={timeoutSeconds}
              onChange={(e) => setTimeoutSeconds(e.target.value)}
              className={inputClass}
            />
          </div>
        </div>
      </div>
      </div>
    </EditorSidebar>
  );
}