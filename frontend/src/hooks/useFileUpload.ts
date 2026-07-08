// 【文件上传 hook】管理聊天输入框的附件上传：拉取上传限制、校验大小/数量、
// 图片上传前压缩、通过 Web Worker 计算文件哈希以支持「秒传」（服务端已存在则直接引用），
// 带进度回调与可取消能力，并把附件状态经 onAttachmentsChange 回写给外层。

import { useState, useCallback, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { uploadApi } from "../services/api";
import { buildApiUrl } from "../services/api/config";
import type { FileCheckResult } from "../types";
import { compressImageFile } from "../utils/imageCompression";
import { uuid } from "../utils/uuid";
import type { MessageAttachment, FileCategory } from "../types";

// 各类文件的大小上限（MB）与单次最大文件数，由后端配置下发。
export interface UploadLimits {
  image: number;
  video: number;
  audio: number;
  document: number;
  maxFiles: number;
}

// hook 入参：受控的附件列表及其变更回调（支持直接赋值或函数式更新）。
export interface UseFileUploadOptions {
  attachments: MessageAttachment[];
  onAttachmentsChange: (
    attachments:
      | MessageAttachment[]
      | ((prev: MessageAttachment[]) => MessageAttachment[]),
  ) => void;
}

// 依据 MIME 类型判定文件类别（图片/视频/音频/其余为文档）。
function getFileCategory(file: File): FileCategory {
  const type = file.type.toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return "audio";
  return "document";
}

// 在 Web Worker 中计算文件哈希（避免阻塞主线程），用于「秒传」去重。返回哈希字符串的 Promise。
function computeFileHash(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL("../workers/hashWorker.ts", import.meta.url),
      { type: "module" },
    );
    worker.onmessage = (e) => {
      worker.terminate();
      if (e.data.error) {
        reject(new Error(e.data.error));
      } else {
        resolve(e.data.hash);
      }
    };
    worker.onerror = (e) => {
      worker.terminate();
      reject(new Error(e.message));
    };
    worker.postMessage({ file });
  });
}

// useFileUpload：提供上传限制、上传/取消方法与大小/数量校验，供聊天输入框使用。
export function useFileUpload({
  attachments,
  onAttachmentsChange,
}: UseFileUploadOptions) {
  const { t } = useTranslation();
  const [uploadLimits, setUploadLimits] = useState<UploadLimits | null>(null);
  const limitsFetched = useRef(false);
  // 进行中上传的取消函数表：key 为临时附件 ID，value 为其 abort 回调
  const abortMapRef = useRef<Map<string, () => void>>(new Map());
  const isMountedRef = useRef(true);

  // 卸载时中止所有进行中的上传并清空取消表，避免更新已卸载组件
  useEffect(() => {
    const abortMap = abortMapRef.current;
    return () => {
      isMountedRef.current = false;
      for (const abort of abortMap.values()) {
        abort();
      }
      abortMap.clear();
    };
  }, []);

  // Fetch upload limits once
  // 仅拉取一次上传限制配置（用 ref 防重复）
  useEffect(() => {
    if (limitsFetched.current) {
      return;
    }

    limitsFetched.current = true;
    let isMounted = true;

    uploadApi
      .getConfig()
      .then((config) => {
        if (isMounted && config.uploadLimits) {
          setUploadLimits(config.uploadLimits);
        }
      })
      .catch(() => {});

    return () => {
      isMounted = false;
    };
  }, []);

  /** Validate file size, returns true if ok */
  // 校验单个文件大小是否在该类别上限内（未取到限制时放行）；超限则 toast 提示并返回 false。
  const validateSize = useCallback(
    (file: File, category: FileCategory): boolean => {
      if (!uploadLimits) return true;
      const maxMB = uploadLimits[category];
      if (file.size > maxMB * 1024 * 1024) {
        toast.error(`${t("fileUpload.fileTooLarge")} (${maxMB}MB)`);
        return false;
      }
      return true;
    },
    [uploadLimits, t],
  );

  /** Validate file count (existing + new), returns true if ok */
  // 校验「已有 + 新增」文件数是否超过上限；超限则提示并返回 false。
  const validateCount = useCallback(
    (newFileCount: number): boolean => {
      if (!uploadLimits) return true;
      const remaining = uploadLimits.maxFiles - attachments.length;
      if (remaining <= 0 || newFileCount > remaining) {
        toast.error(
          t("fileUpload.tooManyFiles", { count: uploadLimits.maxFiles }),
        );
        return false;
      }
      return true;
    },
    [uploadLimits, attachments.length, t],
  );

  /** Cancel an in-progress upload by attachment id */
  // 按附件 ID 取消进行中的上传：调用其 abort、从取消表移除，并把该附件从列表中删除。
  const cancelUpload = useCallback(
    (id: string) => {
      const abort = abortMapRef.current.get(id);
      if (abort) {
        abort();
        abortMapRef.current.delete(id);
      }
      onAttachmentsChange((prev) => prev.filter((a) => a.id !== id));
    },
    [onAttachmentsChange],
  );

  /** Upload a single file with progress tracking */
  // 上传单个文件（带进度）。流程：图片先压缩 → 插入临时占位附件 → 计算哈希 →
  // 调 checkFile 探测「秒传」（服务端已有则直接引用）→ 否则真正上传并跟踪进度 →
  // 成功后用最终附件替换占位；失败则移除占位并提示。全程用 isMountedRef 防卸载后更新。
  const uploadFile = useCallback(
    (file: File, category?: FileCategory) => {
      const fileCategory = category || getFileCategory(file);

      // Compress images before upload
      // 图片先尝试压缩（失败则回退用原文件），其余类型直接使用原文件
      const maybeCompress =
        fileCategory === "image"
          ? compressImageFile(file).catch(() => file)
          : Promise.resolve(file);

      maybeCompress.then((processedFile) => {
        if (!isMountedRef.current) {
          return;
        }
        const tempId = `temp-${uuid()}`;

        // 先插入一个「上传中」的临时占位附件，UI 立即显示进度
        const tempAttachment: MessageAttachment = {
          id: tempId,
          key: "",
          name: processedFile.name,
          type: fileCategory,
          mimeType: processedFile.type,
          size: processedFile.size,
          url: "",
          uploadProgress: 0,
          isUploading: true,
        };

        onAttachmentsChange((prev) => [...prev, tempAttachment]);

        computeFileHash(processedFile)
          .then((hash) => {
            if (!isMountedRef.current) {
              throw new Error("Upload was aborted");
            }
            onAttachmentsChange((prev: MessageAttachment[]) =>
              prev.map((a) =>
                a.id === tempId ? { ...a, uploadProgress: 1 } : a,
              ),
            );
            // 用哈希向服务端探测是否已存在该文件（秒传）
            return uploadApi
              .checkFile(
                hash,
                processedFile.size,
                processedFile.name,
                processedFile.type,
              )
              .then((check) => ({ check }));
          })
          .catch(() => ({ check: { exists: false } }))
          .then(({ check }) => {
            if (!isMountedRef.current) {
              return;
            }
            // 秒传命中：服务端已有该文件，直接用其 key/url 生成最终附件，跳过真正上传
            if (check.exists && "key" in check) {
              abortMapRef.current.delete(tempId);
              const c = check as FileCheckResult;
              const finalAttachment: MessageAttachment = {
                id: uuid(),
                key: c.key ?? "",
                name: c.name || processedFile.name,
                type: c.type as FileCategory,
                mimeType: c.mimeType ?? processedFile.type,
                size: c.size ?? processedFile.size,
                url: buildApiUrl(c.url || `/api/upload/file/${c.key ?? ""}`),
              };
              onAttachmentsChange((prev: MessageAttachment[]) =>
                prev.map((a) =>
                  a.id === tempId
                    ? {
                        ...finalAttachment,
                        uploadProgress: 100,
                        isUploading: false,
                      }
                    : a,
                ),
              );
              return;
            }

            // 未命中秒传：真正上传文件，onProgress 回调实时更新占位附件的进度
            const handle = uploadApi.uploadFile(processedFile, {
              onProgress: (progress) => {
                if (!isMountedRef.current) {
                  return;
                }
                onAttachmentsChange((prev: MessageAttachment[]) =>
                  prev.map((a) =>
                    a.id === tempId
                      ? { ...a, uploadProgress: progress, isUploading: true }
                      : a,
                  ),
                );
              },
            });

            abortMapRef.current.set(tempId, handle.abort);

            // 上传完成：用服务端返回的最终信息替换占位附件
            return handle.promise.then((result) => {
              if (!isMountedRef.current) {
                return;
              }
              abortMapRef.current.delete(tempId);
              const finalAttachment: MessageAttachment = {
                id: uuid(),
                key: result.key,
                name: result.name || processedFile.name,
                type: result.type as FileCategory,
                mimeType: result.mimeType,
                size: result.size,
                url: buildApiUrl(result.url),
              };
              onAttachmentsChange((prev: MessageAttachment[]) =>
                prev.map((a) => (a.id === tempId ? finalAttachment : a)),
              );
            });
          })
          .catch((error) => {
            // 上传失败：清理取消表；主动中止不提示，其余错误 toast 并移除占位附件
            abortMapRef.current.delete(tempId);
            if (!isMountedRef.current) {
              return;
            }
            if (
              error instanceof Error &&
              error.message === "Upload was aborted"
            ) {
              return;
            }
            console.error("Upload failed:", error);
            toast.error(
              error instanceof Error
                ? error.message
                : t("fileUpload.uploadFailed"),
            );
            onAttachmentsChange((prev: MessageAttachment[]) =>
              prev.filter((a) => a.id !== tempId),
            );
          });
      });
    },
    [onAttachmentsChange, t],
  );

  /** Validate and upload multiple files */
  // 批量上传：先校验总数，再逐个按大小校验后上传（跳过超限文件）。
  const uploadFiles = useCallback(
    (files: FileList | File[], category?: FileCategory) => {
      const fileArray = Array.from(files);
      if (fileArray.length === 0) return;

      if (!validateCount(fileArray.length)) return;

      for (const file of fileArray) {
        const fileCategory = category || getFileCategory(file);
        if (!validateSize(file, fileCategory)) continue;
        uploadFile(file, fileCategory);
      }
    },
    [validateCount, validateSize, uploadFile],
  );

  return {
    uploadLimits,
    uploadFiles,
    uploadFile,
    validateSize,
    validateCount,
    cancelUpload,
  };
}

// 同时按具名导出 getFileCategory，供其它模块单独复用类别判定逻辑。
export { getFileCategory };
