#!/usr/bin/env python3
"""
FlOZE 复刻 —— 用 MiniMax M2-her 替换竞品自研模型的调用层

设计目标：把 FlOZE 的产品数据模型，映射到 MiniMax M2-her 的消息协议，
使三方模型可以承载"角色扮演 + 长期记忆 + 多档位"这套产品逻辑。

映射关系（依据 APK / Web 逆向得到的真实结构）：
    FlOZE role（角色卡）        → system
    FlOZE persona（用户人设）    → user_system
    FlOZE scenario（情景）       → group
    FlOZE 记忆(diary/summary)    → 追加进 system
    FlOZE chatMode（档位）       → temperature / max_completion_tokens / 示例对话

用法：
    export MINIMAX_API_KEY=...        # 或写入同目录 .minimax_key
    python3 minimax_rp_client.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = "M2-her"                      # 注意：不是 MiniMax-M2-her
MAX_COMPLETION_TOKENS_CAP = 2048      # M2-her 官方上限


# ---------------------------------------------------------------- 凭证

def load_api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY")
    if key:
        return key.strip()
    f = Path(__file__).resolve().parent.parent / ".minimax_key"
    if f.exists():
        return f.read_text().strip()
    sys.exit("未找到 API key：请设置 MINIMAX_API_KEY 或创建 .minimax_key")


# ---------------------------------------------------------------- FlOZE 数据模型

@dataclass
class VesperineRole:
    """对应 FlOZE 的 role 实体（/role/*，Creator Studio 可编辑）"""
    name: str
    description: str                  # 角色简介
    personality: str = ""             # 性格（对应官方「最多 10 个 personality traits」）
    background: str = ""              # 背景故事（官方上限 3000 字符）
    first_message: str = ""           # 开场白（官方上限 1000 字符）
    status_bar: str = ""              # 可选状态栏（如 📍地点 / 💖好感度）

    def to_system(self) -> str:
        parts = [f"You are {self.name}.", self.description]
        if self.personality:
            parts.append(f"Personality: {self.personality}")
        if self.background:
            parts.append(f"Background: {self.background}")
        if self.status_bar:
            parts.append(
                f"Append this status bar at the end of every reply:\n{self.status_bar}"
            )
        parts.append(
            "Stay fully in character. Portray actions and expressions with "
            "asterisks. Never break character or mention being an AI."
        )
        return "\n\n".join(p for p in parts if p)


@dataclass
class VesperinePersona:
    """对应 FlOZE 的 persona（用户自称身份，/persona/*）"""
    name: str
    description: str

    def to_user_system(self) -> str:
        return f"The user is {self.name}. {self.description}"


@dataclass
class Memory:
    """对应 FlOZE 的记忆系统（chat diary + starred summaries + 结构化 Memories）"""
    diary: list[str] = field(default_factory=list)     # 聊天日记
    summaries: list[str] = field(default_factory=list)  # 星标摘要
    facts: dict[str, str] = field(default_factory=dict)  # 「你怎么称呼我」等结构化记忆

    def to_block(self) -> str:
        if not (self.diary or self.summaries or self.facts):
            return ""
        lines = ["[Long-term memory — treat as established facts]"]
        if self.facts:
            lines.append("Key facts:")
            lines += [f"- {k}: {v}" for k, v in self.facts.items()]
        if self.summaries:
            lines.append("Starred story moments:")
            lines += [f"- {s}" for s in self.summaries]
        if self.diary:
            lines.append("Recent diary entries:")
            lines += [f"- {d}" for d in self.diary]
        return "\n".join(lines)


@dataclass
class ChatMode:
    """
    对应 FlOZE 的聊天模式档位（Web bundle chatMode store 实测：
    模型档位 standard / quick / smooth / sparkline；
    情景类型 novel / short_talk / story）。
    这里映射为推理参数 + 输出长度 + 示例对话引导。
    """
    mode_id: str
    temperature: float
    max_tokens: int
    style_hint: str = ""
    examples: list[tuple[str, str]] = field(default_factory=list)


# 按 FlOZE 的档位语义重建（Lite=便宜短，Epic=长输出）
CHAT_MODES: dict[str, ChatMode] = {
    "lite": ChatMode("lite", 0.85, 160,
                     "Keep replies short and conversational."),
    "quick": ChatMode("quick", 0.90, 220,
                      "Reply briefly and naturally, like instant messaging."),
    "classic": ChatMode("classic", 1.00, 500,
                        "Balanced, immersive narration."),
    "smooth": ChatMode("smooth", 1.05, 700,
                       "Write in a romantic, novel-like tone."),
    "novel": ChatMode("novel", 1.05, 900,
                      "Write as literary narrative prose."),
    "story": ChatMode("story", 1.10, 900,
                      "Advance the plot actively; introduce new developments."),
    "epic": ChatMode("epic", 1.10, 1400,
                     "Write long, richly detailed, immersive responses."),
}


# ---------------------------------------------------------------- 客户端

class MiniMaxRPClient:
    def __init__(self, api_key: str, model: str = MODEL):
        self.api_key = api_key
        self.model = model
        self.usage = {"prompt": 0, "completion": 0, "calls": 0}
        self.sensitive_hits = 0

    def _post(self, payload: dict, timeout: int = 90) -> dict:
        req = urllib.request.Request(
            f"{API_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}") from None

    def build_messages(self, role: VesperineRole, persona: VesperinePersona | None,
                       memory: Memory | None, mode: ChatMode,
                       history: list[tuple[str, str]]) -> list[dict]:
        msgs: list[dict] = []
        system = role.to_system()
        if mode.style_hint:
            system += f"\n\nStyle: {mode.style_hint}"
        if memory:
            blk = memory.to_block()
            if blk:
                system += f"\n\n{blk}"
        msgs.append({"role": "system", "name": role.name, "content": system})

        # 注意：非标准 role（user_system / sample_message_* / group）必须携带
        # name 字段，否则 API 返回 400 业务码 2013。实测确认。
        if persona:
            msgs.append({"role": "user_system", "name": persona.name,
                         "content": persona.to_user_system()})

        for u, a in mode.examples:
            msgs.append({"role": "sample_message_user",
                         "name": persona.name if persona else "User", "content": u})
            msgs.append({"role": "sample_message_ai",
                         "name": role.name, "content": a})

        for speaker, text in history:
            if speaker == "user":
                msgs.append({"role": "user", "content": text})
            else:
                msgs.append({"role": "assistant", "name": role.name, "content": text})
        return msgs

    def chat(self, role: VesperineRole, persona: VesperinePersona | None,
             memory: Memory | None, mode: ChatMode,
             history: list[tuple[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": self.build_messages(role, persona, memory, mode, history),
            "temperature": mode.temperature,
            "top_p": 0.95,
            "max_completion_tokens": min(mode.max_tokens, MAX_COMPLETION_TOKENS_CAP),
            "stream": False,
        }
        data = self._post(payload)
        if "choices" not in data:
            raise RuntimeError(f"异常响应: {json.dumps(data, ensure_ascii=False)[:400]}")

        u = data.get("usage", {})
        self.usage["prompt"] += u.get("prompt_tokens", 0)
        self.usage["completion"] += u.get("completion_tokens", 0)
        self.usage["calls"] += 1
        # MiniMax 的前置/后置审核标记 —— 合规监控点
        if data.get("input_sensitive") or data.get("output_sensitive"):
            self.sensitive_hits += 1
            print("  ⚠️  [审核标记] input_sensitive=%s output_sensitive=%s type=%s" % (
                data.get("input_sensitive"), data.get("output_sensitive"),
                data.get("output_sensitive_type")))

        return data["choices"][0]["message"]["content"]

    def cost_estimate(self) -> float:
        """按 M2-her 量级估费（$0.30/$1.20 per 1M，实际以账单为准）"""
        return (self.usage["prompt"] * 0.30 + self.usage["completion"] * 1.20) / 1_000_000


# ---------------------------------------------------------------- Demo

def demo():
    key = load_api_key()
    client = MiniMaxRPClient(key)

    # —— 用 FlOZE 的真实产品结构构造（对应 Creator Studio 的可填字段）——
    role = VesperineRole(
        name="Lucien",
        description="A brooding vampire duke who rules a decaying moonlit estate.",
        personality="Restrained, elegant, possessive, secretly tender",
        background=("Once a celebrated general, Lucien was turned during a war he lost. "
                    "For three centuries he has kept the estate frozen in the night "
                    "his fiancée died, refusing to let time move forward."),
        status_bar="📍 Moonlit Hall | 💖 Affection: 12% | 🩸 Thirst: 40%",
    )
    persona = VesperinePersona(
        name="Elara",
        description=("A mortal cartographer hired to map the estate. Bold, curious, "
                     "and unafraid of things that should frighten her."),
    )
    memory = Memory(
        diary=["Day 1: Elara arrived at the estate and refused to leave despite warnings."],
        summaries=["Lucien admitted the frozen night is a memorial, not a curse."],
        facts={"How Elara addresses him": "Your Grace",
               "How Lucien addresses her": "little cartographer"},
    )

    turns = [
        ("lite", "*I step into the moonlit hall* You asked to see me?"),
        ("smooth", "Why do you keep the clocks stopped at this hour?"),
        ("epic", "Then stop hiding behind the memorial. Let the night move again."),
    ]

    print("=" * 72)
    print("FlOZE 复刻 · MiniMax M2-her 替换自研模型 —— 多档位实测")
    print("=" * 72)
    print(f"角色: {role.name} | 用户: {persona.name} | 模型: {client.model}")

    history: list[tuple[str, str]] = []
    for mode_id, user_msg in turns:
        mode = CHAT_MODES[mode_id]
        history.append(("user", user_msg))
        t0 = time.time()
        reply = client.chat(role, persona, memory, mode, history)
        dt = time.time() - t0
        history.append(("assistant", reply))

        print(f"\n{'─' * 72}")
        print(f"[{mode_id:7s}] {persona.name}: {user_msg}")
        print(f"          {role.name}: {reply}")
        print(f"           ({len(reply)} 字符 · {dt:.1f}s)")

    print(f"\n{'=' * 72}")
    print(f"调用 {client.usage['calls']} 次 | "
          f"input {client.usage['prompt']} tok / output {client.usage['completion']} tok")
    print(f"估算成本 ≈ ${client.cost_estimate():.6f} | 审核命中: {client.sensitive_hits}")
    print("=" * 72)


if __name__ == "__main__":
    demo()
