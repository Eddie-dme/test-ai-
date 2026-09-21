#!/usr/bin/env python3
"""
FlOZE 复刻 —— 角色立绘批量生成

固化经过多轮调试的 prompt 方案。重新生成肖像时用它，避免丢失调好的措辞。

用法：
    export MINIMAX_API_KEY=...
    python3 gen_portraits.py            # 生成全部角色
    python3 gen_portraits.py Lucien     # 只生成指定角色

调优历程（记录踩过的坑）：
  1. 参考素材（豆包生成的哥特插画）视觉过重：血月+满屏玫瑰+尖塔，
     缩到移动端列表里糊成一团 → 改为「半身像 + 简化背景」
  2. 第一版太"肖像化"，导致模型画成大头照、眼神呆滞（恐怖谷）
     → 加 "expressive vivid eyes with clear highlights, alive and present gaze"
  3. 出现伪文字（模型生成的乱码汉字）→ 负面词加 text/kanji/calligraphy
  4. 「下颌的疤」被画成面部血痕 → 负面词加 face scar/blood/wound
  5. 女性角色（Seraphine）会漂向写实风 → 必须强化
     "Japanese anime key visual, 2D illustration, flat cel shading,
      absolutely not photorealistic" + 负面词 photorealistic/semi-realistic/3d
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request

from seed_data import ROLES

ENDPOINT = "https://api.minimax.io/v1/image_generation"
OUT_DIR = os.path.join(os.path.dirname(__file__), "static", "generated")

# ---- 统一基础风格（保证 4 张风格一致）----------------------------------
BASE = ("Japanese anime key visual, 2D illustration, flat cel shading, clean lineart, "
        "visual novel character art, Korean webtoon style, absolutely not photorealistic, "
        "mature adult character, expressive vivid eyes with clear highlights, "
        "upper body composition with breathing room, highly detailed hair, "
        "dramatic cinematic rim lighting, moody atmospheric background with soft bokeh, "
        "painterly anime rendering")

# ---- 反向提示词（压制实测出现过的问题）--------------------------------
NEGATIVE = ("photorealistic, semi-realistic, realistic skin texture, 3d render, cgi, "
            "hyperrealistic, photograph, "
            "text, kanji, chinese characters, calligraphy, signature, watermark, logo, seal, "
            "face scar, blood, wound, injury, "
            "child, teenager, baby face, chibi, dead eyes, empty stare, doll-like, "
            "lowres, blurry, deformed hands, extra limbs, "
            "extra symbols, forehead mark")

# ---- 各角色差异化（只换这段，配色用于列表辨识）------------------------
SPECS: dict[str, str] = {
    "Lucien": (
        "a brooding vampire duke, long black hair loosely tied, crimson eyes, pale skin, "
        "high-collared black coat with silver filigree embroidery, restrained melancholy "
        "with quiet intensity, cold slate blue and deep wine palette, "
        "dim candlelit hall behind him"),
    "Seraphine": (
        "a fallen knight, short dark hair tucked behind one ear, crimson eyes, "
        "worn dark steel pauldron with tarnished gold trim and leather straps, "
        "guarded but determined expression, iron grey and warm amber palette, "
        "blurred fortress rampart and sunset sky behind"),
    "Ilias": (
        "a court sorcerer, silver hair swept back, crimson eyes behind a knowing look, "
        "ornate deep purple robe with gold arcane embroidery, faint enigmatic half-smile, "
        "violet and antique gold palette, blurred candlelit library with floating dust"),
    "Nyx": (
        "a rival cartographer, androgynous traveller with ash-grey hair loosely braided, "
        "crimson eyes, dark leather coat with brass buckles, a rolled map tucked under one arm, "
        "competitive confident smirk, forest green and warm amber palette, "
        "blurred lamplit tavern behind"),
    "Isolde": (
        "a spectral bride, long pale hair drifting as if underwater, crimson eyes that "
        "catch light and do not let it go, an antique ivory gown with tarnished silver "
        "lace, faintly translucent, tender and sorrowful expression, "
        "cold moonlight blue and candle-warm ivory palette, "
        "a stopped grandfather clock blurred behind her"),
    "Rook": (
        "a mercenary captain woman, short cropped black hair with a shaved side, "
        "crimson eyes with a sardonic glint, battered dark leather armor with steel "
        "pauldron and a blade resting across one shoulder, wry unimpressed expression, "
        "smoked steel and warm lantern amber palette, "
        "blurred night encampment behind"),
}


def generate_one(name: str, spec: str, aspect: str = "3:4") -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        sys.exit("未设置 MINIMAX_API_KEY")

    payload = {
        "model": "image-01",
        "prompt": f"{BASE}, {spec}",
        "aspect_ratio": aspect,
        "response_format": "base64",
        "n": 1,
        "negative_prompt": NEGATIVE,
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read().decode())

    imgs = (d.get("data") or {}).get("image_base64") or []
    if not imgs:
        raise RuntimeError(json.dumps(d, ensure_ascii=False)[:200])

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"portrait-{name.lower()}.jpeg")
    with open(path, "wb") as f:
        f.write(base64.b64decode(imgs[0]))
    return path


def main():
    targets = sys.argv[1:] or [r["name"] for r in ROLES]
    print(f"生成 {len(targets)} 张立绘（{os.path.getsize if False else '3:4'} 半身像）")
    for name in targets:
        spec = SPECS.get(name)
        if not spec:
            print(f"  ⚠️  跳过 {name}（无 prompt 定义）")
            continue
        t0 = time.time()
        try:
            p = generate_one(name, spec)
            print(f"  ✅ {name:12s} {os.path.getsize(p):>8,} 字节  {time.time()-t0:.1f}s")
        except Exception as e:
            print(f"  ❌ {name:12s} {str(e)[:120]}")
        time.sleep(1)          # 图像接口限流 10 RPM


if __name__ == "__main__":
    main()
