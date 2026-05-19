import type { Options as DocxPreviewOptions } from "docx-preview";

type MammothHtmlResult = {
  value: string;
};

export type DocxPreviewRenderAsync = (
  data: ArrayBuffer,
  bodyContainer: HTMLElement,
  styleContainer?: HTMLElement,
  options?: Partial<DocxPreviewOptions>,
) => Promise<unknown>;

export type MammothConvertToHtml = (
  input: { arrayBuffer: ArrayBuffer },
  options?: Record<string, unknown>,
) => Promise<MammothHtmlResult>;

export interface RenderDocxPreviewHtmlInput {
  arrayBuffer: ArrayBuffer;
  container: HTMLElement;
  styleContainer?: HTMLElement;
  renderAsync: DocxPreviewRenderAsync;
  convertToHtml: MammothConvertToHtml;
  onDocxPreviewError?: (error: unknown) => void;
}

export type WordPreviewRenderResult =
  | { kind: "docx-preview" }
  | { kind: "html"; html: string };

const mammothStyleMap = [
  "p[style-name='Heading 1'] => h1:fresh",
  "p[style-name='Heading 2'] => h2:fresh",
  "p[style-name='Heading 3'] => h3:fresh",
  "p[style-name='Heading 4'] => h4:fresh",
  "b => strong",
  "i => em",
  "u => u",
];

export function createWordPreviewRendererOptions(): Partial<DocxPreviewOptions> {
  return {
    className: "docx",
    inWrapper: false,
    ignoreWidth: true,
    ignoreHeight: false,
    ignoreFonts: false,
    breakPages: true,
    renderHeaders: true,
    renderFooters: true,
    renderFootnotes: true,
    renderEndnotes: true,
    renderComments: false,
    renderChanges: false,
    renderAltChunks: false,
    useBase64URL: true,
  };
}

export async function renderDocxPreviewHtml({
  arrayBuffer,
  container,
  styleContainer,
  renderAsync,
  convertToHtml,
  onDocxPreviewError = (error) => {
    console.warn("docx-preview failed, falling back to mammoth:", error);
  },
}: RenderDocxPreviewHtmlInput): Promise<WordPreviewRenderResult> {
  container.innerHTML = "";

  try {
    await renderAsync(
      arrayBuffer,
      container,
      styleContainer,
      createWordPreviewRendererOptions(),
    );
    if (container.textContent?.trim() || container.innerHTML.trim()) {
      return { kind: "docx-preview" };
    }
  } catch (error) {
    onDocxPreviewError(error);
  }

  container.innerHTML = "";
  const result = await convertToHtml(
    { arrayBuffer },
    {
      styleMap: mammothStyleMap,
    },
  );
  return { kind: "html", html: result.value };
}
