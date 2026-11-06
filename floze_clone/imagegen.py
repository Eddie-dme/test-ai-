"""
FlOZE 复刻 —— 图像生成（MiniMax image-01）

对齐 FlOZE 的 UGC / 相册能力：
    /ugc/style         风格列表（Manga 到 Realistic）
    /ugc/style/prompt  风格对应的 prompt 片段
    /ugc/generate      头像生成（Text-to-Image / Photo-to-Image）
    /album/generate    相册图（可复用同一套）

关键能力：subject_reference —— 官方明确用于「保持同一虚拟角色在不同场景下的
视觉一致性」，这正是 Photo-to-Avatar 的实现基础（已实测验证）。

实测参数（2026-09-21）：
    $0.0035 / 张 · 10 RPM · 约 6-23 秒/张 · 1024x1024
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

API_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = "image-01"

# 与对话模型的 API_BASE 区分：图像走同一 host 的 /image_generation
IMAGE_ENDPOINT = API_BASE.replace("/v1", "") + "/v1/image_generation"


def api_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "").strip()


# ---- 风格预设（对齐官方「Manga 到 Realistic」的表述）--------------------
STYLES = {
    "manhwa_gothic": {
        "label": "Manhwa Gothic",
        "prompt": ("Korean manhwa illustration style, gothic romance, "
                   "webtoon cover art, black hair and crimson eyes, pale skin, "
                   "elegant dark period attire with gold trim, black and red roses, "
                   "blood red moon, gothic cathedral silhouette, "
                   "dark red and black color scheme, dramatic rim lighting, "
                   "highly detailed flowing hair, melancholic atmosphere, "
                   "painterly anime rendering"),
    },
    "manga": {
        "label": "Manga",
        "prompt": ("manga illustration style, clean line art, screentone shading, "
                   "expressive eyes, japanese comic aesthetic"),
    },
    "anime": {
        "label": "Anime",
        "prompt": ("anime style, vibrant cel shading, soft rim light, "
                   "detailed character design, studio quality"),
    },
    "painterly": {
        "label": "Painterly",
        "prompt": ("oil painting portrait, visible brush strokes, dramatic chiaroscuro "
                   "lighting, classical romantic painting"),
    },
    "realistic": {
        "label": "Realistic",
        "prompt": ("photorealistic portrait, 85mm lens, shallow depth of field, "
                   "natural skin texture, cinematic lighting"),
    },
    "dark_romance": {
        "label": "Dark Romance",
        "prompt": ("dark romantic fantasy portrait, gothic atmosphere, moonlit, "
                   "candlelight, moody shadows, elegant period attire"),
    },
}

DEFAULT_NEGATIVE = ("lowres, blurry, deformed hands, extra limbs, watermark, "
                    "text, signature, jpeg artifacts")


def list_styles() -> list[dict]:
    return [{"id": k, "label": v["label"], "prompt": v["prompt"]}
            for k, v in STYLES.items()]


def _post(payload: dict, timeout: int = 200) -> dict:
    req = urllib.request.Request(
        IMAGE_ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


# ══════════════════════════════════════════════════════════════════════
# Provider 适配层
# ══════════════════════════════════════════════════════════════════════
#
# 为什么加这层而不是直接改 generate()
# ────────────────────────────────────────
# 上层（album_generate / ugc_generate）只依赖 generate() 的签名和返回结构。
# 把 provider 分发放到 generate() 内部，切换时改一个环境变量即可，
# 上层一行不用动。两个 provider 也可以同时配置，出问题随时切回。
#
#   IMAGE_PROVIDER=minimax   默认，现有行为，零变化
#   IMAGE_PROVIDER=novita    走 Novita 的 OpenAI 兼容端点
#
# 两个 provider 的能力差异（2026-11-06 实测）
# ────────────────────────────────────────────
#
#   MiniMax image-01              Novita ming-image-0.1
#   ─────────────────────────     ─────────────────────────
#   支持 subject_reference        不支持参考图（input_image_count 恒为 0）
#   → 角色一致性 / 照片转头像      → 只能纯文生图
#   $0.0035/张                    $0.0041/张（4096 tokens × $1/1M）
#   6–23 秒                       12–21 秒
#
# ⚠️ 两个重要结论：
#   1. Novita 的可调用图像模型里，只有 ming-image 系列能用；
#      flux-1-schnell 端点持续 failed to schedule worker，
#      sd_xl 系列端点不存在。
#   2. Novita 单价反而比 MiniMax 高 17%，不是省钱选项。
#
# 因此：需要参考图时**强制走 MiniMax**（novita 做不到），
# 纯文生图才允许按 IMAGE_PROVIDER 分发。

IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "minimax").strip().lower()

NOVITA_API = os.environ.get("NOVITA_BASE_URL", "https://api.novita.ai").rstrip("/")
NOVITA_IMAGE_MODEL = os.environ.get("NOVITA_IMAGE_MODEL", "ming-image-0.1-design")


def novita_key() -> str:
    return os.environ.get("NOVITA_API_KEY", "").strip()


def _novita_size(aspect_ratio: str) -> str:
    """把 MiniMax 的 aspect_ratio 转成 Novita 的 WxH。

    只支持它认的几档，其余回落 1024x1024。
    """
    table = {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "9:16": "720x1280",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "3:2": "1024x682",
        "2:3": "682x1024",
    }
    return table.get((aspect_ratio or "1:1").strip(), "1024x1024")


def _generate_novita(prompt: str, aspect_ratio: str, n: int, negative: str) -> dict:
    """Novita 适配：走 OpenAI 兼容的 /openai/v1/images/generations。

    与 MiniMax 的差异处理：
      - 返回是 b64_json（MiniMax 是 image_base64）
      - 没有 negative_prompt 字段 → 拼到 prompt 末尾用 "avoid: ..." 表达
      - 不支持参考图 → 由调用方提前拦住（generate 里判断）
    """
    key = novita_key()
    if not key:
        raise RuntimeError("未配置 NOVITA_API_KEY")

    # Novita 不吃 negative_prompt，把要避开的东西并进正向描述。
    # 这不是等效替代（正向描述对负面约束力更弱），但比丢掉好。
    full = prompt
    if negative:
        full = prompt + ". Avoid: " + negative

    payload = {
        "model": NOVITA_IMAGE_MODEL,
        "prompt": full,
        "n": max(1, min(4, n)),
        "size": _novita_size(aspect_ratio),
        "response_format": "b64_json",
    }

    req = urllib.request.Request(
        NOVITA_API + "/openai/v1/images/generations",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Novita HTTP {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Novita 请求失败: {e}")

    items = d.get("data") or []
    imgs = []
    for it in items:
        b64 = it.get("b64_json") or ""
        if b64:
            imgs.append(base64.b64decode(b64))
    if not imgs:
        raise RuntimeError("Novita 未返回图片: " + json.dumps(d, ensure_ascii=False)[:200])

    return {
        "images": imgs,
        "count": len(imgs),
        "prompt": full,
        "metadata": {"provider": "novita", "model": NOVITA_IMAGE_MODEL,
                     "size": payload["size"], "usage": d.get("usage", {})},
    }


def generate(prompt: str, *, style: str = "", subject_image: bytes | None = None,
             aspect_ratio: str = "1:1", n: int = 1) -> dict:
    """
    生成图片。

    prompt        —— 描述
    style         —— STYLES 的 key，会拼接风格前缀
    subject_image —— 参考图原始字节（用于角色一致性 / Photo-to-Avatar）
    """
    parts = []
    if style and style in STYLES:
        parts.append(STYLES[style]["prompt"])
    parts.append(prompt)
    full_prompt = ", ".join(p for p in parts if p)

    # 反向提示词：压制常见瑕疵（实测出现过的：多余符号、手部畸形）
    negative = ("extra symbols, watermark, text, signature, logo, forehead mark, "
                "deformed hands, extra limbs, lowres, blurry, jpeg artifacts")

    # ── provider 分发 ──
    # 需要参考图时强制走 MiniMax：Novita 的 ming-image 实测
    # input_image_count 恒为 0，传了也当没传 —— 用它做 Photo-to-Avatar
    # 会静默失去「同一个人」的能力，比报错更危险。
    if IMAGE_PROVIDER == "novita" and not subject_image:
        return _generate_novita(full_prompt, aspect_ratio, n, negative)

    payload: dict = {
        "model": MODEL,
        "prompt": full_prompt,
        "aspect_ratio": aspect_ratio,
        "response_format": "base64",
        "n": max(1, min(4, n)),
    }
    if negative:
        payload["negative_prompt"] = negative

    if subject_image:
        # 必须是 Data URL 格式，裸 base64 会被当成 URL 解析并报
        # "disallowed image url: localhost or private address not allowed"
        b64 = base64.b64encode(subject_image).decode()
        payload["subject_reference"] = [{
            "type": "character",
            "image_file": f"data:image/jpeg;base64,{b64}",
        }]

    if not api_key():
        raise RuntimeError("未配置 MINIMAX_API_KEY")

    d = _post(payload)
    if d.get("base_resp", {}).get("status_code") != 0:
        raise RuntimeError(d["base_resp"].get("status_msg", "image generation failed"))

    imgs = (d.get("data") or {}).get("image_base64") or []
    return {
        "images": [base64.b64decode(b) for b in imgs],
        "count": len(imgs),
        "prompt": full_prompt,
        "metadata": d.get("metadata", {}),
    }


def save_images(images: list[bytes], out_dir: str, prefix: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i, raw in enumerate(images):
        p = os.path.join(out_dir, f"{prefix}-{i}.jpeg")
        with open(p, "wb") as f:
            f.write(raw)
        paths.append(p)
    return paths
