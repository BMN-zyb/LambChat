/**
 * Message part manipulation utilities.
 *
 * Low-level building blocks for creating, updating, and routing
 * message parts (text, thinking, tool, subagent, sandbox).
 * Used by eventProcessor.ts (the unified event handler).
 */
// 【消息 parts 的底层构造 / 合并 / 路由工具】
// 这些是被 eventProcessor 复用的纯函数积木：创建各类 part、把流式增量合并进已有 part、
// 按 depth 把 part 路由进正确的（可能多层嵌套的）子 agent，以及流结束时清理 loading 态。
// 全部为不可变更新（返回新数组，不改动入参）。

import type {
  MessagePart,
  SandboxPart,
  SubagentPart,
  SummaryPart,
  ThinkingPart,
  ToolPart,
  TodoPart,
} from "../../types";
import { parseDate } from "../../utils/datetime";
import type { SubagentStackItem } from "./types";

// ============================================
// Part creators
// ============================================

/**
 * Create a tool part from tool data.
 */
// 创建一个「待完成」的工具调用 part（isPending=true），工具结果到达后再回填。
export function createToolPart(
  toolName: string,
  args: Record<string, unknown>,
  depth: number,
  agentId?: string,
  toolCallId?: string,
  startedAt?: string,
): ToolPart {
  return {
    type: "tool",
    id: toolCallId,
    name: toolName,
    args: args,
    isPending: true,
    depth,
    agent_id: agentId,
    startedAt,
  };
}

/**
 * Create a thinking part from thinking data.
 */
// 创建思考 part；thinking_id 用于把同一思考块的多个增量归并到一起。
export function createThinkingPart(
  content: string,
  thinkingId: string | undefined,
  depth: number,
  agentId?: string,
  isStreaming = true,
): ThinkingPart {
  return {
    type: "thinking",
    content,
    thinking_id: thinkingId,
    depth,
    agent_id: agentId,
    isStreaming,
  };
}

/**
 * Create a subagent part from agent call data.
 */
// 创建子 agent part（status=running）；其 parts 字段用于承载该子 agent 内部的嵌套内容。
export function createSubagentPart(
  agentId: string,
  agentName: string,
  input: string,
  depth: number,
  timestamp?: string,
  agentAvatar?: string,
): SubagentPart {
  const startedAt = timestamp ? parseDate(timestamp).getTime() : Date.now();
  return {
    type: "subagent",
    agent_id: agentId,
    agent_name: agentName,
    agent_avatar: agentAvatar,
    input: input,
    isPending: true,
    status: "running",
    depth: depth,
    parts: [],
    startedAt,
  };
}

// ============================================
// Part merge helpers
// ============================================

/**
 * Merge a thinking chunk into an existing parts array (reverse scan).
 * Returns a new array with content concatenated, or null if no match found.
 */
// 把思考增量并入已有思考 part：倒序查找同一 thinking_id 的部件（无 ID 时匹配同样无 ID 的），
// 命中则拼接文本返回新数组；未找到返回 null（由调用方决定追加新部件）。
function mergeThinkingPart(
  parts: MessagePart[],
  part: ThinkingPart,
): MessagePart[] | null {
  const thinkingId = part.thinking_id;
  let existingIndex = -1;

  if (thinkingId !== undefined) {
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      if (
        p.type === "thinking" &&
        (p as ThinkingPart).thinking_id === thinkingId
      ) {
        existingIndex = i;
        break;
      }
    }
  } else {
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      if (
        p.type === "thinking" &&
        (p as ThinkingPart).thinking_id === undefined
      ) {
        existingIndex = i;
        break;
      }
    }
  }

  if (existingIndex < 0) return null;

  const newParts = [...parts];
  const existing = newParts[existingIndex] as ThinkingPart;
  newParts[existingIndex] = {
    ...existing,
    content: existing.content + part.content,
    isStreaming: true,
  };
  return newParts;
}

/**
 * Merge a text chunk into an existing parts array.
 * If the last part is text, concatenates content and returns a new array.
 * Otherwise returns null (caller should append).
 */
// 把正文增量并入末尾 part：仅当末尾是 text part 时拼接并返回新数组，否则返回 null。
function mergeTextPart(
  parts: MessagePart[],
  content: string,
): MessagePart[] | null {
  const lastPart = parts[parts.length - 1];
  if (lastPart?.type === "text") {
    const newParts = [...parts];
    newParts[newParts.length - 1] = {
      ...lastPart,
      content: lastPart.content + content,
    };
    return newParts;
  }
  return null;
}

/**
 * Merge a summary chunk into an existing parts array.
 * Returns a new array with content concatenated, or null if no match found.
 */
// 把摘要增量并入已有摘要 part（按 summary_id 匹配）；未找到返回 null。
function mergeSummaryPart(
  parts: MessagePart[],
  part: SummaryPart,
): MessagePart[] | null {
  const idx = findSummaryIndex(parts, part.summary_id);
  if (idx < 0) return null;

  const newParts = [...parts];
  const existing = newParts[idx] as SummaryPart;
  newParts[idx] = {
    ...existing,
    content: existing.content + part.content,
    isStreaming: part.isStreaming ? true : existing.isStreaming,
  };
  return newParts;
}

/**
 * Merge or append a part into a parts array.
 * Handles thinking, text, summary, and todo with merge semantics.
 * For all other types, appends a new copy.
 */
// 通用「合并或追加」：thinking/text/summary 走累加语义，todo 走单例 upsert，其余类型直接追加新副本。
function mergeOrAppendPart(
  existingParts: MessagePart[],
  part: MessagePart,
): MessagePart[] {
  switch (part.type) {
    case "thinking": {
      const merged = mergeThinkingPart(existingParts, part);
      return merged ?? [...existingParts, part];
    }
    case "text": {
      const merged = mergeTextPart(existingParts, part.content);
      return merged ?? [...existingParts, part];
    }
    case "summary": {
      const merged = mergeSummaryPart(existingParts, part);
      return merged ?? [...existingParts, part];
    }
    case "todo": {
      // Upsert: at most one todo per subagent
      // upsert：每个（子）agent 至多保留一个 todo part，已有则替换
      const todoIdx = existingParts.findIndex((p) => p.type === "todo");
      if (todoIdx >= 0) {
        const newParts = [...existingParts];
        newParts[todoIdx] = part;
        return newParts;
      }
      return [...existingParts, part];
    }
    default:
      return [...existingParts, part];
  }
}

// ============================================
// Depth management
// ============================================

/**
 * Search parts array for a matching subagent and merge/append the part into it.
 * Recursively descends into nested subagents. Returns updated parts array,
 * or null if no matching subagent was found.
 */
// 在 parts 中递归查找匹配的子 agent（pending、depth 相符、agent_id 相符），把 part 合并进它的内部 parts；
// 若本层未命中，则深入更深层嵌套的子 agent 继续查找。全程未找到返回 null。
function findAndMergeInSubagent(
  parts: MessagePart[],
  part: MessagePart,
  targetDepth: number,
  effectiveAgentId?: string,
): MessagePart[] | null {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];

    if (p.type === "subagent" && p.depth === targetDepth && p.isPending) {
      if (effectiveAgentId && p.agent_id !== effectiveAgentId) {
        continue;
      }
      const newSubagentParts = mergeOrAppendPart(p.parts || [], part);
      const newParts = [...parts];
      newParts[i] = { ...p, parts: newSubagentParts };
      return newParts;
    }

    // Recurse into nested subagents
    // 本层不是目标：若该子 agent 内部还有嵌套，递归深入其 parts 继续查找
    if (p.type === "subagent" && p.parts) {
      const result = findAndMergeInSubagent(
        p.parts,
        part,
        targetDepth,
        effectiveAgentId,
      );
      if (result) {
        const newParts = [...parts];
        newParts[i] = { ...p, parts: result };
        return newParts;
      }
    }
  }
  return null;
}

/**
 * Add a part to the correct depth position in the parts array.
 * For subagent events (depth > 0), the event's depth equals the subagent's depth.
 * Returns a new parts array (immutable update).
 * Uses agent_id for precise matching to support parallel subagents.
 */
// 把 part 添加到正确的嵌套深度（本模块的核心路由函数）：
// - targetDepth<=0：主线层。text 与相邻的顶层 text 合并，其余直接追加；
// - targetDepth>0：先从子 agent 栈解析出目标 agent_id（支持并行子 agent 的精确归属），
//   再递归找到对应子 agent 并合并进去；
//   若对应子 agent 尚未出现（例如 thinking 早于 agent:call 到达），则回退到主线层合并/追加。
// 始终返回不可变的新数组。
export function addPartToDepth(
  parts: MessagePart[],
  part: MessagePart,
  targetDepth: number,
  activeSubagentStack: SubagentStackItem[],
  targetAgentId?: string,
  messageId?: string,
): MessagePart[] {
  if (targetDepth <= 0) {
    // Merge adjacent text blocks at depth 0
    // 主线层：相邻的顶层 text 合并成一段，避免碎片化
    if (part.type === "text") {
      const lastPart = parts[parts.length - 1];
      if (lastPart?.type === "text" && !lastPart.depth) {
        const newParts = [...parts];
        newParts[newParts.length - 1] = {
          ...lastPart,
          content: lastPart.content + part.content,
        };
        return newParts;
      }
    }
    return [...parts, part];
  }

  // Resolve effectiveAgentId from stack (reverse scan, no allocation)
  // 未显式给出 agent_id 时，从子 agent 栈倒序推断目标 agent（匹配同一消息、深度相符或差一层）
  let effectiveAgentId = targetAgentId;
  if (!effectiveAgentId && messageId) {
    for (let i = activeSubagentStack.length - 1; i >= 0; i--) {
      const item = activeSubagentStack[i];
      if (
        item.message_id === messageId &&
        (item.depth === targetDepth || item.depth === targetDepth - 1)
      ) {
        effectiveAgentId = item.agent_id;
        break;
      }
    }
  }

  // Try to find matching subagent and merge into it
  // 尝试在（可能多层嵌套的）子 agent 中找到目标并合并进去
  const subagentResult = findAndMergeInSubagent(
    parts,
    part,
    targetDepth,
    effectiveAgentId,
  );
  if (subagentResult) return subagentResult;

  // Fallback: merge at top level when subagent block doesn't exist yet
  // (e.g. thinking arrives before agent:call)
  // 回退：对应子 agent 块尚未出现（如 thinking 早于 agent:call 到达）时，就地在主线层合并/追加
  if (part.type === "thinking") {
    const merged = mergeThinkingPart(parts, part);
    if (merged) return merged;
  } else if (part.type === "text") {
    const merged = mergeTextPart(parts, part.content);
    if (merged) return merged;
  } else if (part.type !== "subagent") {
    console.warn(
      "[addPartToDepth] No matching subagent found for depth:",
      targetDepth,
      "agent_id:",
      effectiveAgentId,
      "adding to top level",
    );
  }
  return [...parts, part];
}

// ============================================
// Subagent result
// ============================================

// 倒序查找匹配 summaryId 的摘要 part 索引；找不到返回 -1。
function findSummaryIndex(parts: MessagePart[], summaryId?: string): number {
  for (let i = parts.length - 1; i >= 0; i--) {
    const part = parts[i];
    if (part.type === "summary" && part.summary_id === summaryId) {
      return i;
    }
  }
  return -1;
}

/**
 * Update subagent result. Returns new parts array.
 */
// 子 agent 结束时回填其结果/成功态/错误：先在本层查找匹配的 pending 子 agent，
// 命中则置为 complete/error 终态；本层未命中则递归进更深层。无匹配则原样返回。
export function updateSubagentResult(
  parts: MessagePart[],
  agentId: string,
  result: string,
  success: boolean,
  targetDepth: number,
  error?: string,
  timestamp?: string,
): MessagePart[] {
  const completedAt = timestamp ? parseDate(timestamp).getTime() : Date.now();
  const status = success ? "complete" : "error";

  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (
      p.type === "subagent" &&
      p.agent_id === agentId &&
      p.depth === targetDepth &&
      p.isPending
    ) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        status,
        completedAt,
      };
      return newParts;
    }
    if (p.type === "subagent" && p.parts) {
      const updatedSubagent = updateSubagentResultInParts(
        p.parts,
        agentId,
        result,
        success,
        targetDepth,
        error,
        completedAt,
        status,
      );
      if (updatedSubagent) {
        const newParts = [...parts];
        newParts[i] = { ...p, parts: updatedSubagent };
        return newParts;
      }
    }
  }
  return parts;
}

/**
 * Recursively update subagent result in parts.
 */
// updateSubagentResult 的递归内核：在嵌套 parts 中查找并回填子 agent 结果；未找到返回 null。
export function updateSubagentResultInParts(
  parts: MessagePart[],
  agentId: string,
  result: string,
  success: boolean,
  targetDepth: number,
  error?: string,
  completedAt?: number,
  status?: "complete" | "error",
): MessagePart[] | null {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (
      p.type === "subagent" &&
      p.agent_id === agentId &&
      p.depth === targetDepth &&
      p.isPending
    ) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        status,
        completedAt,
      };
      return newParts;
    }
    if (p.type === "subagent" && p.parts) {
      const updatedParts = updateSubagentResultInParts(
        p.parts,
        agentId,
        result,
        success,
        targetDepth,
        error,
        completedAt,
        status,
      );
      if (updatedParts) {
        const newParts = [...parts];
        newParts[i] = { ...p, parts: updatedParts };
        return newParts;
      }
    }
  }
  return null;
}

// ============================================
// Tool result
// ============================================

/**
 * Update tool result at specified depth. Returns new parts array.
 */
// 工具结果回填：优先按 tool_call_id 精确匹配顶层 pending 工具（兼容无 id 时匹配首个 pending 工具），
// 顶层未命中再进入（agent_id 相符的）子 agent 内部递归查找。无匹配则原样返回。
export function updateToolResultInDepth(
  parts: MessagePart[],
  toolCallId: string,
  result: string | Record<string, unknown>,
  success: boolean,
  error?: string,
  _targetDepth?: number,
  targetAgentId?: string,
  completedAt?: string,
): MessagePart[] {
  // Try direct match on top-level tools first
  // 先在顶层工具中直接匹配
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (p.type === "tool" && p.id === toolCallId && p.isPending) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        completedAt,
      };
      return newParts;
    }
    // Backward compat: match by name when no id
    // 向后兼容：历史数据无 id 时，回填首个仍 pending 的无 id 工具
    if (p.type === "tool" && !p.id && p.isPending) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        completedAt,
      };
      return newParts;
    }
  }

  // Then search inside subagents
  // 顶层未命中：进入子 agent 内部递归查找
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (p.type === "subagent" && p.parts) {
      if (targetAgentId && p.agent_id !== targetAgentId) {
        continue;
      }
      const updatedParts = updateToolResultInPartsById(
        p.parts,
        toolCallId,
        result,
        success,
        error,
        completedAt,
      );
      if (updatedParts) {
        const newParts = [...parts];
        newParts[i] = { ...p, parts: updatedParts };
        return newParts;
      }
    }
  }
  return parts;
}

/**
 * Recursively update tool result in parts by tool_call_id.
 */
// updateToolResultInDepth 的递归内核：按 tool_call_id 在嵌套 parts 中回填工具结果；未找到返回 null。
export function updateToolResultInPartsById(
  parts: MessagePart[],
  toolCallId: string,
  result: string | Record<string, unknown>,
  success: boolean,
  error?: string,
  completedAt?: string,
): MessagePart[] | null {
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p.type === "tool" && p.id === toolCallId && p.isPending) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        completedAt,
      };
      return newParts;
    }
    if (p.type === "tool" && !p.id && p.isPending) {
      const newParts = [...parts];
      newParts[i] = {
        ...p,
        result,
        success,
        error,
        isPending: false,
        completedAt,
      };
      return newParts;
    }
    if (p.type === "subagent" && p.parts) {
      const updatedParts = updateToolResultInPartsById(
        p.parts,
        toolCallId,
        result,
        success,
        error,
        completedAt,
      );
      if (updatedParts) {
        const newParts = [...parts];
        newParts[i] = { ...p, parts: updatedParts };
        return newParts;
      }
    }
  }
  return null;
}

// ============================================
// Utility
// ============================================

/**
 * Clear all loading states in message parts recursively.
 * Sets isPending: false and cancelled: true on tools and subagents,
 * isStreaming: false on thinking, cancels unfinished todos.
 * Returns a new parts array with updated loading states.
 */
// 递归清理所有「加载中」状态（在流结束/中断时收尾，避免残留转圈动画）：
// 未完成的工具 → 取消；流式思考 → 结束；子 agent → 保留其已有的完成/错误终态、否则标记取消；
// 未完成的 todo 项 → 取消；启动中的沙箱 → 取消。返回新数组。
export function clearAllLoadingStates(parts: MessagePart[]): MessagePart[] {
  return parts.map((part) => {
    switch (part.type) {
      case "tool": {
        const toolPart = part as ToolPart;
        if (!toolPart.isPending) return part;
        return { ...toolPart, isPending: false, cancelled: true };
      }
      case "thinking": {
        const thinkingPart = part as ThinkingPart;
        if (!thinkingPart.isStreaming) return part;
        return { ...thinkingPart, isStreaming: false };
      }
      case "subagent": {
        const subagentPart = part as SubagentPart;
        const updatedParts = subagentPart.parts
          ? clearAllLoadingStates(subagentPart.parts)
          : [];
        // Preserve existing terminal status (complete/error) instead of forcing cancelled
        // 保留子 agent 已有的终态（complete/error），仅对仍未结束的才标记为 cancelled
        const wasCompleted = subagentPart.status === "complete";
        const hadError = subagentPart.status === "error";
        return {
          ...subagentPart,
          isPending: false,
          cancelled: !wasCompleted && !hadError,
          status: wasCompleted ? "complete" : hadError ? "error" : "cancelled",
          completedAt: subagentPart.completedAt || Date.now(),
          parts: updatedParts,
        };
      }
      case "todo": {
        const todoPart = part as TodoPart;
        const hasUnfinished = todoPart.items.some(
          (i) => i.status === "pending" || i.status === "in_progress",
        );
        if (!hasUnfinished && !todoPart.isStreaming) return part;
        return {
          ...todoPart,
          isStreaming: false,
          items: todoPart.items.map((i) =>
            i.status === "pending" || i.status === "in_progress"
              ? { ...i, status: "cancelled" as const, activeForm: undefined }
              : i,
          ),
        };
      }
      case "sandbox": {
        const sandboxPart = part as SandboxPart;
        if (sandboxPart.status !== "starting") return part;
        return { ...sandboxPart, status: "cancelled" };
      }
      default:
        return part;
    }
  });
}
