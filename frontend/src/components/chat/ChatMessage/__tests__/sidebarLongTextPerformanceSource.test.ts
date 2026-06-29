import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../SubagentBlocks.tsx", import.meta.url),
  "utf8",
);

test("thinking and subagent sidebars use a lightweight renderer for long or streaming text", () => {
  assert.match(source, /function SidebarMarkdownContent/);
  assert.match(source, /SIDEBAR_MARKDOWN_PREVIEW_LIMIT/);
  assert.match(source, /SUBAGENT_PARTS_PREVIEW_LIMIT/);
  assert.match(source, /const shouldUsePreview =/);
  assert.match(source, /const shouldUsePartsPreview =/);
  assert.match(source, /max-w-prose/);
  assert.match(source, /from-theme-bg-card/);
  assert.doesNotMatch(source, /<pre className=/);

  assert.match(source, /<SidebarMarkdownContent\s+content=\{content\}/);
  assert.match(source, /<SidebarMarkdownContent\s+content=\{data\.input\}/);
  assert.match(source, /<SidebarMarkdownContent\s+content=\{data\.result\}/);
  assert.match(source, /<SidebarMarkdownContent\s+content=\{partsText\}/);
});
