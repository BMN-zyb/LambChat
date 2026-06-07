import assert from "node:assert/strict";
import test from "node:test";

import { buildScheduledTaskInputPayload } from "../scheduledTaskPayload.ts";

test("clearing the model removes stale scheduled task agent options", () => {
  assert.deepEqual(
    buildScheduledTaskInputPayload(
      {
        message: "run",
        agent_options: {
          model_id: "old-model-id",
          model: "old-model",
          _resolved_model_config: { id: "old-model-id" },
        },
      },
      {
        modelId: "",
        modelValue: "",
        availableModels: null,
      },
    ),
    {
      message: "run",
    },
  );
});

test("clearing the model preserves non-model agent options", () => {
  assert.deepEqual(
    buildScheduledTaskInputPayload(
      {
        message: "run",
        agent_options: {
          model_id: "old-model-id",
          temperature: 0.2,
        },
      },
      {
        modelId: "",
        modelValue: "",
        availableModels: null,
      },
    ),
    {
      message: "run",
      agent_options: {
        temperature: 0.2,
      },
    },
  );
});
