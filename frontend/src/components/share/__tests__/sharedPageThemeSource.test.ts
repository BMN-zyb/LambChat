import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const sharedPageSource = readFileSync(
  join(import.meta.dirname, "../SharedPage.tsx"),
  "utf8",
);

test("shared page top-level surfaces use theme tokens for light and dark modes", () => {
  assert.match(sharedPageSource, /bg-theme-bg text-theme-text min-h-dvh/);
  assert.match(
    sharedPageSource,
    /min-h-dvh font-sans border-r border-theme-border/,
  );
  assert.match(
    sharedPageSource,
    /max-w-6xl mx-auto px-4 sm:px-8 h-14 flex items-center justify-between border-r border-theme-border/,
  );
  assert.match(sharedPageSource, /border-b border-theme-border/);
  assert.match(sharedPageSource, /bg-theme-bg-card rounded-2xl/);
  assert.doesNotMatch(sharedPageSource, /bg-\[#faf9f7\]/);
  assert.doesNotMatch(sharedPageSource, /dark:bg-\[#0f0e0d\]/);
});
