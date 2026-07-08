// 【目标模式命令解析】把用户输入解析为「目标模式」的意图：
// 支持 `/goal <目标>` 斜杠命令（可用 `---` 分隔目标与自定义评分标准 rubric），
// 或在目标模式开关打开时把普通输入当作目标。产出统一的 GoalSubmissionPlan 指导后续发送。

import type { ActiveGoalSpec } from "./types";

// 匹配以 /goal 开头的命令（不区分大小写），捕获其后的正文
const GOAL_PREFIX_RE = /^\s*\/goal(?:\s+|\n|$)([\s\S]*)$/i;
// 匹配单独一行的 `---` 分隔符，用于切分「目标」与「评分标准」
const SEPARATOR_RE = /^\s*---\s*$/m;

// 前端目标命令的三种结果：run 启动目标（含目标与实际发送的 prompt）、clear 清除、invalid 非法输入。
export type FrontendGoalCommand =
  | { action: "run"; goal: ActiveGoalSpec; prompt: string }
  | { action: "clear" }
  | { action: "invalid" };

// 目标提交方案：告诉调用方实际要发送的内容、目标对象、目标模式开关与激活目标的下一状态，
// 以及是否「无需真正发送」（如仅清除/非法）与可选的错误提示键。
export interface GoalSubmissionPlan {
  content: string;
  goal: ActiveGoalSpec | null;
  nextGoalModeEnabled: boolean;
  nextActiveGoal: ActiveGoalSpec | null;
  handledWithoutSend: boolean;
  errorKey?: string;
}

// 依据目标描述生成默认评分标准（rubric）：用户未显式提供 rubric 时的兜底验收清单。
export function buildDefaultGoalRubric(objective: string): string {
  return [
    `- The final result directly satisfies this objective: ${objective}`,
    "- Every explicit requirement from the user has been addressed.",
    "- The work is verified with the strongest relevant evidence available.",
    "- Any remaining uncertainty, limitation, or skipped verification is clearly reported.",
  ].join("\n");
}

// 解析 /goal 命令：
// - 非 /goal 开头 → 返回 null（不是目标命令）；
// - 正文为 clear/reset/done/complete → clear（清除目标）；
// - 正文为空 → invalid；
// - 否则用 `---` 切出目标与可选 rubric，返回 run（含构造好的目标）。
export function parseFrontendGoalCommand(
  message: string,
): FrontendGoalCommand | null {
  const match = GOAL_PREFIX_RE.exec(message || "");
  if (!match) return null;

  const body = match[1]?.trim() || "";
  if (["clear", "reset", "done", "complete"].includes(body.toLowerCase())) {
    return { action: "clear" };
  }
  if (!body) return { action: "invalid" };

  const parts = body.split(SEPARATOR_RE);
  const objective = parts[0]?.trim() || "";
  if (!objective) return { action: "invalid" };
  const explicitRubric = parts.slice(1).join("---").trim();
  return {
    action: "run",
    goal: buildGoalFromPrompt(objective, explicitRubric),
    prompt: objective,
  };
}

// 从 prompt 构造目标对象：目标即 prompt，rubric 用显式值或默认清单，默认最多迭代 3 次。
export function buildGoalFromPrompt(
  prompt: string,
  explicitRubric?: string,
): ActiveGoalSpec {
  const objective = prompt.trim();
  return {
    objective,
    rubric: explicitRubric?.trim() || buildDefaultGoalRubric(objective),
    max_iterations: 3,
  };
}

// 汇总目标提交决策（发送前调用）：综合 /goal 命令与目标模式开关，产出 GoalSubmissionPlan。
// 分支：clear→清除且不发送；invalid→不发送并给出错误键；run→按命令目标发送；
// 目标模式开启但非命令→把普通输入当作目标发送；否则→普通发送（无目标）。
export function planGoalSubmission(
  content: string,
  goalModeEnabled: boolean,
): GoalSubmissionPlan {
  const command = parseFrontendGoalCommand(content);
  if (command?.action === "clear") {
    return {
      content,
      goal: null,
      nextGoalModeEnabled: false,
      nextActiveGoal: null,
      handledWithoutSend: true,
    };
  }
  if (command?.action === "invalid") {
    return {
      content,
      goal: null,
      nextGoalModeEnabled: goalModeEnabled,
      nextActiveGoal: null,
      handledWithoutSend: true,
      errorKey: "chat.goal.required",
    };
  }
  if (command?.action === "run") {
    return {
      content: command.prompt,
      goal: command.goal,
      nextGoalModeEnabled: false,
      nextActiveGoal: command.goal,
      handledWithoutSend: false,
    };
  }
  if (goalModeEnabled) {
    const goal = buildGoalFromPrompt(content);
    return {
      content,
      goal,
      nextGoalModeEnabled: true,
      nextActiveGoal: goal,
      handledWithoutSend: false,
    };
  }
  return {
    content,
    goal: null,
    nextGoalModeEnabled: false,
    nextActiveGoal: null,
    handledWithoutSend: false,
  };
}
