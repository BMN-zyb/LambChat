import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const panelSource = source("../index.tsx");

test("scheduled task header uses shared panel action styling", () => {
  assert.match(panelSource, /panel-filter-trigger/);
  assert.match(panelSource, /data-filter-menu/);
  assert.match(panelSource, /panel-filter-menu/);
  assert.match(panelSource, /scheduledTask\.allStatuses/);
  assert.match(panelSource, /scheduledTask\.create/);
  assert.doesNotMatch(
    panelSource,
    /<select[\s\S]*?className="scheduled-task-input min-h-10 px-3 py-0"/,
  );
  assert.doesNotMatch(
    panelSource,
    /className="scheduled-task-button scheduled-task-button--primary"/,
  );
});
