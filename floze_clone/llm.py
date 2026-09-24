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
                "heart_cost": 1},
    "quick":   {"model": MAIN_MODEL, "temperature": 0.90, "max_tokens": 320,
                "style": ("Reply briefly — 1 short paragraph, under 70 words. "
                          "Like instant messaging: natural and tight."),
                "heart_cost": 1},
    "classic": {"model": MAIN_MODEL, "temperature": 1.00, "max_tokens": 600,
                "style": ("Reply with 1-2 paragraphs (~400-700 characters total). "
                          "Balanced: some atmosphere, one or two actions, then dialogue."),
                "heart_cost": 2},
    "smooth":  {"model": MAIN_MODEL, "temperature": 1.05, "max_tokens": 800,
                "style": ("Write in a romantic, novel-like tone. 2-3 paragraphs "
                          "(~700-1000 characters). Prioritise emotional texture "
                          "over plot advancement."),
                "heart_cost": 3},
    "story":   {"model": MAIN_MODEL, "temperature": 1.10, "max_tokens": 900,
                "style": ("Advance the plot actively. 2-3 paragraphs (~700-1000 characters). "
                          "Introduce a new development, a discovery, or a complication."),
                "heart_cost": 3},
    "epic":    {"model": MAIN_MODEL, "temperature": 1.10, "max_tokens": 1800,
                "style": ("Write long, richly detailed, immersive prose — at least "
                          "4 paragraphs and over 1200 characters. Use multiple beats: "
                          "environment, physical action, interiority, and dialogue. "
                          "This is the premium tier; give the reader something worth paying for."),
                "heart_cost": 5},
}

BASE_SYSTEM = (
    "Portray actions and expressions with asterisks. "
    "Never break character or mention being an AI."
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
                   history: list[tuple], chat_mode: str) -> list[dict]:
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
    if role.get("status_bar"):
        system += (f"\n\nAppend this status bar at the end of every reply:\n"
                   f"{role['status_bar']}")
    system += f"\n\nStyle: {cfg['style']}\n{BASE_SYSTEM}"

    pname = persona.get("persona_name") if persona else ""

    if native:
        if memory:
            system += f"\n\n{memory}"
        msgs.append({"role": "system", "name": role["name"], "content": system})
        # user_system 必须带 name（实测：不带则 400/2013）
        if pname:
            msgs.append({"role": "user_system", "name": pname,
                         "content": f"The user is {pname}. "
                                    f"{persona.get('persona_desc','')}"})
    else:
        # —— 降级路径：M2.7 / M3 不支持专有 role，全部并入 system ——
        if pname:
            system += (f"\n\nThe user is {pname}. "
                       f"{persona.get('persona_desc','')}")
        for ex_user, ex_ai in STYLE_EXAMPLES.get(chat_mode,
                                                 STYLE_EXAMPLES["classic"]):
            system += (f"\n\nExample exchange —\nUser: {ex_user}\n"
                       f"{role['name']}: {ex_ai}")
        if memory:
            system += f"\n\n{memory}"
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


def chat_stream(role: dict, persona: dict, memory: str,
                history: list[tuple], chat_mode: str) -> Iterator[str]:
    """流式生成回复片段。"""
    cfg = mode_config(chat_mode)
    messages = trim_to_budget(
        build_messages(role, persona, memory, history, chat_mode),
        budget=context_limit(cfg["model"]) - 2_000)     # 留输出余量

    if not api_key():
        yield from _mock_stream(role, cfg)
        return

    payload = {"model": resolve_model(cfg["model"]), "messages": messages,
               "temperature": cfg["temperature"], "top_p": 0.95,
               "max_completion_tokens": cfg["max_tokens"], "stream": True,
               **thinking_params(cfg["model"])}
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"}, method="POST")

    def _raw() -> Iterator[str]:
        with urllib.request.urlopen(req, timeout=180) as r:
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
                choices = d.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                # 部分实现把思维链放在 reasoning_content 里，直接不要
                if not piece:
                    continue
                yield piece

    # 关键：过滤 <think> 区间，避免角色内心独白泄漏给用户
    yield from strip_reasoning_stream(_raw())


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
