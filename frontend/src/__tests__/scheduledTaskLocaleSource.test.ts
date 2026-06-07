import assert from "node:assert/strict";
import test from "node:test";
import zh from "../i18n/locales/zh.json";

test("scheduled-task Chinese UI copy does not expose placeholders", () => {
  const scheduledTask = zh.scheduledTask as Record<string, string>;

  assert.equal(scheduledTask.conversationTasks, "会话定时任务");
  assert.equal(scheduledTask.details, "详情");
  assert.equal(
    scheduledTask.noConversationTasks,
    "当前会话暂无 Agent 创建的定时任务",
  );

  for (const [key, value] of Object.entries(scheduledTask)) {
    assert.doesNotMatch(value, /【待翻译】/, `scheduledTask.${key}`);
  }
});
