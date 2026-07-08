import { resolvePersonaEnabledSkills } from "./personaRequestConfig";

// 【本次运行的启用技能覆盖】决定单次发送最终使用的启用技能：
// 本次显式指定的 runEnabledSkills 优先；否则回退到人设的启用技能（见 resolvePersonaEnabledSkills）。
export function resolveRunEnabledSkills({
  personaPresetId,
  personaEnabledSkills,
  runEnabledSkills,
}: {
  personaPresetId?: string | null;
  personaEnabledSkills?: string[];
  runEnabledSkills?: string[];
}): string[] | undefined {
  if (runEnabledSkills) {
    return runEnabledSkills;
  }
  return resolvePersonaEnabledSkills(personaPresetId, personaEnabledSkills);
}
