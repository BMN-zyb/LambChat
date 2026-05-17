import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const panelSource = readFileSync(
  new URL("../FeishuPanel.tsx", import.meta.url),
  "utf8",
);
const formSource = readFileSync(
  new URL("../FeishuPanelForm.tsx", import.meta.url),
  "utf8",
);
const channelTypesSource = readFileSync(
  new URL("../../../../../types/channel.ts", import.meta.url),
  "utf8",
);

test("registration polling cleanup cancels active server-side session", () => {
  assert.match(panelSource, /cancelFeishuRegistration/);
  assert.match(
    panelSource,
    /channelApi\s*\.\s*cancelFeishuRegistration\(\s*registrationSessionId\s*\)/,
  );
  assert.match(panelSource, /return\s+\(\)\s*=>\s*\{/);
});

test("feishu channel form wires persona preset selection through save payloads", () => {
  assert.match(formSource, /ChannelPersonaSelect/);
  assert.match(formSource, /personaPresetId/);
  assert.match(
    panelSource,
    /const\s+\[personaPresetId,\s*setPersonaPresetId\]/,
  );
  assert.match(
    panelSource,
    /setPersonaPresetId\(initialConfig\.persona_preset_id \|\| null\)/,
  );
  assert.match(panelSource, /persona_preset_id:\s*personaPresetId/);
  assert.match(channelTypesSource, /persona_preset_id\?: string \| null/);
});
