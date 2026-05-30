import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileText,
  Image as ImageIcon,
  File,
} from "lucide-react";
import { useState, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import { MarkdownContent } from "../MarkdownContent";
import { CopyButton, ImageViewer } from "../../../common";
import { dispatchPersonaPresetsChanged } from "../../../../hooks/personaPresetEvents";
import { getPersonaPresetMutationDetail } from "./personaPresetToolResult";
import type { McpContentBlock, McpMultiModalResult } from "./toolUtils";
import { isMarkdownText, extractText } from "./toolUtils";
import { ToolResultPanel } from "./ToolResultPanel";
import {
  extractGeneratedImageResults,
  type GeneratedImageResult,
} from "./toolImageResults";
import { ImageWithSkeleton } from "../ImageWithSkeleton";
import {
  closeBlockPreview,
  getBlockPreview,
  openBlockPreview,
  subscribeBlockPreview,
} from "./blockPreviewStore";

function useBlockPreview() {
  const [, setCount] = useState(0);
  useEffect(() => {
    const fn = () => setCount((c) => c + 1);
    return subscribeBlockPreview(fn);
  }, []);
  return { preview: getBlockPreview(), close: closeBlockPreview };
}

/** Standalone portal — render once at app level, survives any component tree changes */
export function BlockPreviewPortal() {
  const { t } = useTranslation();
  const { preview, close } = useBlockPreview();

  if (!preview) return null;

  let icon: React.ReactNode;
  let title: string;
  let content: React.ReactNode;

  if (preview.type === "image" && preview.src) {
    icon = <ImageIcon size={16} />;
    title = t("chat.message.toolOutput");
    content = (
      <div className="flex items-center justify-center p-4 bg-stone-50 dark:bg-stone-900 min-h-[200px]">
        <ImageWithSkeleton
          src={preview.src}
          alt={t("chat.message.toolOutput")}
          skipUrlResolve
          className="max-w-full max-h-[70dvh] object-contain rounded-lg"
          wrapperClassName="!my-0"
        />
      </div>
    );
  } else if (preview.type === "file" && preview.url) {
    icon = <File size={16} />;
    title = preview.fileName || t("chat.message.toolFile");
    content = (
      <div className="p-4 sm:p-5 space-y-3">
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-stone-100 dark:bg-stone-800 text-sm text-stone-500 dark:text-stone-400 font-mono truncate">
          <span className="truncate">{preview.url}</span>
        </div>
        <a
          href={preview.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-stone-100 dark:bg-stone-800 text-sm text-stone-600 dark:text-stone-300 hover:bg-stone-200 dark:hover:bg-stone-700 transition-colors border border-stone-200 dark:border-stone-700"
        >
          <ExternalLink size={14} />
          {t("chat.message.toolOpenFile", "Open file")}
        </a>
      </div>
    );
  } else if (preview.type === "text" && preview.text) {
    icon = <FileText size={16} />;
    title = t("chat.message.toolOutput");
    content = (
      <div className="p-4 sm:p-5">
        <div className="flex justify-end mb-2">
          <CopyButton text={preview.text} />
        </div>
        <pre className="text-sm text-stone-700 dark:text-stone-300 whitespace-pre-wrap break-words font-mono">
          {preview.text}
        </pre>
      </div>
    );
  } else {
    return null;
  }

  return createPortal(
    <ToolResultPanel
      open
      onClose={close}
      title={title}
      icon={icon}
      status="success"
    >
      {content}
    </ToolResultPanel>,
    document.body,
  );
}

// LangChain content blocks 数组: [{"type": "text", "text": "..."}, ...]
function isContentBlocksArray(result: unknown): result is McpContentBlock[] {
  return (
    Array.isArray(result) &&
    result.length > 0 &&
    typeof result[0] === "object" &&
    result[0] !== null &&
    "type" in result[0]
  );
}

// 单个 MCP content block 的预览
export function McpBlockPreview({ block }: { block: McpContentBlock }) {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);

  if (block.type === "image") {
    const src = block.base64
      ? `data:${block.mime_type || "image/png"};base64,${block.base64}`
      : block.url || "";
    return (
      <>
        {!loaded && (
          <div className="w-48 h-32 rounded-md border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 animate-pulse" />
        )}
        <img
          src={src}
          alt={t("chat.message.toolOutput")}
          className={`max-w-full max-h-48 rounded-md border border-stone-200 dark:border-stone-700 cursor-pointer hover:opacity-80 transition-opacity${
            !loaded ? " hidden" : ""
          }`}
          onClick={() => {
            if (src) openBlockPreview({ type: "image", src });
          }}
          onLoad={() => setLoaded(true)}
        />
      </>
    );
  }

  if (block.type === "file") {
    const url = block.url || "";
    const fileName = url.split("/").pop() || t("chat.message.toolFile");
    return (
      <button
        onClick={() => openBlockPreview({ type: "file", url, fileName })}
        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-stone-100 dark:bg-stone-800 text-xs text-stone-600 dark:text-stone-300 hover:bg-stone-200 dark:hover:bg-stone-700 transition-colors border border-stone-200 dark:border-stone-700 cursor-pointer"
      >
        <File size={12} />
        {fileName}
      </button>
    );
  }

  if (block.text) {
    return (
      <div className="group/pre relative">
        <pre
          onClick={() => openBlockPreview({ type: "text", text: block.text })}
          className="text-xs text-stone-600 dark:text-stone-300 whitespace-pre-wrap break-words overflow-y-auto min-w-0 cursor-pointer hover:text-stone-900 dark:hover:text-stone-100 transition-colors"
        >
          {block.text}
        </pre>
        <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/pre:opacity-100 transition-opacity">
          <CopyButton text={block.text} size={12} />
        </div>
      </div>
    );
  }

  return null;
}

function GeneratedImageResults({ images }: { images: GeneratedImageResult[] }) {
  const { t } = useTranslation();
  const [activeImage, setActiveImage] = useState<GeneratedImageResult | null>(
    null,
  );
  const activeImageIndex = activeImage
    ? images.findIndex((image) => image.url === activeImage.url)
    : -1;
  const previousImage =
    activeImageIndex > 0 ? images[activeImageIndex - 1] : null;
  const nextImage =
    activeImageIndex >= 0 && activeImageIndex < images.length - 1
      ? images[activeImageIndex + 1]
      : null;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3">
        {images.map((image) => (
          <figure
            key={image.url}
            className="overflow-hidden rounded-md border border-stone-200 bg-stone-50 dark:border-stone-700 dark:bg-stone-900"
          >
            <button
              type="button"
              className="block w-full bg-stone-100 dark:bg-stone-950"
              onClick={() => setActiveImage(image)}
              aria-label={t("chat.message.openImage", "Open image")}
            >
              <ImageWithSkeleton
                src={image.url}
                alt={image.name}
                skipUrlResolve
                className="mx-auto max-h-[70dvh] w-full object-contain"
                loading="lazy"
                wrapperClassName="!my-0 !shadow-none"
              />
            </button>
            <figcaption className="flex items-center gap-2 border-t border-stone-200 px-3 py-2 text-xs text-stone-600 dark:border-stone-700 dark:text-stone-300">
              <ImageIcon
                size={14}
                className="shrink-0 text-stone-500 dark:text-stone-400"
              />
              <span className="min-w-0 flex-1 truncate">{image.name}</span>
              <a
                href={image.url}
                target="_blank"
                rel="noopener noreferrer"
                className="shrink-0 rounded-md p-1 text-stone-500 transition-colors hover:bg-stone-200 hover:text-stone-800 dark:text-stone-400 dark:hover:bg-stone-800 dark:hover:text-stone-100"
                aria-label={t("chat.message.openImage", "Open image")}
              >
                <ExternalLink size={14} />
              </a>
            </figcaption>
          </figure>
        ))}
      </div>
      {activeImage && (
        <ImageViewer
          isOpen
          src={activeImage.url}
          alt={activeImage.name}
          onClose={() => setActiveImage(null)}
          onPrevious={() => previousImage && setActiveImage(previousImage)}
          onNext={() => nextImage && setActiveImage(nextImage)}
          hasPrevious={!!previousImage}
          hasNext={!!nextImage}
          positionLabel={
            activeImageIndex >= 0
              ? `${activeImageIndex + 1} / ${images.length}`
              : undefined
          }
        />
      )}
    </div>
  );
}

// 工具结果渲染组件 — 支持 str / dict / MCP 多模态
export function ToolResultContent({
  result,
  hideCopyButton,
}: {
  result?: string | Record<string, unknown>;
  hideCopyButton?: boolean;
}) {
  const personaMutationDetail = useMemo(
    () => getPersonaPresetMutationDetail(result),
    [result],
  );
  const lastPersonaMutationKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!personaMutationDetail) return;
    const key = JSON.stringify(personaMutationDetail);
    if (lastPersonaMutationKeyRef.current === key) return;
    lastPersonaMutationKeyRef.current = key;
    dispatchPersonaPresetsChanged(personaMutationDetail);
  }, [personaMutationDetail]);

  const textContent = extractText(result);
  const generatedImages = useMemo(() => {
    const directImages = extractGeneratedImageResults(result);
    if (directImages.length > 0) return directImages;

    if (typeof result !== "string") return [];
    try {
      return extractGeneratedImageResults(JSON.parse(result));
    } catch {
      return [];
    }
  }, [result]);

  // LangChain content blocks 数组: [{"type": "text", "text": "..."}, ...]
  if (isContentBlocksArray(result)) {
    const blocks = result as McpContentBlock[];
    const textParts: string[] = [];
    const mediaBlocks: McpContentBlock[] = [];

    for (const block of blocks) {
      if (block.type === "text" && block.text) {
        textParts.push(block.text);
      } else if (block.type === "image" || block.type === "file") {
        mediaBlocks.push(block);
      }
    }

    const combinedText = textParts.join("\n");
    return (
      <div className="space-y-1.5">
        {combinedText && (
          <div className="group/result relative text-xs text-stone-600 dark:text-stone-300 overflow-y-auto">
            {isMarkdownText(combinedText) ? (
              <MarkdownContent content={combinedText} />
            ) : (
              combinedText
            )}
            <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/result:opacity-100 transition-opacity">
              {!hideCopyButton && <CopyButton text={combinedText} size={12} />}
            </div>
          </div>
        )}
        {mediaBlocks.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {mediaBlocks.map((block, i) => (
              <McpBlockPreview key={i} block={block} />
            ))}
          </div>
        )}
      </div>
    );
  }

  if (
    typeof result === "object" &&
    result !== null &&
    "blocks" in result &&
    Array.isArray((result as McpMultiModalResult).blocks)
  ) {
    const mcp = result as McpMultiModalResult;
    return (
      <div className="space-y-1.5">
        {mcp.text &&
          (isMarkdownText(mcp.text) ? (
            <div className="group/result relative text-xs text-stone-600 dark:text-stone-300 overflow-y-auto">
              <MarkdownContent content={mcp.text} />
              <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/result:opacity-100 transition-opacity">
                {!hideCopyButton && <CopyButton text={mcp.text} size={12} />}
              </div>
            </div>
          ) : (
            <pre className="group/result relative text-xs text-stone-600 dark:text-stone-300 whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
              {mcp.text}
              <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/result:opacity-100 transition-opacity">
                {!hideCopyButton && <CopyButton text={mcp.text} size={12} />}
              </div>
            </pre>
          ))}
        <div className="flex flex-wrap gap-2">
          {(mcp.blocks || []).map((block, i) => (
            <McpBlockPreview key={i} block={block} />
          ))}
        </div>
      </div>
    );
  }

  if (generatedImages.length > 0) {
    return <GeneratedImageResults images={generatedImages} />;
  }

  // 富文本结果：dict 含 title/url/content 结构
  if (
    typeof result === "object" &&
    result !== null &&
    typeof result.content === "string" &&
    (typeof result.title === "string" || typeof result.url === "string")
  ) {
    const title = typeof result.title === "string" ? result.title : "";
    const url = typeof result.url === "string" ? result.url : "";
    return (
      <div className="rounded-md border border-stone-200 dark:border-stone-700 overflow-hidden">
        {(title || url) && (
          <div className="flex items-center gap-2 px-3 py-2 bg-stone-100 dark:bg-stone-800 border-b border-stone-200 dark:border-stone-700">
            <FileText
              size={14}
              className="shrink-0 text-stone-500 dark:text-stone-400"
            />
            {url ? (
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs font-medium text-stone-700 dark:text-stone-200 hover:underline truncate"
              >
                {title || url}
              </a>
            ) : (
              <span className="text-xs font-medium text-stone-700 dark:text-stone-200 truncate">
                {title}
              </span>
            )}
            {url && (
              <ExternalLink
                size={12}
                className="shrink-0 text-stone-400 dark:text-stone-500 ml-auto"
              />
            )}
          </div>
        )}
        <div className="group/rich relative p-3 text-xs text-stone-600 dark:text-stone-300 max-h-96 overflow-y-auto">
          <MarkdownContent content={result.content} />
          <div className="absolute top-1 right-1 opacity-0 group-hover/rich:opacity-100 transition-opacity">
            {!hideCopyButton && <CopyButton text={result.content} size={12} />}
          </div>
        </div>
      </div>
    );
  }

  // Plain object or JSON-parseable string — render as JSON
  if (typeof result === "object" && result !== null) {
    return <JsonFallback data={result} hideCopyButton={hideCopyButton} />;
  }

  if (textContent && typeof result === "string") {
    try {
      const parsed = JSON.parse(textContent);
      if (typeof parsed === "object" && parsed !== null) {
        return <JsonFallback data={parsed} hideCopyButton={hideCopyButton} />;
      }
    } catch {
      // not JSON, fall through
    }
  }

  if (textContent) {
    return isMarkdownText(textContent) ? (
      <div className="group/result relative text-xs text-stone-600 dark:text-stone-300 overflow-y-auto">
        <MarkdownContent content={textContent} />
        <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/result:opacity-100 transition-opacity">
          {!hideCopyButton && <CopyButton text={textContent} size={12} />}
        </div>
      </div>
    ) : (
      <pre className="group/result relative text-xs text-stone-600 dark:text-stone-300 overflow-y-auto whitespace-pre-wrap break-words">
        {textContent}
        <div className="absolute top-0.5 right-0.5 opacity-0 group-hover/result:opacity-100 transition-opacity">
          {!hideCopyButton && <CopyButton text={textContent} size={12} />}
        </div>
      </pre>
    );
  }

  return <JsonFallback data={result} hideCopyButton={hideCopyButton} />;
}

const MAX_JSON_COLLAPSED = 640;

function JsonFallback({
  data,
  hideCopyButton,
}: {
  data: unknown;
  hideCopyButton?: boolean;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const str = JSON.stringify(data, null, 2);
  const needsTruncation = str.length > MAX_JSON_COLLAPSED;
  const display =
    needsTruncation && !expanded
      ? str.slice(0, MAX_JSON_COLLAPSED) + "\n…"
      : str;

  return (
    <div className="group/json relative">
      <div className="absolute top-1 right-1 opacity-0 group-hover/json:opacity-100 transition-opacity z-10">
        {!hideCopyButton && <CopyButton text={str} size={12} />}
      </div>
      <pre className="text-xs text-stone-600 dark:text-stone-300 overflow-y-auto whitespace-pre-wrap break-words min-w-0">
        {display}
      </pre>
      {needsTruncation && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-1 mt-1 text-xs text-stone-500 dark:text-stone-400 hover:text-stone-700 dark:hover:text-stone-200 transition-colors"
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {expanded ? t("chat.message.collapse") : t("chat.message.expandAll")}
        </button>
      )}
    </div>
  );
}
