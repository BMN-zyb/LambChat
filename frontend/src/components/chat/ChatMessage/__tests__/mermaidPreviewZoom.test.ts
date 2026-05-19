import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../MermaidDiagram.tsx", import.meta.url),
  "utf8",
);

test("Mermaid preview captures wheel zoom locally instead of letting the page zoom", () => {
  const handleWheelMatches = source.match(/const handleWheel = useCallback/g);
  assert.equal(handleWheelMatches?.length, 2);
  assert.match(source, /event\.(?:ctrlKey|metaKey)/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /onWheel=\{handleWheel\}/);
});
