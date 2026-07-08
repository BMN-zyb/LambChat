/**
 * Unified message event processor.
 *
 * Single source of truth for transforming message state in response to events.
 * Both streaming (eventHandlers.ts) and history (historyLoader.ts) delegate here.
 *
 * Side effects like subagent stack push/pop, connection status, etc.
 * are handled by the caller based on event type.
 */
// 【事件 → 消息 parts 的唯一转换核心】
// 无论是实时流（eventHandlers）还是历史重建（historyLoader），都调用 processMessageEvent
// 把一个事件「纯函数式」地合并进消息的 parts / content / toolCalls。它不做任何副作用，
// 只返回新的状态片段，副作用（子 agent 入栈出栈、连接状态、沙箱状态等）由调用方负责。
//
// 贯穿全文件的关键概念——按 depth 路由：
// - depth > 0：事件来自子 agent，统一用 addPartToDepth 将 part 塞进对应子 agent part 的嵌套 children；
// - depth === 0：主线消息，按 part 类型做累加（thinking/text/summary 拼接）或替换（sandbox/todo 等 upsert）。

import type {
  MessagePart,
  MessageAttachment,
  ToolCall,
  ToolResult,
  TokenUsagePart,
  SandboxPart,
  TodoPart,
  SummaryPart,
  RecommendQuestion,
  ArtifactPartArtifact,
} from "../../types";
import i18n from "../../i18n";
import { translateBackendError } from "../../utils/backendErrors";
import type { EventData, SubagentStackItem } from "./types";
import {
  addPartToDepth,
  createSubagentPart,
  createThinkingPart,
  createToolPart,
  updateSubagentResult,
  updateToolResultInDepth,
  clearAllLoadingStates,
} from "./messageParts";
import type { ThinkingPart } from "../../types";

// ============================================
// Shared utilities
// ============================================

/**
 * Convert backend attachment format to frontend format.
 */
// 把后端附件字段（snake_case）转换为前端使用的驼峰命名结构；入参为空则返回 undefined。
export function convertAttachments(
  attachments?: Array<{
    id: string;
    key: string;
    name: string;
    type: string;
    mime_type: string;
    size: number;
    url: string;
  }>,
): MessageAttachment[] | undefined {
  return attachments?.map((a) => ({
    id: a.id,
    key: a.key,
    name: a.name,
    type: a.type as MessageAttachment["type"],
    mimeType: a.mime_type,
    size: a.size,
    url: a.url,
  }));
}

// ============================================
// Event processor
// ============================================

/**
 * Result of processing a message event.
 */
// 处理单个事件后返回的状态片段：更新后的 parts、正文 content、工具调用列表，
// 以及可选的工具结果 / token 用量 / 耗时 / 是否被取消（由调用方回写到消息上）。
export interface ProcessMessageEventResult {
  parts: MessagePart[];
  content: string;
  toolCalls: ToolCall[];
  toolResult?: ToolResult;
  tokenUsage?: TokenUsagePart;
  duration?: number;
  cancelled?: boolean;
}

/**
 * Unified message event processor.
 */
// 统一事件处理器（纯函数）。
// 参数：eventType 事件类型、data 事件载荷、parts/content/toolCalls 当前消息状态、
// depth 嵌套深度、subagentStack 子 agent 栈、isStreaming 是否流式中、messageId 消息 ID。
// 返回：合并事件后的新状态片段（见 ProcessMessageEventResult），不改动入参。
export function processMessageEvent(
  eventType: string,
  data: EventData,
  parts: MessagePart[],
  content: string,
  toolCalls: ToolCall[],
  depth: number,
  subagentStack: SubagentStackItem[],
  isStreaming: boolean,
  messageId?: string,
): ProcessMessageEventResult {
  // 以当前状态为基础构造结果对象，后续按事件类型就地覆盖对应字段后返回
  const result: ProcessMessageEventResult = { parts, content, toolCalls };
  const agentId = data.agent_id;

  switch (eventType) {
    // ---- Agent events ----

    // 子 agent 调用开始：创建 subagent part，并按 depth 塞入嵌套结构（记录到子 agent 栈）
    case "agent:call": {
      const subagentPart = createSubagentPart(
        agentId || "unknown",
        data.agent_name || agentId || i18n.t("chat.unknownAgent"),
        data.input || "",
        depth,
        data.timestamp,
        data.agent_avatar,
      );
      result.parts = addPartToDepth(
        parts,
        subagentPart,
        depth,
        subagentStack,
        agentId || "unknown",
        messageId,
      );
      break;
    }

    // 子 agent 调用结束：把结果/成功态/错误写回对应的 subagent part
    case "agent:result": {
      result.parts = updateSubagentResult(
        parts,
        agentId || "unknown",
        String(data.result || ""),
        data.success !== false,
        depth,
        data.error,
        data.timestamp,
      );
      break;
    }

    // ---- Thinking events ----

    // 思考事件：depth>0 归入子 agent；depth===0 时按 thinking_id 找到同一思考块累加文本，否则新建
    case "thinking": {
      const thinkingContent = data.content || "";
      if (!thinkingContent) break;

      const thinkingPart = createThinkingPart(
        thinkingContent,
        data.thinking_id,
        depth,
        agentId,
        isStreaming,
      );

      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          thinkingPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        const newParts = [...parts];
        let existingIndex = -1;

        // Reverse scan: matching thinking part is usually at the end
        // 从尾部倒序查找同一思考块（流式追加时目标通常就在末尾），命中则累加、否则新增
        for (let i = newParts.length - 1; i >= 0; i--) {
          const p = newParts[i];
          if (p.type === "thinking") {
            const tid = (p as ThinkingPart).thinking_id;
            if (
              data.thinking_id !== undefined
                ? tid === data.thinking_id
                : tid === undefined
            ) {
              existingIndex = i;
              break;
            }
          }
        }

        if (existingIndex >= 0) {
          const existing = newParts[existingIndex] as ThinkingPart;
          newParts[existingIndex] = {
            ...existing,
            content: existing.content + thinkingContent,
            isStreaming: isStreaming ? true : existing.isStreaming,
          };
        } else {
          newParts.push(thinkingPart);
        }
        result.parts = newParts;
      }
      break;
    }

    // ---- Message chunk events ----

    // 正文增量：depth>0 归入子 agent；depth===0 时若上一个是顶层 text part 则拼接，
    // 否则新建 text part，并同步把增量累加到消息 content
    case "message:chunk": {
      const chunkContent = data.content || "";
      if (!chunkContent) break;

      if (depth > 0) {
        const textPart = {
          type: "text" as const,
          content: chunkContent,
          depth,
          agent_id: agentId,
        };
        result.parts = addPartToDepth(
          parts,
          textPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        const newParts = [...parts];
        const lastPart = newParts[newParts.length - 1];
        if (lastPart?.type === "text" && !lastPart.depth) {
          newParts[newParts.length - 1] = {
            ...lastPart,
            content: lastPart.content + chunkContent,
          };
        } else {
          newParts.push({ type: "text" as const, content: chunkContent });
        }
        result.parts = newParts;
        result.content = content + chunkContent;
      }
      break;
    }

    // ---- Tool events ----

    // 工具调用开始：构造 toolCall 与 tool part；depth>0 入嵌套，否则追加到顶层并登记 toolCalls
    case "tool:start": {
      const toolCallId = data.tool_call_id as string | undefined;
      const toolCall: ToolCall = {
        id: toolCallId,
        name: data.tool || "",
        args: data.args || {},
      };
      const toolPart = createToolPart(
        data.tool || "",
        data.args || {},
        depth,
        agentId,
        toolCallId,
        data.timestamp as string | undefined,
      );

      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          toolPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        result.parts = [...parts, toolPart];
        result.toolCalls = [...toolCalls, toolCall];
      }
      break;
    }

    // 工具结果：有 tool_call_id 或 depth>0 时按 ID 精确回填对应工具 part；
    // 否则回填「第一个同名且仍 pending」的工具 part，并产出 toolResult 供调用方收集
    case "tool:result": {
      const toolCallId = data.tool_call_id as string | undefined;
      const toolName = data.tool || "";
      const isSuccess = data.success !== false;
      const errorMsg = data.error as string | undefined;
      const resultContent = data.result || "";
      const completedAt = data.timestamp as string | undefined;

      if (depth > 0 || toolCallId) {
        result.parts = updateToolResultInDepth(
          parts,
          toolCallId || "",
          resultContent,
          isSuccess,
          errorMsg,
          depth,
          agentId,
          completedAt,
        );
      } else {
        let updated = false;
        const newParts = parts.map((p) => {
          if (
            p.type === "tool" &&
            p.name === toolName &&
            p.isPending &&
            !updated
          ) {
            updated = true;
            return {
              ...p,
              result: resultContent,
              success: isSuccess,
              error: errorMsg,
              isPending: false,
              completedAt,
            };
          }
          return p;
        });
        result.parts = newParts;
        result.toolResult = {
          id: toolCallId,
          name: toolName,
          result: resultContent,
          success: isSuccess,
        };
      }
      break;
    }

    // ---- Artifact events ----

    // 产物结果：构造 artifact part（含成功态/错误），按 depth 入嵌套或追加到顶层
    case "artifact:result": {
      const artifact = data.artifact as ArtifactPartArtifact | undefined;
      if (!artifact) break;

      const artifactPart = {
        type: "artifact" as const,
        artifact,
        success: data.success !== false,
        error: data.error as string | undefined,
        depth,
        agent_id: agentId,
        completedAt: data.timestamp as string | undefined,
      };

      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          artifactPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        result.parts = [...parts, artifactPart];
      }
      break;
    }

    // ---- Sandbox events ----

    // 沙箱启动中：以单例形式 upsert 一个 sandbox part（保留原有 startedAt）
    case "sandbox:starting": {
      const sandboxPart: SandboxPart = {
        type: "sandbox",
        status: "starting",
        timestamp: data.timestamp,
      };
      result.parts = upsertSandboxPart(parts, sandboxPart);
      break;
    }

    // 沙箱就绪：更新 sandbox part 为 ready 状态，带上沙箱 ID 与工作目录
    case "sandbox:ready": {
      const readyPart: SandboxPart = {
        type: "sandbox",
        status: "ready",
        sandbox_id: data.sandbox_id,
        work_dir: data.work_dir,
        timestamp: data.timestamp,
        completedAt: data.timestamp,
      };
      result.parts = upsertSandboxPart(parts, readyPart);
      break;
    }

    // 沙箱错误：更新 sandbox part 为 error 状态并记录错误信息
    case "sandbox:error": {
      const errorPart: SandboxPart = {
        type: "sandbox",
        status: "error",
        error: data.error,
        timestamp: data.timestamp,
        completedAt: data.timestamp,
      };
      result.parts = upsertSandboxPart(parts, errorPart);
      break;
    }

    // ---- Token usage ----

    // token 用量：写入 tokenUsage 统计；duration 由后端的秒转为毫秒
    case "token:usage": {
      result.tokenUsage = {
        type: "token_usage",
        input_tokens: data.input_tokens || 0,
        output_tokens: data.output_tokens || 0,
        total_tokens: data.total_tokens || 0,
        cache_creation_tokens: data.cache_creation_tokens || 0,
        cache_read_tokens: data.cache_read_tokens || 0,
        model_id: data.model_id,
        model: data.model,
      };
      if (data.duration) result.duration = data.duration * 1000;
      break;
    }

    // ---- Error ----

    // ---- Todo events ----

    // 待办更新：depth>0 入嵌套；否则整体替换（待办清单在一条消息中为单例 part）
    case "todo:updated": {
      const todos = (data.todos || []) as TodoPart["items"];
      if (!todos.length) break;
      const todoPart: TodoPart = { type: "todo", items: todos, isStreaming };
      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          todoPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        result.parts = upsertTodoPart(parts, todoPart);
      }
      break;
    }

    // ---- Summary events ----

    // 摘要：depth>0 入嵌套；否则按 summary_id 找到同一摘要块累加文本，否则新建
    case "summary": {
      const summaryContent = data.content || "";
      if (!summaryContent) break;

      const summaryPart: SummaryPart = {
        type: "summary",
        content: summaryContent,
        summary_id: data.summary_id,
        depth,
        agent_id: agentId,
        isStreaming,
      };

      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          summaryPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        const newParts = [...parts];
        let lastSummaryIdx = -1;
        for (let i = newParts.length - 1; i >= 0; i--) {
          const p = newParts[i];
          if (p.type === "summary" && p.summary_id === data.summary_id) {
            lastSummaryIdx = i;
            break;
          }
        }
        if (lastSummaryIdx >= 0) {
          const existing = newParts[lastSummaryIdx] as SummaryPart;
          newParts[lastSummaryIdx] = {
            ...existing,
            content: existing.content + summaryContent,
          };
        } else {
          newParts.push(summaryPart);
        }
        result.parts = newParts;
      }
      break;
    }

    // ---- Recommended follow-up questions ----

    // 推荐/追问问题：规范化问题列表后，depth>0 入嵌套，否则整体替换（单例 part）
    case "recommend:questions":
    case "followup:questions": {
      const questions = normalizeRecommendQuestions(data.questions);
      if (!questions.length) break;

      const recommendPart = {
        type: "recommend_questions" as const,
        questions,
        depth,
        agent_id: agentId,
      };

      if (depth > 0) {
        result.parts = addPartToDepth(
          parts,
          recommendPart,
          depth,
          subagentStack,
          agentId,
          messageId,
        );
      } else {
        result.parts = upsertRecommendQuestionsPart(parts, recommendPart);
      }
      break;
    }

    // ---- Completion ----

    // 完成：清除所有 loading 占位态（把仍在 pending 的部件收尾）
    case "complete":
    case "done": {
      result.parts = clearAllLoadingStates(parts);
      break;
    }

    // ---- Error ----

    // 错误：翻译后端错误信息；若为取消则标记 cancelled，否则把正文替换为错误提示
    case "error": {
      const errorMsg = data.error
        ? translateBackendError(data.error, i18n.t.bind(i18n))
        : i18n.t("chat.unknownError");
      const isCancelled = data.type === "CancelledError";
      result.parts = isStreaming ? clearAllLoadingStates(parts) : parts;
      result.cancelled = isCancelled;
      if (!isCancelled) {
        result.content = i18n.t("chat.errorPrefix", { error: errorMsg });
      }
      break;
    }
  }

  return result;
}

// ============================================
// Internal helpers
// ============================================

/** Replace existing sandbox part or append if none exists.
 *  Preserves `startedAt` from the previous part so the original
 *  starting timestamp survives across status transitions. */
// 沙箱 part 单例化：已存在则替换（并沿用旧的 startedAt，使状态切换时保留最初开始时间），否则追加。
function upsertSandboxPart(
  parts: MessagePart[],
  sandboxPart: SandboxPart,
): MessagePart[] {
  return parts.some((p) => p.type === "sandbox")
    ? parts.map((p) => {
        if (p.type !== "sandbox") return p;
        const prevStartedAt = p.startedAt;
        return {
          ...sandboxPart,
          startedAt: sandboxPart.startedAt ?? prevStartedAt,
        };
      })
    : [
        ...parts,
        {
          ...sandboxPart,
          startedAt: sandboxPart.startedAt ?? sandboxPart.timestamp,
        },
      ];
}

/** Replace existing todo part or append if none exists. */
// 待办 part 单例化：已存在则整体替换为最新待办，否则追加。
function upsertTodoPart(
  parts: MessagePart[],
  todoPart: TodoPart,
): MessagePart[] {
  return parts.some((p) => p.type === "todo")
    ? parts.map((p) => (p.type === "todo" ? todoPart : p))
    : [...parts, todoPart];
}

// 规范化推荐/追问问题：兼容「纯字符串」与「对象（content/text/title + 上传配置）」两种形态，
// 统一转成 { content, upload? } 并过滤掉空内容项。
function normalizeRecommendQuestions(
  questions: EventData["questions"],
): RecommendQuestion[] {
  if (!Array.isArray(questions)) return [];

  return questions
    .map((question) => {
      if (typeof question === "string") {
        const content = question.trim();
        return content ? { content } : null;
      }

      const content = (
        question.content ||
        question.text ||
        question.title ||
        ""
      ).trim();
      if (!content) return null;

      return {
        content,
        upload: question.upload || question.data_upload,
      };
    })
    .filter((question): question is RecommendQuestion => question !== null);
}

// 推荐问题 part 单例化：已存在则替换，否则追加。
function upsertRecommendQuestionsPart(
  parts: MessagePart[],
  recommendPart: Extract<MessagePart, { type: "recommend_questions" }>,
): MessagePart[] {
  return parts.some((p) => p.type === "recommend_questions")
    ? parts.map((p) => (p.type === "recommend_questions" ? recommendPart : p))
    : [...parts, recommendPart];
}
