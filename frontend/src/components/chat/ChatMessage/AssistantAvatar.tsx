import {
  getPersonaAvatarIcon,
  isPersonaImageAvatar,
  isEmojiAvatar,
  getEmojiAvatarUrl,
  type PersonaAvatarIconKey,
} from "../../persona/personaAvatar";
import { useTranslation } from "react-i18next";
import { getFluentEmojiCDN } from "@lobehub/fluent-emoji";
import {
  Code2,
  Database,
  GraduationCap,
  Package,
  PenTool,
  Shield,
  Sparkles,
  Zap,
  type LucideIcon,
} from "lucide-react";

const ICONS: Record<PersonaAvatarIconKey, LucideIcon> = {
  sparkles: Sparkles,
  academic: GraduationCap,
  coding: Code2,
  writing: PenTool,
  security: Shield,
  data: Database,
  productivity: Zap,
  general: Package,
};

const DEFAULT_AVATAR_EMOJI = "🤖";
const DEFAULT_AVATAR_SRC = getFluentEmojiCDN(DEFAULT_AVATAR_EMOJI, {
  type: "anim",
});

export function AssistantAvatar({
  className,
  personaAvatar,
  personaSize = 22,
}: {
  className?: string;
  personaAvatar?: string | null;
  personaSize?: number;
}) {
  const { t } = useTranslation();
  const builtInIcon = getPersonaAvatarIcon(personaAvatar);
  if (builtInIcon) {
    const Icon = ICONS[builtInIcon.key];
    return (
      <div
        className={className}
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: builtInIcon.bg,
          borderRadius: "50%",
          width: personaSize + 6,
          height: personaSize + 6,
        }}
      >
        <Icon size={personaSize} style={{ color: builtInIcon.color }} />
      </div>
    );
  }

  if (isEmojiAvatar(personaAvatar)) {
    return (
      <img
        src={getEmojiAvatarUrl(personaAvatar)}
        alt={t("chat.assistant", "助手")}
        width={personaSize + 6}
        height={personaSize + 6}
        className={className}
      />
    );
  }

  if (isPersonaImageAvatar(personaAvatar)) {
    return (
      <img
        src={personaAvatar}
        alt={t("chat.assistant", "助手")}
        width={personaSize + 6}
        height={personaSize + 6}
        className={className}
      />
    );
  }

  return (
    <img
      src={DEFAULT_AVATAR_SRC}
      alt={t("chat.assistant", "助手")}
      width={28}
      height={28}
      className={className}
    />
  );
}
