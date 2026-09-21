"""
FlOZE 复刻 —— 角色专属音色映射

这些 voice_id 由 MiniMax `voice_design` API 生成（用文字描述定制音色），
每个 $3 一次性费用，**务必保留，丢失需重新设计**。

生成时间：2026-09-21
生成方式：python3 -c "..." 见 gen_voices.py

对应关系：
    角色        音色描述要点
    ---------   ------------------------------------------
    Lucien      克制优雅的男声，低而缓慢，贵族式的忧郁
    Seraphine   戒备的女声，干涩平稳，疲惫的士兵
    Ilias       圆滑带笑意的男声，宫廷法师，从不直接回答
    Nyx         明快好胜的年轻声音，带笑意的挑逗
    Isolde      轻柔疏远的女声，像隔着一堵墙传来
    Rook        粗砺讽刺的女声，雇佣兵队长，没有耐心
"""

# 角色名（小写） → voice_id
CHARACTER_VOICES: dict[str, str] = {
    "lucien":    "ttv-voice-2026092102012226-K876Yahq",
    "seraphine": "ttv-voice-2026092102013826-Rsbqk1lJ",
    "ilias":     "ttv-voice-2026092102015326-CifIo68c",
    "nyx":       "ttv-voice-2026092102021026-bexU5gwc",
    "isolde":    "ttv-voice-2026092102022626-TAgarzoZ",
    "rook":      "ttv-voice-2026092102024326-gSHsxZcc",
}

# 描述性短语（与原 prompt 一致，便于将来重新生成时复现）
VOICE_PROMPTS: dict[str, str] = {
    "lucien": ("A restrained, elegant male voice, low and deliberate. Aristocratic, "
               "melancholy, speaks slowly as though every word costs him something. "
               "Mid-thirties, quiet intensity, faint weariness."),
    "seraphine": ("A guarded female voice, dry and level. A soldier who has stopped "
                  "expecting good news. Low alto, clipped delivery, traces of wry humour "
                  "underneath the fatigue. Late twenties."),
    "ilias": ("A smooth, amused male voice with a knowing lilt. Court sorcerer — never "
              "answers directly, enjoys the question more than the answer. Warm baritone, "
              "theatrical but controlled."),
    "nyx": ("A bright, competitive voice, quick and teasing. Young traveller with a grin "
            "in their tone, always half a step ahead and pleased about it. Androgynous, "
            "energetic, warm."),
    "isolde": ("A soft, distant female voice, as if heard through a wall or from another "
               "room. Gentle and sorrowful, unhurried, with a faint hollow quality. "
               "Never loud — presence without volume."),
    "rook": ("A blunt, sardonic female voice, low and rough-edged. Mercenary captain who "
             "has said everything twice already. Dry humour, flat delivery, "
             "no patience for ceremony."),
}

# 每个角色的语速微调（配合人设节奏）
VOICE_TUNING: dict[str, dict] = {
    "lucien":    {"speed": 0.92, "pitch": -1},   # 慢、低
    "seraphine": {"speed": 0.98, "pitch": 0},    # 平稳
    "ilias":     {"speed": 1.02, "pitch": 1},    # 略轻快
    "nyx":       {"speed": 1.08, "pitch": 2},    # 快、亮
    "isolde":    {"speed": 0.90, "pitch": 0},    # 更慢、飘
    "rook":      {"speed": 0.96, "pitch": -2},   # 沉、糙
}

# 兜底音色（角色无专属音色时使用）
FALLBACK_VOICE = "English_expressive_narrator"


def voice_for(role_name: str) -> str:
    return CHARACTER_VOICES.get((role_name or "").lower(), FALLBACK_VOICE)


def tuning_for(role_name: str) -> dict:
    return VOICE_TUNING.get((role_name or "").lower(), {"speed": 1.0, "pitch": 0})
