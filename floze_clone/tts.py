"""
FlOZE 复刻 —— 语音合成（MiniMax T2A）

对齐 FlOZE 的语音能力：
    POST /message/speech/generate   台词转语音（按需，非每条都生成）
    GET  /tts/vocal/list            可用音色列表

设计要点：
  1. **按需生成** —— 不是每条消息都转语音。原品如此（独立端点），
     且 TTS 计费按字符（$60-100/M），全量生成成本不可接受。
  2. **动作描写不朗读** —— 消息里的 *...* 是舞台指示，不是台词。
     直接送去合成会让 AI 把"他转身看向窗外"也念出来。
  3. **本地缓存** —— 同一段文本+音色只合成一次，落盘复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request

import voices as V

API = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
T2A_ENDPOINT = API.rstrip("/") + "/t2a_v2"

# 模型选择：hd 质量更好（$100/M），turbo 更快更便宜（$60/M）
TTS_MODEL = os.environ.get("MINIMAX_TTS_MODEL", "speech-2.8-turbo")

CACHE_DIR = os.path.join(os.path.dirname(__file__), "static", "speech")


def api_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "").strip()


# ------------------------------------------------------------------ 文本清洗

_ACTION = re.compile(r"\*[^*]*\*")          # *动作描写*
_PAREN = re.compile(r"\((?:[^()]*)\)")       # （旁白）
_QUOTES = "\"'“”‘’"


def speech_text(message: str) -> str:
    """
    从一条角色消息里提取「应该被朗读的部分」。

    规则：
      - 去掉 *动作描写*
      - 去掉括号旁白
      - 去掉引号（朗读时不该念出引号本身）
      - 折叠空白
    """
    if not message:
        return ""
    t = _ACTION.sub(" ", message)
    t = _PAREN.sub(" ", t)
    for q in _QUOTES:
        t = t.replace(q, "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ------------------------------------------------------------------ 合成

def _cache_path(text: str, voice_id: str) -> str:
    h = hashlib.sha1(f"{voice_id}|{TTS_MODEL}|{text}".encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"{h}.mp3")


def synthesize(message: str, role_name: str, *, force: bool = False) -> dict:
    """
    把一条角色消息合成语音。返回 {ok, path, url, cached, chars, error}。

    无 API key 时返回 ok=False（上层降级为不提供语音）。
    """
    text = speech_text(message)
    if not text:
        return {"ok": False, "error": "no speakable text"}

    voice_id = V.voice_for(role_name)
    tune = V.tuning_for(role_name)

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(text, voice_id)
    if os.path.exists(path) and not force:
        return {"ok": True, "path": path,
                "url": "/speech/" + os.path.basename(path),
                "cached": True, "chars": len(text)}

    if not api_key():
        return {"ok": False, "error": "no api key"}

    payload = {
        "model": TTS_MODEL,
        "text": text,
        "stream": False,
        "language_boost": "auto",
        "output_format": "hex",
        "voice_setting": {
            "voice_id": voice_id,
            "speed": tune.get("speed", 1.0),
            "vol": 1,
            "pitch": tune.get("pitch", 0),
        },
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000,
            "format": "mp3", "channel": 1,
        },
    }
    req = urllib.request.Request(
        T2A_ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:160]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}

    if d.get("base_resp", {}).get("status_code") != 0:
        return {"ok": False, "error": d.get("base_resp", {}).get("status_msg", "t2a failed")}

    audio_hex = (d.get("data") or {}).get("audio") or ""
    if not audio_hex:
        return {"ok": False, "error": "empty audio"}

    try:
        raw = bytes.fromhex(audio_hex)
    except ValueError:
        return {"ok": False, "error": "bad hex"}

    with open(path, "wb") as f:
        f.write(raw)

    info = d.get("extra_info", {})
    return {
        "ok": True, "path": path,
        "url": "/speech/" + os.path.basename(path),
        "cached": False,
        "chars": info.get("usage_characters", len(text)),
        "duration_ms": info.get("audio_length", 0),
        "size": len(raw),
    }


def list_voices() -> dict:
    """拉取可用音色（系统音色 + 已定制的角色音色）。"""
    if not api_key():
        return {"system": [], "custom": []}
    payload = {"voice_type": "system"}
    req = urllib.request.Request(
        API.rstrip("/") + "/get_voice", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        sysv = d.get("system_voice") or []
        return {
            "system": [{"id": v.get("voice_id"), "name": v.get("voice_name")}
                       for v in sysv],
            "custom": [{"role": k, "id": v} for k, v in V.CHARACTER_VOICES.items()],
        }
    except Exception:
        return {"system": [], "custom": [{"role": k, "id": v}
                                        for k, v in V.CHARACTER_VOICES.items()]}
