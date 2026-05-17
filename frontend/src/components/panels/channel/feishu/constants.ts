// Predefined emoji options — status/acknowledgment emojis for bot reactions
// Source: https://open.larksuite.com/document/server-docs/im-v1/message-reaction/emojis-introduce
export const PREDEFINED_EMOJIS = [
  { value: "THUMBSUP", emoji: "👍", labelKey: "feishu.emoji.thumbsUp" },
  { value: "OK", emoji: "👌", labelKey: "feishu.emoji.ok" },
  { value: "DONE", emoji: "✅", labelKey: "feishu.emoji.done" },
  { value: "Yes", emoji: "☑️", labelKey: "feishu.emoji.yes" },
  { value: "CheckMark", emoji: "✔️", labelKey: "feishu.emoji.checkMark" },
  { value: "Get", emoji: "📥", labelKey: "feishu.emoji.get" },
  { value: "OnIt", emoji: "🎯", labelKey: "feishu.emoji.onIt" },
  { value: "OneSecond", emoji: "⏳", labelKey: "feishu.emoji.oneSecond" },
  { value: "LGTM", emoji: "👀", labelKey: "feishu.emoji.lgtm" },
  { value: "MeMeMe", emoji: "🙋", labelKey: "feishu.emoji.meMeMe" },
  { value: "THANKS", emoji: "🙏", labelKey: "feishu.emoji.thanks" },
  { value: "SALUTE", emoji: "🫡", labelKey: "feishu.emoji.salute" },
  { value: "CLAP", emoji: "👏", labelKey: "feishu.emoji.clap" },
  { value: "Fire", emoji: "🔥", labelKey: "feishu.emoji.fire" },
  { value: "MUSCLE", emoji: "💪", labelKey: "feishu.emoji.muscle" },
  { value: "PRAISE", emoji: "🏅", labelKey: "feishu.emoji.praise" },
];

export const DEFAULT_AUDIO_TRANSCRIBE_PROMPT =
  "Please transcribe and understand this voice message. Use the audio_transcribe tool for the attached audio when needed.";
