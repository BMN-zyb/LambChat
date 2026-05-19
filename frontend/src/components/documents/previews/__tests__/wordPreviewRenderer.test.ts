import assert from "node:assert/strict";
import test from "node:test";

import {
  createWordPreviewRendererOptions,
  renderDocxPreviewHtml,
} from "../wordPreviewRenderer.ts";

test("disables altChunk rendering by default for DOCX previews", () => {
  const options = createWordPreviewRendererOptions();

  assert.equal(options.renderAltChunks, false);
});

test("renders with docx-preview before using mammoth fallback", async () => {
  const calls: string[] = [];
  const container = {
    innerHTML: "",
    textContent: "",
  } as unknown as HTMLElement;
  const styleContainer = {} as HTMLElement;
  const arrayBuffer = new ArrayBuffer(8);

  const result = await renderDocxPreviewHtml({
    arrayBuffer,
    container,
    styleContainer,
    renderAsync: async (input, output, styles, options) => {
      calls.push("docx-preview");
      assert.equal(input, arrayBuffer);
      assert.equal(output, container);
      assert.equal(styles, styleContainer);
      assert.ok(options);
      assert.equal(options.renderAltChunks, false);
      output.innerHTML = "<section>Rendered DOCX</section>";
    },
    convertToHtml: async () => {
      calls.push("mammoth");
      return { value: "<p>Fallback</p>" };
    },
  });

  assert.deepEqual(calls, ["docx-preview"]);
  assert.deepEqual(result, { kind: "docx-preview" });
  assert.equal(container.innerHTML, "<section>Rendered DOCX</section>");
});

test("falls back to mammoth when docx-preview cannot render", async () => {
  const calls: string[] = [];
  const container = {
    innerHTML: "<section>stale</section>",
    textContent: "stale",
  } as unknown as HTMLElement;
  const styleContainer = {} as HTMLElement;

  const result = await renderDocxPreviewHtml({
    arrayBuffer: new ArrayBuffer(8),
    container,
    styleContainer,
    renderAsync: async () => {
      calls.push("docx-preview");
      throw new Error("docx-preview failed");
    },
    convertToHtml: async () => {
      calls.push("mammoth");
      return { value: "<p>Fallback</p>" };
    },
    onDocxPreviewError: () => undefined,
  });

  assert.deepEqual(calls, ["docx-preview", "mammoth"]);
  assert.deepEqual(result, { kind: "html", html: "<p>Fallback</p>" });
  assert.equal(container.innerHTML, "");
});
