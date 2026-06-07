import type { TFunction } from "i18next";
import type { ScheduledTask } from "../../../types/scheduledTask";
import type { SSEEventRecord } from "../../../types/session";
import { formatDateTimeShort } from "../../../utils/datetime";
import type { RunConversationMessage, ScheduledTaskDefaults } from "./types";

const SCHEDULED_TASK_DEFAULTS_KEY = "lambchat_scheduled_task_defaults";

export function readScheduledTaskDefaults(): ScheduledTaskDefaults {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(SCHEDULED_TASK_DEFAULTS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as ScheduledTaskDefaults;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function extractRunConversationMessages(
  events: SSEEventRecord[],
): RunConversationMessage[] {
  const messages: RunConversationMessage[] = [];

  for (const event of events) {
    if (event.event_type === "user:message") {
      const content = event.data?.content as string | undefined;
      if (content) {
        messages.push({
          role: "user",
          content,
          timestamp: event.timestamp,
        });
      }
      continue;
    }

    if (event.event_type === "assistant:text") {
      const content = event.data?.content as string | undefined;
      if (!content) continue;

      const last = messages[messages.length - 1];
      if (last?.role === "assistant" && last.timestamp === event.timestamp) {
        last.content += content;
      } else {
        messages.push({
          role: "assistant",
          content,
          timestamp: event.timestamp,
        });
      }
    }
  }

  return messages;
}

export function toDateTimeLocalValue(value: string | null | undefined): string {
  const date = value ? new Date(value) : new Date(Date.now() + 5 * 60 * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const offsetMs = date.getTimezoneOffset() * 60 * 1000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

export function formatTaskTrigger(task: ScheduledTask, t: TFunction): string {
  if (task.trigger_type === "interval") {
    const cfg = task.trigger_config as { seconds?: number };
    return `${t("scheduledTask.interval")}: ${cfg.seconds ?? "-"}s`;
  }

  if (task.trigger_type === "date") {
    const cfg = task.trigger_config as { run_date?: string };
    return `${t("scheduledTask.date")}: ${
      cfg.run_date ? formatDateTimeShort(cfg.run_date) : "-"
    }`;
  }

  const cfg = task.trigger_config as {
    hour?: string;
    minute?: string;
    day?: string;
    month?: string;
    day_of_week?: string;
  };
  return `${t("scheduledTask.cron")}: ${[
    cfg.minute ?? "*",
    cfg.hour ?? "*",
    cfg.day ?? "*",
    cfg.month ?? "*",
    cfg.day_of_week ?? "*",
  ].join(" ")}`;
}
