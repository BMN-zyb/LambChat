// 【人设的启用技能解析】计算「人设预设」应向后端声明的启用技能列表：
// 仅当选中了人设预设且该人设确有技能时才返回技能名列表，否则返回 undefined（表示不覆盖默认技能）。
export function resolvePersonaEnabledSkills(
  personaPresetId: string | null | undefined,
  personaSkillNames: string[] | undefined,
): string[] | undefined {
  if (!personaPresetId) return undefined;
  if (!personaSkillNames || personaSkillNames.length === 0) return undefined;
  return personaSkillNames ?? [];
}
