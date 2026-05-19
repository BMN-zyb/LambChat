import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../ImageViewer.tsx", import.meta.url),
  "utf8",
);

test("ImageViewer captures wheel zoom locally instead of letting the page zoom", () => {
  assert.match(source, /addEventListener\("wheel",\s*handleNativeWheel/);
  assert.match(source, /passive:\s*false/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /setScale/);
});
