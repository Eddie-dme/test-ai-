"""
FlOZE 复刻 —— Affinity（好感度）评估

作用：把「用户说了什么」转化为 Affinity 变化量，驱动隐藏章节解锁。
对齐官方说明：Affinity 范围 -2000 ~ 2000，分正/负向里程碑，
同一个角色的 Affinity 跨所有会话合并计算。

两种模式：
  heuristic —— 默认，零成本、低延迟。基于情感词与行为模式打分。
  model     —— 有 MINIMAX_API_KEY 时启用，由模型判断细微情感（更贴近 FlOZE 做法）。

模型调用是额外的 token 成本，因此设计为「可选 + 变更时缓存」。
"""

from __future__ import annotations

import json
import os
import re

# ---- 启发式词表 ------------------------------------------------------
# 说明：这是刻意保守的起步实现。真实的 RP 产品应该用模型评估，
# 因为「微妙的线索」正是这类产品体验的核心（也是 FlOZE 用户抱怨最多的地方）。

POSITIVE_PATTERNS = [
    (r"\b(thank you|thanks|grateful|appreciate)\b", 3, "gratitude"),
    (r"\b(please|may I|would you)\b", 1, "politeness"),
    (r"\b(trust|believe in|rely on)\b", 4, "trust"),
    (r"\b(care about|worried about|are you okay|are you alright)\b", 5, "caring"),
    (r"\b(beautiful|wonderful|amazing|lovely)\b", 2, "praise"),
    (r"\b(I understand|I see why|that must have been)\b", 4, "empathy"),
    (r"\b(stay|remain|with you|not leaving|won't leave)\b", 5, "commitment"),
    (r"\b(remember|I recall|you told me)\b", 3, "attentiveness"),
    (r"\b(kind|gentle|warm)\b", 2, "warmth"),
    (r"\*[^*]*\b(reach|take .{0,12}hand|embrace|hug|touch)\b[^*]*\*", 3, "physical_warmth"),
]

NEGATIVE_PATTERNS = [
    (r"\b(shut up|silence|go away|leave me|get out)\b", -5, "hostility"),
    (r"\b(hate|despise|disgusting|pathetic)\b", -6, "contempt"),
    (r"\b(liar|you lied|betray|traitor)\b", -7, "betrayal_accusation"),
    (r"\b(whatever|I don't care|don't care)\b", -3, "indifference"),
    (r"\b(stupid|idiot|useless|worthless)\b", -5, "insult"),
    (r"\b(no|never|refuse|won't)\b.*\b(you|your)\b", -2, "refusal"),
    (r"\*[^*]*\b(slap|strike|push .{0,10}away|turn away|walk out)\b[^*]*\*", -4, "physical_rejection"),
]

# 单条消息的变动上限（避免一句话把好感拉满/打穿）
DELTA_CAP = 12


def _heuristic(text: str) -> tuple[int, list[str]]:
    low = text.lower()
    total = 0
    reasons: list[str] = []
    for pat, w, tag in POSITIVE_PATTERNS:
        if re.search(pat, low):
            total += w
            reasons.append(f"+{w}:{tag}")
    for pat, w, tag in NEGATIVE_PATTERNS:
        if re.search(pat, low):
            total += w
            reasons.append(f"{w}:{tag}")
    # 疑问句通常表示关注
    if "?" in text and total >= 0:
        total += 1
        reasons.append("+1:curiosity")
    total = max(-DELTA_CAP, min(DELTA_CAP, total))
    return total, reasons


PROMPT = (
    "You are an affect scorer for a roleplay chat. Given ONE user message to a "
    "roleplay character, output ONLY a JSON object:\n"
    '{"delta": <integer -12..12>, "reason": "<4 words max>"}\n'
    "Guidance: warmth, curiosity, care, keeping promises, remembering details -> positive. "
    "Hostility, contempt, betrayal, indifference, walking away -> negative. "
    "Neutral small talk -> 0. Be restrained; most messages are 0 to +3."
)


def _model(text: str) -> tuple[int, str]:
    """用模型评估（需要 MINIMAX_API_KEY）。失败时回退 0（由调用方回退启发式）。"""
    import llm

    # 重要：必须用主模型（M3）并关闭 thinking。
    # 早期版本硬编码 M2.7 + 60 token 预算，而 M2.7 的 thinking 无法关闭，
    # 会把 token 全吃光 → 解析失败 → delta 永远是 0。
    model = llm.MAIN_MODEL
    payload = {"model": llm.resolve_model(model),
               "messages": [{"role": "system", "content": PROMPT},
                            {"role": "user", "content": text[:1500]}],
               "temperature": 0.2, "max_completion_tokens": 200,
               **llm.thinking_params(model)}
    try:
        d = llm._post(payload, timeout=60)
        raw = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        raw = llm.strip_reasoning(raw)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return 0, "unparsed"
        obj = json.loads(m.group(0))
        delta = int(obj.get("delta", 0))
        delta = max(-DELTA_CAP, min(DELTA_CAP, delta))
        return delta, str(obj.get("reason", ""))[:40]
    except Exception:
        return 0, "model_error"


def evaluate(text: str, mode: str | None = None) -> tuple[int, str]:
    """
    返回 (delta, reason)。

    mode: 'heuristic' | 'model' | None
      None 时自动：有 API key 用 model 模式，否则用 heuristic。
      注意：model 模式失败会**回退到 heuristic**，而不是直接返回 0 ——
      否则一旦模型侧出问题，好感度会静默停止增长。
    """
    if not text.strip():
        return 0, "empty"

    if mode is None:
        mode = "model" if os.environ.get("MINIMAX_API_KEY", "").strip() else "heuristic"

    if mode == "model":
        d, r = _model(text)
        if r not in ("model_error", "unparsed"):
            return d, r
        # 模型失败 → 回退启发式（不静默归零）
        d2, reasons = _heuristic(text)
        return d2, f"fallback:{reasons}"

    d, reasons = _heuristic(text)
    return d, ",".join(reasons) or "neutral"


def describe(affinity: int) -> str:
    """把数值转成人类可读档位，用于前端展示。"""
    if affinity >= 1500:
        return "Devoted"
    if affinity >= 800:
        return "Close"
    if affinity >= 300:
        return "Warm"
    if affinity >= 50:
        return "Curious"
    if affinity > -50:
        return "Neutral"
    if affinity > -300:
        return "Distant"
    if affinity > -800:
        return "Cold"
    return "Hostile"
