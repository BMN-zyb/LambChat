import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

function readSource(relativePath: string): string {
  return readFileSync(resolve(__dirname, "..", relativePath), "utf8");
}

test("chat skill selector receives session-effective skills and counts", () => {
  const source = readSource("ChatAppContent.tsx");

  assert.match(source, /const effectiveSkills = useMemo\(/);
  assert.match(source, /countEnabledSkills\(effectiveSkills\)/);
  assert.match(source, /skills=\{effectiveSkills\}/);
  assert.match(source, /enabledSkillsCount=\{effectiveEnabledSkillsCount\}/);
  assert.match(source, /totalSkillsCount=\{effectiveSkills\.length\}/);
  assert.doesNotMatch(source, /enabledSkillsCount=\{totalEnabledSkillCount\}/);
});
