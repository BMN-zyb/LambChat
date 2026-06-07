import type { AvailableModel } from "../../contexts/SettingsContext";

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
  delete next._resolved_fallback_model;
  delete next._resolved_model_profile;
  return next;
}

export function buildScheduledTaskInputPayload(
  payload: Record<string, unknown>,
  {
    modelId,
    modelValue,
    availableModels,
  }: {
    modelId: string;
    modelValue: string;
    availableModels: AvailableModel[] | null;
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
  if (Object.keys(nextAgentOptions).length > 0) {
    nextPayload.agent_options = nextAgentOptions;
  }
  return nextPayload;
}
