import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../ExcalidrawPreview.tsx", import.meta.url),
  "utf8",
);

test("Excalidraw preview captures wheel zoom locally instead of letting the page zoom", () => {
  assert.match(source, /handleWheel/);
  assert.match(source, /event\.(?:ctrlKey|metaKey)/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /onWheel=\{handleWheel\}/);
});
