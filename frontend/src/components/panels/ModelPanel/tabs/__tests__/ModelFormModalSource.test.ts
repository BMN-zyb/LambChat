import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../ModelFormModal.tsx", import.meta.url),
  "utf8",
);

test("model form persists the supports vision profile flag", () => {
  assert.match(source, /formSupportsVision/);
  assert.match(source, /model\?\.profile\?\.supports_vision/);
  assert.match(source, /supports_vision:\s*formSupportsVision/);
  assert.match(source, /max_input_tokens:\s*maxInputTokens/);
});

test("model form persists the image URL base64 profile flag", () => {
  assert.match(source, /formImageUrlToBase64/);
  assert.match(source, /model\?\.profile\?\.image_url_to_base64/);
  assert.match(source, /image_url_to_base64:\s*formImageUrlToBase64/);
});

test("model form persists an explicit model icon selection", () => {
  assert.match(source, /formIcon/);
  assert.match(source, /model\?\.icon/);
  assert.match(source, /icon:\s*formIcon\s*\|\|\s*undefined/);
  assert.match(source, /<ModelIconSelect/);
});
