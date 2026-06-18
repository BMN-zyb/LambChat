import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("skills search resets pagination in the input change handler before fetching", () => {
  const file = source("../useSkillsActions.ts");

  assert.match(file, /const handleSearchQueryChange = useCallback/);
  assert.match(
    file,
    /const handleSearchQueryChange = useCallback\(\s*\(query: string\) => \{\s*setPage\(1\);\s*setSearchQuery\(query\);/s,
  );
  assert.match(file, /setSearchQuery:\s*handleSearchQueryChange/);
  assert.doesNotMatch(
    file,
    /useEffect\(\(\) => \{\s*setPage\(1\);\s*\}, \[searchQuery/,
  );
});
