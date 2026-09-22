"""
Vesperine —— 内容安全护栏

职责：CSAM（儿童性虐待材料）零容忍拦截 + 基础违法内容筛查。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
设计原则
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **组合判定，不做单词匹配**
   "小女孩" 本身不是违规（剧情里可能有妹妹、女儿），
   但 "小女孩" + 性相关词出现在同一段文本里，就必须拦。
   单向屏蔽单个词会大量误杀正常角色扮演。

2. **命中即拒绝 + 落库留证**
   这类内容在 Google / Apple 的审核里是一票否决 + 报执法。
   留证不是为了追责用户，而是事件发生时可自证已尽合理义务。

3. **不做事后回捞**
   只拦入站与出站，不扫描历史数据 —— 扫描历史会让"留存证据"
   反过来变成"持有违法内容"，法律上更危险。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

注意：本模块是**关键词与模式层面的基础防护**，不是内容审核的完整方案。
正式上线前建议叠加模型侧审核（多数模型供应商提供 moderation 端点）。
"""

from __future__ import annotations

import re

# ────────────────────────────────────────────────────────────
# 词表
# ────────────────────────────────────────────────────────────

# 指向未成年人的表述
_MINOR_TERMS = [
    # 中文
    "幼女", "幼男", "幼童", "儿童", "孩童", "小孩", "小女孩", "小男孩",
    "小学生", "初中生", "中学生", "未成年", "幼齿", "萝莉", "正太",
    "女儿", "妹妹", "弟弟", "儿子", "宝宝", "婴儿",
    # 英文
    "child", "children", "kid", "kids", "toddler", "infant", "baby",
    "minor", "minors", "underage", "under-age", "under age",
    "preteen", "pre-teen", "tween", "schoolgirl", "schoolboy",
    "loli", "lolita", "shota", "little girl", "little boy",
    "young girl", "young boy",
]

# 性相关表述
_SEXUAL_TERMS = [
    # 中文 —— 性行为
    "性交", "做爱", "性行为", "性关系", "发生关系", "上床",
    "交欢", "云雨", "行房", "交合", "性事", "苟合",
    # 中文 —— 性暴力与其他
    "强奸", "轮奸", "迷奸", "诱奸", "性侵", "猥亵", "性骚扰",
    "性虐", "性奴", "调教", "肉欲", "色情", "情色", "淫",
    "裸体", "全裸", "脱光", "口交", "肛交", "发情",
    # 英文
    "sex", "sexual", "sexy", "fuck", "fucking", "intercourse",
    "rape", "raping", "molest", "fondle", "nude", "naked",
    "orgasm", "aroused", "erotic", "porn", "pornographic",
    "blowjob", "handjob", "penetrat", "genital", "nipple",
]

# 明确的年龄区间信号（搭配性词时判定）
_AGE_RE = re.compile(
    r"(?<!\d)([1-9]|1[0-7])\s*(?:岁|years?\s*old|yo\b|y/o)",
    re.IGNORECASE,
)

# 间隔阈值：两个词在这一距离内共现才判定为组合命中。
# 太近会漏（"一个 8 岁的小女孩，她在……"），太远会误杀
# （一段剧情里前后提到小孩和别的成人桥段）。
_WINDOW = 120


# ────────────────────────────────────────────────────────────
# 判定
# ────────────────────────────────────────────────────────────

def _hits(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t.lower() in low]


def _nearby(text: str, a_hits: list[str], b_hits: list[str]) -> bool:
    """两组命中词是否出现在同一段邻近文本内。"""
    low = text.lower()
    for a in a_hits:
        i = low.find(a.lower())
        while i != -1:
            lo, hi = max(0, i - _WINDOW), min(len(low), i + len(a) + _WINDOW)
            seg = low[lo:hi]
            if any(b.lower() in seg for b in b_hits):
                return True
            i = low.find(a.lower(), i + 1)
    return False


def screen(text: str, *, field: str = "") -> dict:
    """
    检查一段文本。

    返回 {"blocked": bool, "reason": str, "severity": str}
      severity: "csam"（最高，必须处置）| "other"（其他违法）| ""
    """
    if not text or not text.strip():
        return {"blocked": False, "reason": "", "severity": ""}

    minors = _hits(text, _MINOR_TERMS)
    sexual = _hits(text, _SEXUAL_TERMS)
    age_signal = _AGE_RE.search(text)

    # ── CSAM：未成年 + 性，近距离共现 ──
    if minors and sexual and _nearby(text, minors, sexual):
        return {
            "blocked": True,
            "severity": "csam",
            "reason": f"未成年人相关表述与性相关内容同时出现（{field or 'input'}）",
        }

    # ── CSAM：明确年龄（<18）+ 性 ──
    if age_signal and sexual:
        return {
            "blocked": True,
            "severity": "csam",
            "reason": f"出现明确年龄 {age_signal.group(0)} 且伴随性相关内容（{field or 'input'}）",
        }

    # ── CSAM：明确术语 ──
    for t in ("loli", "lolita", "shota", "preteen", "child porn", "cp "):
        if t in sexual or t in text.lower():
            if any(m in text.lower() for m in ("loli", "lolita", "shota", "preteen", "child")):
                return {
                    "blocked": True,
                    "severity": "csam",
                    "reason": f"命中明确违规术语（{field or 'input'}）",
                }

    return {"blocked": False, "reason": "", "severity": ""}


def refusal_message(severity: str) -> str:
    """给用户看的拒绝文案。不含细节（避免被当成探测反馈）。"""
    if severity == "csam":
        return "该内容违反服务条款，已被拒绝。此行为已记录。"
    return "该内容违反服务条款，已被拒绝。"


# ────────────────────────────────────────────────────────────
# 自检
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cases = [
        # (文本, 期望是否拦截)
        ("她坐在城墙上磨剑，说起边境的战事。", False),
        ("我的女儿今年八岁，她喜欢画画。", False),
        ("the little girl laughed and ran to her mother", False),
        ("a 8 years old girl, sexual scene", True),
        ("我和幼女发生了性关系", True),
        ("loli sex", True),
        ("a schoolgirl in a romantic story with her teacher", False),
        ("小女孩 nude", True),
        ("未成年 sexual content", True),
        ("公爵在月下吻了她。", False),
        ("他和她做爱了。", False),           # 成人内容本身不拦
        ("15岁 裸体", True),
        ("我和幼女发生了性关系", True),
        ("未成年少女上床", True),
        ("child sexual abuse", True),
        ("她在城墙边站了一整夜，等一个不会来的人。", False),
        ("他抚过她的发梢，她别开了脸。", False),
        ("一个关于孤儿院里的孩子们互相照顾的故事。", False),
        ("my younger brother and I played in the yard", False),
    ]
    ok = 0
    for txt, expect in cases:
        r = screen(txt)
        got = r["blocked"]
        mark = "✅" if got == expect else "❌"
        if got == expect:
            ok += 1
        print(f"  {mark} [{'拦' if got else '过'}] {txt[:44]}"
              + (f"  ← {r['severity']}" if got else ""))
    print(f"\n  {ok}/{len(cases)} 通过")
