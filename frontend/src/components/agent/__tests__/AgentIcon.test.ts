import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../AgentIcon.tsx", import.meta.url),
  "utf8",
);

test("renders the default bot icon as the fluent 3d robot image", () => {
  assert.match(source, /const DEFAULT_AGENT_ICON_EMOJI = "🤖"/);
  assert.match(
    source,
    /name=\{isDefaultBotIcon\(icon\) \? DEFAULT_AGENT_ICON_EMOJI : icon\}/,
  );
});
