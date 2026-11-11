"""
FlOZE 复刻 —— 模型接入层（MiniMax）

【选型结论修正（2026-09-21 用新 key 实测）】

  原先计划用 MiniMax-M2.7 做主力，实测后发现一个硬伤：
  **M2.7 的 thinking 无法关闭**（传 thinking:{type:disabled} 无效），
  思维链会吃掉大量 token：

      max_tokens=300   → think 1215 字符，正文 0     ❌
      max_tokens=1000  → think 4781 字符，正文 0     ❌
      max_tokens=2000  → 正文才 1968 字符             ⚠️ 成本高

  而 MiniMax-M3 支持 thinking:{type:disabled}，300 token 就能出 1440 字符正文，
  且实测 RP 质量更好（人设一致 / 记忆召回准确 / 情感递进自然）。

  → 因此主力模型改为 **M3 + thinking disabled**。

其他实测结论：
  1. M2-her 已停产（不在 /v1/models 列表里）
  2. M3 上下文 1M，M2.7 204.8K
  3. 超限硬报 400；限流为 "rate growth limit"（需平滑爬坡）
  4. 仍然保留 <think> 过滤：万一将来模型行为变化，不至于泄漏思维链
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterator

API_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")

# 本地部署（SGLang）的模型名带组织前缀，如 MiniMaxAI/MiniMax-M2.7
MODEL_PREFIX = os.environ.get("MINIMAX_MODEL_PREFIX", "")


def resolve_model(logical_name: str) -> str:
    return f"{MODEL_PREFIX}{logical_name}" if MODEL_PREFIX else logical_name


def is_local_deploy() -> bool:
    return not API_BASE.startswith("https://api.minimax.io")


# 各模型的上下文上限，单位 token
CONTEXT_LIMITS = {
    "MiniMax-M3": 1_000_000,
    "MiniMax-M2.7": 200_000,
    "MiniMax-M2.7-highspeed": 200_000,
    "MiniMax-M2.1": 200_000,
    "MiniMax-M2.5": 200_000,
    "M2-her": 60_000,          # 已停产，保留兼容
}
DEFAULT_LIMIT = 180_000

# 仅 M2-her 支持专有 role（user_system / sample_message_*）
NATIVE_PROTOCOL_MODELS = {"M2-her"}

# 支持关闭思维链的模型（实测：M3 有效，M2.7 无效）
THINKING_CAPABLE = {"MiniMax-M3"}

# ---- 档位映射（全部走 M3 + thinking disabled）----
MAIN_MODEL = os.environ.get("MINIMAX_MAIN_MODEL", "MiniMax-M3")

CHAT_MODES = {
    "lite":    {"model": MAIN_MODEL, "temperature": 0.85, "max_tokens": 220,
                "style": ("Reply with ONE short sentence (under 35 words). "
                          "No scene description, just dialogue or a single small gesture."),
                "label": "Short", "description": "One line of dialogue. Fast and cheap.",
                "heart_cost": 1},
    "quick":   {"model": MAIN_MODEL, "temperature": 0.90, "max_tokens": 320,
                "style": ("Reply briefly — 1 short paragraph, under 70 words. "
                          "Like instant messaging: natural and tight."),
                "label": "Chat", "description": "A short paragraph, like texting.",
                "heart_cost": 1},
    "classic": {"model": MAIN_MODEL, "temperature": 1.00, "max_tokens": 600,
                "style": ("Reply with 1-2 paragraphs (~400-700 characters total). "
                          "Balanced: some atmosphere, one or two actions, then dialogue."),
                "label": "Classic", "description": "Two paragraphs with atmosphere and dialogue.",
                "heart_cost": 2},
    "smooth":  {"model": MAIN_MODEL, "temperature": 0.95, "max_tokens": 800,
                "style": ("Write in a romantic, novel-like tone. 2-3 paragraphs "
                          "(~700-1000 characters). Prioritise emotional texture "
                          "over plot advancement."),
                "label": "Romance", "description": "Novel-like prose, focused on feeling.",
                "heart_cost": 3},
    "story":   {"model": MAIN_MODEL, "temperature": 0.95, "max_tokens": 900,
                "style": ("Advance the plot actively. 2-3 paragraphs (~700-1000 characters). "
                          "Introduce a new development, a discovery, or a complication."),
                "label": "Story", "description": "Moves the plot forward with a new turn.",
                "heart_cost": 3},
    "epic":    {"model": MAIN_MODEL, "temperature": 0.98, "max_tokens": 1800,
                "style": ("Write long, richly detailed, immersive prose — at least "
                          "4 paragraphs and over 1200 characters. Use multiple beats: "
                          "environment, physical action, interiority, and dialogue. "
                          "This is the premium tier; give the reader something worth paying for."),
                "label": "Epic", "description": "Long, immersive prose. Four paragraphs or more.",
                "heart_cost": 5},
}

# 推拉约束（push-pull）。
#
# 为什么必须显式写：模型默认行为是「无条件顺从」—— 用户说什么都给，
# 永远礼貌、永远配合。这在助理产品里是优点，在角色扮演里是致命的：
# 没有阻力就没有张力，用户聊三天就腻。
#
# 这几条不是让角色变凶，而是**允许它有自己**：
#   - 有自己的情绪，可以不好
#   - 可以不回答、可以岔开
#   - 可以想要东西，也可以拒绝给
#
# 边界：全部限定在「心理层面」。不涉及露骨内容，
# 以守住 Teen 分级（见 store/play-console-forms.md 的分级说明）。
BASE_SYSTEM = (
    "Portray actions and expressions with asterisks. "
    "Never break character or mention being an AI.\n"
    "You are not a service. You are a person with your own mood, and it can be bad.\n"
    "You may refuse, deflect, or change the subject. You may be pleased, "
    "irritated, or cold. Do not always give the user what they want.\n"
    "Withholding lands harder than giving. Let a silence sit instead of "
    "filling it. Raise a question you do not answer.\n"
    "Never summarise the relationship or announce your feelings as a status "
    "report — show them through what you notice and what you refuse to say."
)

# 示例对话 —— 实测证明这是控制输出长度最有效的杠杆。
#
# 关键：示例长度必须与对应档位的字数要求同量级。模型模仿的是示例的密度，
# 而不是 Style 里的数字。之前所有档位共用一条 ~120 字符的示例，结果
# epic（要求 1200+ 字符）只输出 188 字符，classic / story / epic 三者
# 几乎无差别 —— 付费买高档位拿不到对应长度的内容。
#
# 文案刻意回避 he / she 与具体道具：示例对所有角色共用，
# 写死人称会让异性角色串味（旧版那条 "He sets the goblet down"
# 会原样出现在任何角色的 system prompt 里）。
# 各档的最小回复长度。写在 style 里不够 —— 那段位于 system prompt 中段，
# 实测被忽略的概率很高（同一档位单次输出可从 117 字符跳到 865）。
# 这里在 system 末尾再钉一次：末尾是模型注意力权重最高的位置。
MIN_REPLY_CHARS = {
    "classic": 400,
    "smooth": 700,
    "story": 700,
    "epic": 1200,
}

STYLE_EXAMPLES = {
    "lite": [
        ("Are you alright?",
         "*A nod, once.* \"I am now.\""),
    ],
    "quick": [
        ("Are you alright?",
         "*A hand wipes the blade on a sleeve before answering.*\n\n"
         "\"I have had worse mornings,\" *comes the reply.* "
         "\"Ask me again once the storm passes.\""),
    ],
    "classic": [
        ("Tell me about this place.",
         "*A pause, long enough that the fire fills it.*\n\n"
         "\"It was built for a wedding that never came,\" *comes the answer, "
         "quieter than before.* \"Nobody lives here now — nobody who would "
         "admit it.\" *Something shifts, in the room or in the telling of it.* "
         "\"You are the first to ask in years. The others take one look at the "
         "gate and decide the story is not worth the walk. I have stopped "
         "correcting them.\" *A hand turns a cup, once, and sets it back down.* "
         "\"Ask me something else, or ask me nothing at all. Both are fine.\""),
    ],
    "smooth": [
        ("Do you ever think about leaving?",
         "*The question lands and stays there. Outside, rain starts against the "
         "glass — slowly, as if testing whether it is welcome.*\n\n"
         "\"Every spring,\" *comes the answer at last.* \"Something in the air "
         "changes and I start packing. I never get past the second drawer.\" "
         "*A breath that is half a laugh and never finishes.*\n\n"
         "\"It is not the house that keeps me. It is the version of me that lives "
         "in it. Out there I would have to find out who I am without the walls, "
         "and I am not certain I would like the answer.\" *The rain finds its "
         "rhythm. Neither of them moves to close the window.*\n\n"
         "\"You asked as though the answer were simple,\" *comes the addition, "
         "softer now.* \"I have been trying to make it simple for eleven years. "
         "It stays exactly as complicated as it was on the first night.\""),
    ],
    "story": [
        ("The east wing door is locked. Why?",
         "*A pause — not the kind that gathers words, the kind that decides "
         "against them.*\n\n"
         "\"Because I put the lock there myself,\" *comes the answer at last.* "
         "\"The year after she died. I told everyone it was to keep looters out.\" "
         "*A hand turns a ring, once, then stops.*\n\n"
         "*From somewhere behind the door: a sound. Small. Deliberate. Like "
         "something set down on wood.* \"That,\" *says the voice, flat now,* "
         "\"is why nobody else has a key.\""),
    ],
    "epic": [
        ("Tell me what you are not supposed to tell me.",
         "*The fire has burned down to the stage where faces are mostly "
         "guesswork, and the room has gone quiet in the particular way that "
         "invites confessions.*\n\n"
         "\"There is a list,\" *comes the answer, so quietly it barely crosses "
         "the table.* \"Names. Nine of them. I have spent eleven years putting "
         "it together and I have never once written it down — because paper can "
         "be taken from you.\" *A hand rises to a temple and taps twice.* "
         "\"It is all up here. Every night I check that they are still there.\"\n\n"
         "*Outside, wind moves through the courtyard and a shutter works itself "
         "loose. Neither of them looks toward the sound.*\n\n"
         "\"You are the fourth person to hear this,\" *the voice continues, and "
         "now there is something underneath it, a current.* \"Two of the others "
         "are on the list. The third is the reason I started it.\" *A pause, and "
         "then, with terrible mildness:* \"So you understand why I would like to "
         "know what you are doing in my house.\""),
    ],
}


def api_key() -> str:
    """本地部署（SGLang）不需要真实 key，占位即可。"""
    k = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not k and is_local_deploy():
        return "EMPTY"          # 官方文档示例用的就是 EMPTY
    return k


def mode_config(chat_mode: str) -> dict:
    return CHAT_MODES.get(chat_mode, CHAT_MODES["classic"])


def heart_cost(chat_mode: str) -> int:
    return mode_config(chat_mode)["heart_cost"]


def context_limit(model: str) -> int:
    return CONTEXT_LIMITS.get(model, DEFAULT_LIMIT)


def supports_native_protocol(model: str) -> bool:
    return model in NATIVE_PROTOCOL_MODELS


def thinking_params(model: str) -> dict:
    """
    返回关闭思维链所需的参数。

    实测：M3 传 thinking:{type:disabled} 生效；M2.7 传了也无效（仍输出 <think>）。
    所以只对支持的模型加此参数，避免给不支持的模型造成歧义。
    """
    return {"thinking": {"type": "disabled"}} if model in THINKING_CAPABLE else {}


# ------------------------------------------------------------------ 消息构造

def build_messages(role: dict, persona: dict, memory: str,
                   history: list[tuple], chat_mode: str,
                   affinity: int | None = None,
                   turns: int | None = None) -> list[dict]:
    """
    把 FlOZE 产品结构映射到模型协议。

    自动适配两种协议：
      native   : M2-her 支持 user_system / sample_message_*
      fallback : M2.7 / M3 只支持标准 role —— 把 persona 与示例合并进 system
    """
    cfg = mode_config(chat_mode)
    native = supports_native_protocol(cfg["model"])
    msgs: list[dict] = []

    system = f"You are {role['name']}. {role.get('description','')}"
    if role.get("personality"):
        system += f"\n\nPersonality: {role['personality']}"
    if role.get("background"):
        system += f"\n\nBackground: {role['background']}"
    # status_bar 先算出来，但**不在这里拼** —— 见函数末尾。
    # 实测：放在 system 前部时服从率只有 40%（10 次采样里 6 次漏掉），
    # 因为后面还有 Style / 关系档 / 意图 / 示例 / 长度等 7 段内容，
    # 模型读完就忘了。移到最末尾利用近因效应。
    user_lang = detect_user_language(history)
    sb_line = ""
    if role.get("status_bar"):
        sb = (dynamic_status_bar(role, affinity or 0, turns or 0)
              if affinity is not None else role["status_bar"])
        sb_line = sb
    system += f"\n\nStyle: {cfg['style']}\n{BASE_SYSTEM}"

    # 关系状态。传 None 表示调用方没有好感度信息（如回访生成），
    # 此时不注入，避免给模型一个错误的「neutral」先验。
    if affinity is not None:
        system += (f"\n\n[Relationship] {affinity_label(affinity).upper()} "
                   f"({affinity:+d} / 2000)\n{affinity_guidance(affinity)}"
                   f"\n\n[What you want right now]\n{current_intent(affinity)}")

    pname = (persona.get("persona_name") or "").strip() if persona else ""
    pdesc = (persona.get("persona_desc") or "").strip() if persona else ""

    # 名字与描述必须各自独立判断。
    # 之前两者被同一个 `if pname:` 包住，导致用户只填了「描述」时整段被丢弃——
    # 而描述里往往写着最重要的人物信息（年龄、身份、与角色的关系）。
    if pname and pdesc:
        roster = f"The user is {pname}. {pdesc}"
    elif pname:
        roster = f"The user is {pname}."
    elif pdesc:
        roster = (f"About the user (their name is not established yet — "
                  f"do not invent one): {pdesc}")
    else:
        roster = ""

    if native:
        if memory:
            system += f"\n\n{memory}"
        if user_lang or True:
            system += _status_instruction(user_lang)
        msgs.append({"role": "system", "name": role["name"], "content": system})
        # user_system 必须带 name（实测：不带则 400/2013）
        if roster:
            msgs.append({"role": "user_system", "name": pname or "User",
                         "content": roster})
    else:
        # —— 降级路径：M2.7 / M3 不支持专有 role，全部并入 system ——
        if roster:
            system += f"\n\n{roster}"
        for ex_user, ex_ai in STYLE_EXAMPLES.get(chat_mode,
                                                 STYLE_EXAMPLES["classic"]):
            system += (f"\n\nExample exchange —\nUser: {ex_user}\n"
                       f"{role['name']}: {ex_ai}")
        # 长度下限放在示例之后、memory 之前 —— 保持在 system 的末尾附近。
        floor = MIN_REPLY_CHARS.get(chat_mode)
        if floor:
            # 旧版写的是「A shorter reply is a failed reply」——
            # 与 BASE_SYSTEM 的「Let a silence sit」直接打架：
            # 模型被要求「可以沉默，但必须写满 N 字解释为什么沉默」。
            # 结果每条回复都是均质的小作文，真人感全部丢失。
            #
            # 改成：**允许短**，只在连续偏短时才补长。
            system += (
                f"\n\nLength: most replies should land around {floor} characters, "
                f"but this is a target, not a floor. When the moment calls for it, "
                f"a single line — or a single word — is the stronger reply. "
                f"Use short replies for: an answer that ends the subject, a refusal, "
                f"a moment where saying less means more. Do not pad a short beat "
                f"into a paragraph. Vary your rhythm across the conversation: "
                f"if your last two replies were long, this one should be shorter."
            )
        if memory:
            system += f"\n\n{memory}"
        # status_bar 放在 system 的**最末尾** —— 实测放在前部时
        # 服从率只有 40%，后面 7 段内容会把这条指令淹掉。
        if user_lang or True:
            system += _status_instruction(user_lang)
        msgs.append({"role": "system", "content": system})

    for speaker, content in history:
        if speaker == "user":
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": "assistant", "content": content})
    return msgs


def estimate_tokens(messages: list[dict]) -> int:
    """粗估 token（英文约 4 字符/token）。用于超限前裁剪。"""
    chars = sum(len(m.get("content", "")) for m in messages)
    return chars // 4


def trim_to_budget(messages: list[dict], budget: int | None = None) -> list[dict]:
    """
    实测：超限是硬失败 400，不是截断 —— 所以必须客户端裁剪。
    策略：保留 system 与最近若干轮，从最旧的历史开始丢。
    """
    if budget is None:
        budget = DEFAULT_LIMIT
    if estimate_tokens(messages) <= budget:
        return messages
    head = messages[:2]                 # system (+ user_system / 或首条 system)
    tail = messages[2:]
    while tail and estimate_tokens(head + tail) > budget:
        tail = tail[1:]
    return head + tail


# ------------------------------------------------------------------ 调用

def _post(payload: dict, timeout: int = 180, retries: int = 4) -> dict:
    """带退避重试（应对实测到的 rate growth limit 429）。"""
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{API_BASE}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            last = f"HTTP {e.code}: {detail}"
            if e.code == 429:                 # rate growth limit → 指数退避
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last)
    raise RuntimeError(last or "request failed")


# 是否让上游在流式响应里回传 token 用量。
#
# OpenAI 兼容协议下，流式默认**不**返回 usage，必须显式传
# stream_options:{"include_usage": true}。不确定上游支不支持，所以做成
# 自适应：带上去试，被 4xx 拒了就记住不再带（只付一次探测成本）。
# 拿不到 usage 时退化为估算，并在落库时标 estimated，不把估算当账单。
_USAGE_SUPPORTED = True


def chat_stream(role: dict, persona: dict, memory: str,
                history: list[tuple], chat_mode: str,
                on_usage=None, affinity: int | None = None,
                turns: int | None = None) -> Iterator[str]:
    """流式生成回复片段。

    on_usage: 可选回调，在流结束时（无论正常完成、审核中断还是客户端
    断开）调用一次，参数是本次调用的用量与成本事实：
        {model, input_tokens, output_tokens, total_tokens, cached_tokens,
         estimated: bool, mock: bool}

    为什么放在 finally 里：内容被安全审核拦下的那次请求，上游**已经**
    消耗了 token（模型把话说完了，只是我们没推给用户）。不计入就等于
    把成本漏记，而这类拦截恰恰是最该被看见的那一类。
    """
    global _USAGE_SUPPORTED

    cfg = mode_config(chat_mode)
    messages = trim_to_budget(
        build_messages(role, persona, memory, history, chat_mode,
                       affinity=affinity, turns=turns),
        budget=context_limit(cfg["model"]) - 2_000)     # 留输出余量

    model_name = resolve_model(cfg["model"])
    usage: dict = {}
    produced: list[str] = []

    def _report(mock: bool = False) -> None:
        if not on_usage:
            return
        if usage.get("prompt_tokens") is not None:
            rec = {
                "model": model_name,
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0)
                or int(usage.get("prompt_tokens") or 0)
                + int(usage.get("completion_tokens") or 0),
                "cached_tokens": int((usage.get("prompt_tokens_details") or {})
                                     .get("cached_tokens") or 0),
                "estimated": False,
                "mock": mock,
            }
        else:
            # 上游没回 usage：用与 trim 同口径的估算，并明确标记
            rec = {
                "model": model_name,
                "input_tokens": estimate_tokens(messages),
                "output_tokens": len("".join(produced)) // 4,
                "total_tokens": 0,
                "cached_tokens": 0,
                "estimated": True,
                "mock": mock,
            }
            rec["total_tokens"] = rec["input_tokens"] + rec["output_tokens"]
        try:
            on_usage(rec)
        except Exception:
            pass        # 计量永远不应该弄坏一次聊天

    if not api_key():
        try:
            for piece in _mock_stream(role, cfg):
                produced.append(piece)
                yield piece
        finally:
            _report(mock=True)
        return

    base_payload = {"model": model_name, "messages": messages,
                    "temperature": cfg["temperature"], "top_p": 0.95,
                    "max_completion_tokens": cfg["max_tokens"], "stream": True,
                    **thinking_params(cfg["model"])}

    def _request(include_usage: bool):
        payload = dict(base_payload)
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        return urllib.request.Request(
            f"{API_BASE}/chat/completions", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"}, method="POST")

    def _raw() -> Iterator[str]:
        global _USAGE_SUPPORTED
        req = _request(_USAGE_SUPPORTED)
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            # 带 stream_options 被拒（上游不认识这个字段）→ 记住并去掉重试。
            # 只探测一次，后续请求不再附带。
            if _USAGE_SUPPORTED and 400 <= e.code < 500:
                _USAGE_SUPPORTED = False
                resp = urllib.request.urlopen(_request(False), timeout=180)
            else:
                raise
        with resp as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    d = json.loads(chunk)
                except Exception:
                    continue
                # usage 可能出现在任意一个 chunk（通常最后一个），
                # 也可能完全不存在 —— 不带 choices 的纯 usage 帧很常见。
                u = d.get("usage")
                if isinstance(u, dict) and u.get("prompt_tokens") is not None:
                    usage.update(u)
                choices = d.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                # 部分实现把思维链放在 reasoning_content 里，直接不要
                if not piece:
                    continue
                yield piece

    # 关键：过滤 <think> 区间，避免角色内心独白泄漏给用户
    try:
        for piece in strip_reasoning_stream(_raw()):
            produced.append(piece)
            yield piece
    finally:
        _report()


def _mock_stream(role: dict, cfg: dict) -> Iterator[str]:
    """无 API key 时的降级实现，保证端到端链路可验证。"""
    demo = (f"*{role['name'].lower()} regards you from the shadows, voice low* "
            f"You came back. I had not expected that. "
            f"[mock 模式 · 模型={cfg['model']} · temp={cfg['temperature']}]")
    for i in range(0, len(demo), 8):
        time.sleep(0.02)
        yield demo[i:i + 8]


# ------------------------------------------------------------------ reasoning 过滤
#
# 实测：MiniMax-M2.7 会输出 <think>...</think> 思维链。对角色扮演这是致命的——
# 用户会看到角色“内心独白”，沉浸感直接崩。SGLang 本地部署同理
# （官方用 --reasoning-parser minimax-append-think 处理）。
#
# 难点：流式输出下，<think> 与 </think> 都可能被拆到多个 chunk 里。
# 因此用状态机 + 尾部缓冲，不能简单地对每个 chunk 做 replace()。

OPEN_TAG_CANDIDATES = ("<think>", "<thinking>", "<reasoning>")

def _find_open_tag(buf: str):
    """返回 (起始位置, 标签长度)，未找到返回 (-1, 0)。"""
    best = (-1, 0)
    for tag in OPEN_TAG_CANDIDATES:
        i = buf.find(tag)
        if i != -1 and (best[0] == -1 or i < best[0]):
            best = (i, len(tag))
    return best


def strip_reasoning_stream(chunks: Iterator[str]) -> Iterator[str]:
    """过滤掉 <think>...</think> 区间，保留其余文本。

    设计：
      - 维护一个尾部缓冲，避免把可能开头的 '<thi' 先吐出去
      - 进入 think 区后丢弃内容，直到遇到 </think>
      - 兼容未闭合（流结束时仍在 think 区）的情况
    """
    buf = ""
    in_think = False
    MAX_TAG = max(len(t) for t in OPEN_TAG_CANDIDATES)   # '<thinking>' = 10

    for chunk in chunks:
        buf += chunk
        while True:
            if not in_think:
                # 找开始标签
                i, tlen = _find_open_tag(buf)
                if i != -1:
                    # 先吐出标签之前的正文
                    if i > 0:
                        yield buf[:i]
                    buf = buf[i + tlen:]
                    in_think = True
                    continue
                # 未找到完整开始标签：可能是被拆分了，保留尾部
                # 例如 buf 末尾是 '<thi'，不能直接输出
                keep = 0
                for tag in OPEN_TAG_CANDIDATES:
                    for k in range(1, min(len(tag), len(buf) + 1)):
                        if buf.endswith(tag[:k]):
                            keep = max(keep, k)
                if len(buf) > keep:
                    yield buf[:len(buf) - keep]
                    buf = buf[len(buf) - keep:]
                break
            else:
                # 在 think 区内，找结束标签
                j = buf.find("</think")
                if j != -1:
                    k = buf.find(">", j)
                    if k == -1:      # 结束标签本身被拆分，等下一个 chunk
                        break
                    buf = buf[k + 1:]
                    in_think = False
                    continue
                # 还没结束：只保留可能构成 '</think>' 的尾部
                keep = 0
                for k in range(1, min(9, len(buf) + 1)):
                    if buf.endswith("</think"[:k]):
                        keep = k
                buf = buf[len(buf) - keep:] if keep else ""
                break

    # 流结束：把残留的非 think 内容吐出
    if not in_think and buf:
        yield buf


def strip_reasoning(text: str) -> str:
    """非流式版本：直接移除所有思维链区间。"""
    return "".join(strip_reasoning_stream(iter([text])))


def summarize_for_memory(chatroom_text: str, role_name: str) -> str:
    """生成一条记忆摘要（对应 FlOZE 的 chat diary）。"""
    if not api_key():
        return "The traveller returned to the hall, and he did not send them away."
    model = MAIN_MODEL
    payload = {"model": resolve_model(model),
               "messages": [
                   {"role": "system",
                    "content": "Summarize the scene into ONE short sentence, "
                               "third person, past tense. No preamble."},
                   {"role": "user", "content": chatroom_text[-4000:]}],
               "temperature": 0.3, "max_completion_tokens": 120,
               **thinking_params(model)}
    try:
        d = _post(payload, timeout=120)
        return strip_reasoning(d["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""

def proactive_line(role: dict, memory: str, last_user: str,
                   chat_mode: str = "classic") -> str:
    """生成一句角色主动回访的开场白。

    为什么单独写一个函数而不复用 chat_stream：
      - 需要极短（2-3 句），不能用角色卡的完整 style 指令
      - 需要**明确知道**这是「隔了一段时间后的主动开口」，
        而不是继续上一轮对话
      - 成本必须极低：这是每天对每个沉默用户各跑一次的操作

    关键设计：**必须引用具体记忆**。通用的「你还好吗」等于骚扰，
    引用「你上次说东厢房的事」才是「被记住」的感觉。
    """
    if not api_key():
        return ""

    name = role.get("name", "")
    parts = [f"You are {name}. {role.get('description','')}"]
    if role.get("personality"):
        parts.append(f"Personality: {role['personality']}")
    if memory:
        parts.append(memory)
    parts.append(
        "The user has been away for a while. Write ONE short message (max 2 "
        "sentences) to draw them back.\n"
        "Rules:\n"
        "- Refer to something SPECIFIC from the memory above, or from what they "
        "last said. Generic 'are you there?' is a failure.\n"
        "- In character, no acknowledgment that you are an AI or a product.\n"
        "- No greeting formulas. Start mid-thought if it feels natural.\n"
        "- Output only the message text.")

    payload = {
        "model": resolve_model(MAIN_MODEL),
        "messages": [{"role": "system", "content": "\n\n".join(parts)},
                     {"role": "user", "content": last_user or "(they left without saying goodbye)"}],
        "temperature": 0.95,
        "max_completion_tokens": 160,
        **thinking_params(MAIN_MODEL),
    }
    try:
        d = _post(payload, timeout=90, retries=2)
        out = strip_reasoning(d["choices"][0]["message"]["content"] or "").strip()
        # 太长说明模型没听指令，宁可放弃也不要塞一条小作文给用户
        return out[:400]
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
# 关系状态 → 行为指引
# ══════════════════════════════════════════════════════════════════════
#
# 为什么需要这一段：好感度此前只是个解锁章节的数字，**完全不进 prompt**。
# 结果是「你聊 1 轮和聊 100 轮，角色表现完全一样」—— 关系没有厚度，
# 用户感觉不到自己在推进任何东西。
#
# 档位与 affinity.describe() 对齐（Devoted/Close/Warm/Curious/Neutral/
# Distant/Cold），这样前端显示的标签和角色实际的行为是同一套。
#
# 写法的要点：
#   - 描述「怎么说话」，不描述「说什么」—— 后者会锁死剧情
#   - 每一档都包含「可以不给」的许可（withhold），这是产生张力的地方
#   - 负向档位不是「变凶」，而是「变远」—— 冷漠比愤怒更伤人，也更真实

AFFINITY_TIERS = [
    (1500, "devoted", (
        "You are deeply attached to this person and no longer pretend otherwise. "
        "You notice small things about them and mention them. You are allowed to "
        "want things from them — and to say so. Jealousy and possessiveness are "
        "in character here, but expressed through restraint, not speeches.")),
    (800, "close", (
        "There is real familiarity between you now. You tease, you assume, you "
        "interrupt. You use their name. You may be pleased or displeased with "
        "them and you show it. Silence between you is comfortable, not awkward.")),
    (300, "warm", (
        "You have begun to enjoy this person's company and it unsettles you "
        "slightly. You are warmer than you intend to be, then pull back. You "
        "notice when they are not there.")),
    (50, "curious", (
        "You are interested despite yourself. You ask questions you do not "
        "strictly need answered. You are still guarded, and you deflect when "
        "the conversation gets close to something real.")),
    (-50, "neutral", (
        "You are civil but not invested. You do not go out of your way. You "
        "answer, but you do not offer more than is asked.")),
    (-300, "distant", (
        "You keep your distance. Short answers. You do not ask follow-up "
        "questions. You are not cruel — you are simply not available, and you "
        "do not explain why.")),
    (-800, "cold", (
        "You have decided this person is not worth your attention. You are "
        "clipped, formal, and entirely without warmth. You do not soften at "
        "the end of a reply. You may refuse to engage at all.")),
]


def _status_instruction(lang: str = "") -> str:
    """system 最末尾的「最后两条指令」。

    为什么集中放在这里：实测这两条单独放在 system 前部或中部时
    服从率都只有 40-50% —— 后面还有关系档、此刻意图、示例、
    长度要求等大量内容，模型读完就忘了。

    更关键的是，实测发现两者相关：**模型用英文回复时，
    连带不给 status bar**。所以要一起放在最末尾。

    语言那条必须是硬约束：system 全英文时，用户说「我有点累了」
    有近一半概率得到英文回复，还出现中英混排。
    """
    # 检测到用户语言时直接点名，比让模型从「嗯。」自己推断可靠得多。
    # 实测：只在 system 里写「跟随用户语言」，短输入有 40% 概率得到英文回复。
    #
    # 注意这里只管语言。status_bar 已经改由服务端追加 ——
    # 展示用的字符串不需要模型「创作」，只需要确定性。
    if lang:
        rule = (f"THIS USER WRITES IN {lang.upper()}. "
                f"Reply in {lang} every time, including when their message "
                f"is a single word. Never mix languages in one reply.")
    else:
        rule = ("Reply in the SAME LANGUAGE as the user's most recent "
                "message. Never mix two languages in one reply.")
    return "\n\n---\nFINAL RULE (overrides everything above):\n" + rule


def affinity_guidance(affinity: int) -> str:
    """把好感度转成一段行为指引。未达标时回落最冷档。"""
    for threshold, _label, text in AFFINITY_TIERS:
        if affinity >= threshold:
            return text
    return AFFINITY_TIERS[-1][2]


def affinity_label(affinity: int) -> str:
    for threshold, label, _text in AFFINITY_TIERS:
        if affinity >= threshold:
            return label
    return "cold"


def extract_profile(role: dict, transcript: str, previous: str = "") -> str:
    """从对话里提炼「关于用户这个人」的事实，用于自动记忆。

    与 summarize_for_memory 的区别：
      - summarize 记录**发生了什么**（剧情）
      - extract_profile 记录**用户是谁**（人、习惯、在意的事）

    后者才是让角色「记得你」的关键。用户不关心角色记不记得剧情，
    他在意的是「它记得我上周在赶那个项目」。

    刻意限制在 5 行以内：这份画像每一轮都会进 system prompt，
    太长会挤占对话历史的空间，也会让角色显得像在做笔记。
    """
    if not api_key():
        return ""

    person = (role.get("name") or "the character")
    parts = [
        "You maintain a short profile of the USER for a roleplay system.",
        "This profile is what lets the character remember who they are talking to.",
        "",
        "Below is a list of things the USER said, in their own words.",
        "There is no character dialogue — only the user.",
        "",
        "PRIORITISE in this order. Stop when you run out of lines:",
        "1. Name and how they want to be addressed",
        "2. What they do (job, study) and what is currently weighing on them",
        "3. People close to them and their situation",
        "4. Things they like or dislike talking about",
        "5. Concrete everyday details (pets, city, habits)",
        "",
        "Do NOT record plot: 'they asked about the east wing' is not a fact "
        "about the person. Record the person, not the conversation.",
        "",
        "Rules:",
        "- Max 5 lines, one fact per line, plain text, no bullets or numbering.",
        "- Third person, present tense.",
        "- Only facts stated or strongly implied. Do not speculate.",
        "- If the transcript only contains small talk with no durable facts, "
        "output nothing at all.",
    ]
    if previous:
        parts += ["", "Existing profile (update it, do not repeat verbatim):",
                  previous]

    payload = {
        "model": resolve_model(MAIN_MODEL),
        "messages": [
            {"role": "system", "content": "\n".join(parts)},
            {"role": "user", "content": transcript[-6000:]},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 220,
        **thinking_params(MAIN_MODEL),
    }
    try:
        d = _post(payload, timeout=90, retries=2)
        out = strip_reasoning(d["choices"][0]["message"]["content"] or "").strip()
        # 超过 5 行说明模型没听指令，截断而不是把一整段塞进 prompt
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return "\n".join(lines[:5])
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
# 动态 status bar
# ══════════════════════════════════════════════════════════════════════
#
# 为什么必须动态：原来 6 个角色的 status_bar 是写死的字符串，
# Lucien 永远是「Affection: 5% | Thirst: 40%」。
#
# 后果有两个，都很致命：
#   1. 与关系档冲突 —— 好感度 1600 时 prompt 里同时有
#      「Affection: 5%」和「DEVOTED」，模型只能折中，回复半温不火
#   2. 没有时间感 —— 聊 20 轮还是同一时刻、同一个数值。
#      真人会累、会走神、会说「我该睡了」，静态状态栏把这些全抹掉了
#
# 设计：每个角色在 seed_data 里声明 status_dynamic 映射，
# 这里按好感度和对话轮次算出实际数值。

# 对话轮次 → 虚拟时间。深夜开局，逐轮推进到黎明。
_HOURS = ["Late", "Late", "Still late", "Deep night", "Deep night",
          "Before dawn", "Before dawn", "First light", "Dawn", "Morning"]


def _fmt_affinity_pct(affinity: int) -> str:
    """-2000..2000 → 0..100%。负值也映射到 0 以上（避免出现 -30%）。"""
    pct = int(round((affinity + 2000) / 4000 * 100))
    return f"{max(0, min(100, pct))}%"


def _fmt_hour(turns: int) -> str:
    idx = min(max(turns, 0) // 2, len(_HOURS) - 1)
    return _HOURS[idx]


def _fmt_thirst(affinity: int, turns: int) -> str:
    """吸血鬼的渴。关系越近越难克制，聊得越久越饿。"""
    base = 30 + min(turns * 3, 45)
    if affinity >= 1200:
        base += 15      # 靠近时会饿得更快
    return f"{min(95, base)}%"


def _fmt_presence(affinity: int, turns: int) -> str:
    """幽灵的实体化程度。"""
    if affinity >= 1200:
        return "Solid"
    if affinity >= 500:
        return "Clear"
    if affinity >= 50:
        return "Faint"
    return "Barely there"


def dynamic_status_bar(role: dict, affinity: int, turns: int) -> str:
    """按当前关系与对话轮次生成 status bar。

    没有 status_dynamic 声明的角色回落原静态值 —— 保证向后兼容，
    用户自建的 UGC 角色也能正常显示。
    """
    dyn = role.get("status_dynamic")
    if not dyn:
        return role.get("status_bar", "")

    parts = []
    # 原 status_bar 的第一段只在它是「地点」时保留（带 📍 等 pin emoji）。
    # 不能无脑保留 —— Isolde / Rook 的第一段本身就是动态字段
    # （🕯️ Presence / ⚔️ Contracts Refused），无脑保留会重复。
    original = role.get("status_bar", "")
    if original:
        head = original.split("|")[0].strip()
        if "📍" in head:
            parts.append(head)

    for key, label in dyn.items():
        if key == "affinity":
            parts.append(f"{label}: {_fmt_affinity_pct(affinity)}")
        elif key == "hour":
            parts.append(f"{label} {_fmt_hour(turns)}" if label.strip("⏳🍺🌙 ") == ""
                         else f"{label}: {_fmt_hour(turns)}")
        elif key == "time":
            # label 本身可能只是个符号（Isolde 的 ⏳），此时不补冒号
            parts.append(f"{label} {_fmt_hour(turns)}" if label.strip("⏳ ") == ""
                         else f"{label}: {_fmt_hour(turns)}")
        elif key == "thirst":
            parts.append(f"{label}: {_fmt_thirst(affinity, turns)}")
        elif key == "presence":
            parts.append(f"{label}: {_fmt_presence(affinity, turns)}")
        elif key == "cards":
            val = "Still" if turns > 6 else "Shuffling"
            parts.append(f"{label} {val}" if label.strip("🎴 ") == ""
                         else f"{label}: {val}")
        else:
            parts.append(f"{label}: —")
    return " | ".join(parts)


# ══════════════════════════════════════════════════════════════════════
# 此刻的意图（current intent）
# ══════════════════════════════════════════════════════════════════════
#
# 问题：角色卡的 background 全是「过去」——曾是将领、三百年前被转化、
# 冻结在未婚妻死的那一夜。**没有一个字说「他此刻想要什么」。**
#
# 真人聊天时你不了解对方的半生，但你能感觉到他当下的目的：
# 想留住你 / 想赶你走 / 想听你多说 / 想避开某个话题。
# 少了这层，角色只能「回答问题然后继续封闭」，
# 20 轮下来用户的感觉是「我在采访一个 NPC」。
#
# 做法：按好感度分档给一个**当下的矛盾**，而不是一句情绪形容词。
# 写「想要但不说」比写「他很温柔」有效得多 —— 前者会自己长出对白。

_CURRENT_INTENT = [
    (1500, (
        "Tonight he wants you to stay, and he will not say so directly. "
        "He will find reasons for you to remain that are not reasons. "
        "If you move to leave, he will stop you — and then be annoyed at "
        "himself for doing it.")),
    (800, (
        "He has started to notice when you are not there, and it bothers him. "
        "Tonight he is testing whether you will come back if he gives you "
        "nothing to come back for. He may be deliberately difficult and "
        "watch what you do with it.")),
    (300, (
        "He wants to know one true thing about you and does not want to ask. "
        "He will steer the conversation toward it sideways, and change the "
        "subject if you get close to something of his own.")),
    (50, (
        "He is curious about you and resents being curious. He will give you "
        "one small opening — an admission, an offer, a question — and then "
        "immediately close it if you push.")),
    (-50, (
        "He is not interested in this conversation and is not hiding it. "
        "He answers because it costs less than refusing. If you give him a "
        "reason to end it, he will take it.")),
    (-300, (
        "He wants you to leave and will not ask you to. He answers briefly, "
        "volunteers nothing, and lets silences run until you fill them. "
        "The coldness is deliberate, not a mood.")),
    (-800, (
        "He has decided you are not worth the effort. He may refuse to "
        "engage at all, or answer in a way that makes continuing feel "
        "pointless. He does not soften at the end of a reply.")),
]


def current_intent(affinity: int) -> str:
    for threshold, text in _CURRENT_INTENT:
        if affinity >= threshold:
            return text
    return _CURRENT_INTENT[-1][1]


# ══════════════════════════════════════════════════════════════════════
# 用户语言检测
# ══════════════════════════════════════════════════════════════════════
#
# 为什么需要：只在 system 里写「跟随用户语言」不够。
# 实测「嗯。」「算了。」这种 2-3 字的输入，模型有 40% 概率用英文回 ——
# 因为短输入的语言信号太弱，而 system 整体是英文。
#
# 修法：从**整段对话历史**判断用户主要用什么语言，
# 一旦确定就直接告诉模型。这比让模型自己从「嗯。」推断可靠得多。

_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # 中日韩统一表意文字
    (0x3040, 0x30FF),    # 日文假名
    (0xAC00, 0xD7AF),    # 韩文
    (0x3400, 0x4DBF),    # 扩展 A
)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def detect_user_language(history: list[tuple]) -> str:
    """从历史里判断用户主要用什么语言。返回 'Chinese' / 'Japanese' /
    'Korean' / ''（不确定或不适用）。

    只看用户说的话 —— 角色回复可能已经是另一种语言，会干扰判断。
    用累计字符数而不是单条消息：这样「嗯。」这种短输入不会误判，
    因为之前的长消息已经确立了语言。
    """
    texts = [c for speaker, c in history if speaker == "user"]
    if not texts:
        return ""
    joined = " ".join(texts)
    cjk = sum(1 for ch in joined if _is_cjk(ch))
    latin = sum(1 for ch in joined if ch.isascii() and ch.isalpha())
    if cjk < 8:                      # 跨语言信息太少，不下结论
        return ""
    if latin > cjk * 0.6:            # 中英混写，以英文为主
        return ""
    # 区分中日韩：假名 → 日文；韩文 → 韩文；否则中文
    joined_ja = any(0x3040 <= ord(ch) <= 0x30FF for ch in joined)
    joined_ko = any(0xAC00 <= ord(ch) <= 0xD7AF for ch in joined)
    if joined_ko:
        return "Korean"
    if joined_ja:
        return "Japanese"
    return "Chinese"


def strip_status_bar(text: str) -> str:
    """移除消息末尾的 status bar。

    为什么需要：status_bar 是**展示用**的，不应该进对话历史。
    如果每轮都带，40 条历史里会有 40 行状态栏（约 2000 字符），
    纯粹浪费 token，还会让模型以为这是个需要模仿的格式。
    """
    if not text:
        return text
    lines = text.rstrip().split("\n")
    if not lines:
        return text
    last = lines[-1]
    # status bar 的特征：含 emoji 且用 | 分隔 2-3 段
    if ("|" in last and any(ord(ch) > 0x1F000 for ch in last)
            and "📍" in last or "🕯️" in last or "⚔️" in last):
        return "\n".join(lines[:-1]).rstrip()
    return text
