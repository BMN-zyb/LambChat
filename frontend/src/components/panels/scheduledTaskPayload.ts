import type { AvailableModel } from "../../contexts/SettingsContext";
import type { FileCategory, MessageAttachment } from "../../types";

const FILE_CATEGORIES = new Set<FileCategory>([
  "image",
  "video",
  "audio",
  "document",
]);

function isFileCategory(value: unknown): value is FileCategory {
  return (
    typeof value === "string" && FILE_CATEGORIES.has(value as FileCategory)
  );
}

function getString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function getFiniteSize(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

export function getAgentOptionsFromScheduledTaskPayload(
  payload: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const options = payload?.agent_options;
  return options && typeof options === "object" && !Array.isArray(options)
    ? (options as Record<string, unknown>)
    : {};
}

export function withoutScheduledTaskModelOptions(
  options: Record<string, unknown>,
): Record<string, unknown> {
  const next = { ...options };
  delete next.model_id;
  delete next.model;
  delete next._resolved_model_config;
  delete next._resolved_supports_vision;
  delete next._resolved_image_url_to_base64;
  delete next._resolved_fallback_model;
  delete next._resolved_model_profile;
  return next;
}

export function getScheduledTaskPersonaPresetId(
  payload: Record<string, unknown> | undefined,
): string {
  const value = payload?.persona_preset_id;
  return typeof value === "string" ? value : "";
}

export function getScheduledTaskTeamId(
  payload: Record<string, unknown> | undefined,
): string {
  const value = payload?.team_id;
  return typeof value === "string" ? value : "";
}

export function getScheduledTaskAttachments(
  payload: Record<string, unknown> | undefined,
): MessageAttachment[] {
  const rawAttachments = payload?.attachments;
  if (!Array.isArray(rawAttachments)) return [];

  return rawAttachments.flatMap((item): MessageAttachment[] => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const record = item as Record<string, unknown>;
    const key = getString(record.key);
    const name = getString(record.name);
    const type = record.type;
    const mimeType = getString(record.mimeType);
    const size = getFiniteSize(record.size);
    if (!key || !name || !isFileCategory(type) || !mimeType || size === null) {
      return [];
    }

    return [
      {
        id: getString(record.id) || key,
        key,
        name,
        type,
        mimeType,
        size,
        ...(getString(record.url) ? { url: getString(record.url) } : {}),
      },
    ];
  });
}

export function withScheduledTaskAttachments(
  payload: Record<string, unknown>,
  attachments: MessageAttachment[],
): Record<string, unknown> {
  const nextPayload = { ...payload };
  const uploadedAttachments = getScheduledTaskAttachments({ attachments });
  if (uploadedAttachments.length > 0) {
    nextPayload.attachments = uploadedAttachments;
  } else {
    delete nextPayload.attachments;
  }
  return nextPayload;
}

export function buildScheduledTaskInputPayload(
  payload: Record<string, unknown>,
  {
    agentId,
    modelId,
    modelValue,
    availableModels,
    personaPresetId = "",
    teamId = "",
  }: {
    agentId: string;
    modelId: string;
    modelValue: string;
    availableModels: AvailableModel[] | null;
    personaPresetId?: string;
    teamId?: string;
  },
): Record<string, unknown> {
  const selectedModel = availableModels?.find((model) => model.id === modelId);
  const nextAgentOptions = {
    ...withoutScheduledTaskModelOptions(
      getAgentOptionsFromScheduledTaskPayload(payload),
    ),
    ...(modelId ? { model_id: modelId } : {}),
    ...(selectedModel?.value || modelValue
      ? { model: selectedModel?.value || modelValue }
      : {}),
  };
  const nextPayload = { ...payload };
  delete nextPayload.agent_options;
  delete nextPayload.persona_preset_id;
  delete nextPayload.team_id;
  if (Object.keys(nextAgentOptions).length > 0) {
    nextPayload.agent_options = nextAgentOptions;
  }
  if (agentId === "team") {
    if (teamId) nextPayload.team_id = teamId;
  } else if (personaPresetId) {
    nextPayload.persona_preset_id = personaPresetId;
  }
  return nextPayload;
}
