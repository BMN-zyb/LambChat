import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";

function readRepoFile(path: string): string {
  const url = new URL(`../../../../../${path}`, import.meta.url);
  return readFileSync(url, "utf8");
}

test("release workflow publishes branded desktop and mobile artifacts", () => {
  const workflowPath = ".github/workflows/app-release.yml";
  assert.equal(
    existsSync(new URL(`../../../../../${workflowPath}`, import.meta.url)),
    true,
  );

  const workflow = readRepoFile(workflowPath);
  assert.match(workflow, /LambChat-/);
  assert.match(workflow, /package:desktop/);
  assert.match(workflow, /assembleRelease/);
  assert.match(workflow, /softprops\/action-gh-release/);
  assert.match(workflow, /java-version: '21'/);
  assert.match(workflow, /if: always\(\)/);
  assert.match(workflow, /continue-on-error: true/);
  assert.match(workflow, /-workspace App\.xcworkspace/);
  assert.match(workflow, /CARGO_BUILD_JOBS/);
  assert.match(workflow, /cargo_target_dir: \/tmp\/lambchat-cargo-target/);
  assert.match(workflow, /cargo_target_dir: D:\\cargo-target/);
  assert.match(
    workflow,
    /CARGO_TARGET_DIR: \$\{\{ matrix\.cargo_target_dir \}\}/,
  );
  assert.doesNotMatch(workflow, /CARGO_TARGET_DIR:.*\|\| ''/);
  assert.match(workflow, /timeout-minutes: 45/);
  assert.match(workflow, /runner: windows-2022/);
  assert.doesNotMatch(workflow, /mapfile/);
});

test("release workflow publishes a debug Android APK when signing secrets are missing", () => {
  const workflow = readRepoFile(".github/workflows/app-release.yml");

  assert.doesNotMatch(workflow, /LambChat-android-[^\n]*release-unsigned\.apk/);
  assert.doesNotMatch(workflow, /unsigned-xcarchive/);
  assert.match(workflow, /assembleDebug/);
  assert.match(workflow, /app-debug\.apk/);
  assert.match(workflow, /LambChat-android-\$\{RELEASE_TAG\}-debug\.apk/);
  assert.match(workflow, /LambChat-android-\$\{RELEASE_TAG\}-signed\.apk/);
});

test("mobile package scripts generate and validate branded native images", () => {
  const packageJson = JSON.parse(readRepoFile("frontend/package.json")) as {
    scripts: Record<string, string>;
  };
  const assetScript = readRepoFile(
    "frontend/scripts/generate-branded-assets.mjs",
  );

  assert.match(packageJson.scripts["mobile:sync"], /brand:assets/);
  assert.match(packageJson.scripts["mobile:build"], /brand:assets/);
  assert.match(packageJson.scripts["brand:assets"], /generate-branded-assets/);
  assert.match(packageJson.scripts["brand:assets:check"], /--check/);
  assert.match(assetScript, /LambChat/);
  assert.match(assetScript, /public\/icons\/icon-512\.png/);
  assert.match(assetScript, /scalePngNearest/);
  assert.match(assetScript, /1024/);
});
