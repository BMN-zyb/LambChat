import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const chatViewSource = readFileSync(
  resolve(
    process.cwd(),
    "src",
    "components",
    "layout",
    "AppContent",
    "ChatView.tsx",
  ),
  "utf8",
);

test("drives the Virtuoso session key through state so session switches remount the message list", () => {
  assert.match(chatViewSource, /setMessageListSessionKey/);
  assert.match(chatViewSource, /key=\{messageListSessionKey\}/);
  assert.doesNotMatch(
    chatViewSource,
    /key=\{messageListSessionKeyRef\.current\}/,
  );
});

test("passes the message list session key into the scroll hook as a bottom-lock token", () => {
  assert.match(
    chatViewSource,
    /useMessageScroll\([\s\S]*isLoadingHistory,\s*messageListSessionKey,\s*\)/,
  );
});
