import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const __dirname = dirname(fileURLToPath(import.meta.url));

function readSource(relativePath: string): string {
  return readFileSync(resolve(__dirname, relativePath), "utf8");
}

const argsBlockConsumers = [
  "../EditFileItem.tsx",
  "../GlobItem.tsx",
  "../GrepItem.tsx",
  "../LsItem.tsx",
  "../ReadFileItem.tsx",
  "../WriteFileItem.tsx",
];

test("tool argument blocks share detail and compact wrappers", () => {
  const source = readSource("../ToolArgsBlock.tsx");

  expect(source).toMatch(/type ToolArgsBlockSize = "detail" \| "compact"/);
  expect(source).toMatch(
    /detail:\s*"tool-args-block group\/args relative flex items-center gap-2 px-3 py-2 rounded-lg bg-theme-bg-subtle text-sm text-theme-text-tertiary font-mono"/,
  );
  expect(source).toMatch(
    /compact:\s*"tool-args-block group\/args relative flex items-center gap-2 mb-2 px-2 py-1\.5 rounded-md bg-theme-bg-subtle text-xs text-theme-text-tertiary font-mono"/,
  );
  expect(source).toMatch(/wrap \? "flex-wrap" : ""/);

  for (const relativePath of argsBlockConsumers) {
    const consumer = readSource(relativePath);

    expect(consumer).toMatch(
      /import \{ ToolArgsBlock \} from "\.\/ToolArgsBlock"/,
    );
    expect(consumer).toMatch(/<ToolArgsBlock size="detail"/);
    expect(consumer).toMatch(/<ToolArgsBlock size="compact"/);
    expect(consumer).not.toMatch(
      /group\/args relative flex items-center gap-2 (?:mb-2 )?px-(?:3 py-2 rounded-lg|2 py-1\.5 rounded-md) bg-theme-bg-subtle text-(?:sm|xs) text-theme-text-tertiary font-mono/,
    );
  }
});
