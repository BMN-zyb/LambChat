import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../SubagentBlocks.tsx", import.meta.url),
  "utf8",
);

test("subagent panel lets users pause auto-follow and jump back to the latest content", () => {
  assert.match(source, /const \[showScrollToBottom, setShowScrollToBottom\]/);
  assert.match(
    source,
    /userScrolledUpRef\.current = !isNearSubagentPanelBottom/,
  );
  assert.match(source, /setShowScrollToBottom\(userScrolledUpRef\.current\)/);
  assert.match(source, /const handleJumpToBottom = useCallback/);
  assert.match(source, /userScrolledUpRef\.current = false/);
  assert.match(source, /t\("common\.scrollToBottom"\)/);
});
