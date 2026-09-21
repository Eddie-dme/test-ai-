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
