#!/usr/bin/env python3
"""生成上架图：把 captures/ 里的界面截图套进模板，叠上卖点文案。

输出：
    store/screenshots/play/*.png      1080x1920   Google Play 手机截图
    store/screenshots/ios-6.7/*.png   1290x2796   App Store 6.7"

用法:
    python3 store/tools/make_screenshots.py [profile ...]     # 省略则全部

前置：captures/ 里要有界面截图。生成方式见 store/README.md。
"""
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CAPS = HERE / "captures"
OUT = ROOT / "store" / "screenshots"
TMP = pathlib.Path("/tmp/vesperine_market")

# 画布尺寸与排版参数。两套平台比例不同（Play 1.78、App Store 2.17），
# 所以文案区留白单独调，不能简单等比缩放。
PROFILES = {
    "play": dict(
        W=1080, H=1920,
        font_h1=64, font_sub=28, sub_gap=24,
        pad_top=92, pad_bottom=58, pad_side=66,
        shot_side=54, radius=34,
    ),
    "ios-6.7": dict(
        W=1290, H=2796,
        font_h1=76, font_sub=33, sub_gap=34,
        pad_top=228, pad_bottom=196, pad_side=78,
        shot_side=64, radius=41,
    ),
    # App Store Connect 也接收 6.9"（iPhone 16 Pro Max），比例与 6.7" 几乎一致
    "ios-6.9": dict(
        W=1320, H=2868,
        font_h1=78, font_sub=34, sub_gap=35,
        pad_top=233, pad_bottom=200, pad_side=80,
        shot_side=65, radius=42,
    ),
}

# (输出名, 界面截图, 主标题, 副标题)
CARDS = [
    ("01-chat", "01-chat.png",
     "She remembers<br>what you said last night.",
     "Long-term memory, mood, and a story that reacts to you"),
    ("02-scenario", "02-scenario.png",
     "Pick a scene.<br>Step inside.",
     "Hand-written scenarios across romance, rivalry and intrigue"),
    ("03-album", "03-album.png",
     "See the story,<br>not just read it.",
     "Generate portraits and scenes in one tap"),
    ("04-feed", "04-feed.png",
     "A feed of characters,<br>not influencers.",
     "Post, like and follow the characters you love"),
    ("05-detail", "05-detail.png",
     "Depth you can<br>fall into.",
     "Personalities, backstories and secrets that unlock"),
]

TPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{width:{W}px;height:{H}px;overflow:hidden;display:flex;flex-direction:column;
       background:radial-gradient(130% 62% at 50% 0%, #2c2350 0%, #14102a 38%, #0d0b14 66%);
       font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
       color:#e8e3f5}}
  .head{{flex:0 0 auto;padding:{pad_top}px {pad_side}px {pad_bottom}px;text-align:center}}
  h1{{font-size:{font_h1}px;line-height:1.17;letter-spacing:-1.2px;font-weight:700;
     background:linear-gradient(180deg,#ffffff 12%,#e6c98d 78%,#d8b46a 100%);
     -webkit-background-clip:text;background-clip:text;
     -webkit-text-fill-color:transparent;color:#e6c98d}}
  .sub{{margin-top:{sub_gap}px;font-size:{font_sub}px;line-height:1.5;color:#9a92b8}}
  .shot{{flex:1 1 auto;overflow:hidden;padding:0 {shot_side}px;position:relative}}
  .shot img{{width:100%;display:block;border-radius:{radius}px {radius}px 0 0;
            border:1px solid rgba(216,180,106,.18);border-bottom:none;
            box-shadow:0 -10px 70px rgba(216,180,106,.07),
                       0 34px 90px rgba(0,0,0,.62)}}
</style></head>
<body>
  <div class="head"><h1>{H1}</h1><div class="sub">{SUB}</div></div>
  <div class="shot"><img src="{IMG}" alt=""></div>
</body></html>
"""


def build(profile_name, params):
    target = OUT / profile_name
    target.mkdir(parents=True, exist_ok=True)
    width, height = params["W"], params["H"]
    failures = 0

    for slug, img, h1, sub in CARDS:
        src = CAPS / img
        if not src.exists():
            print(f"  {profile_name}/{slug}: 跳过，缺少 {src.name}")
            failures += 1
            continue

        page = TMP / f"{profile_name}-{slug}.html"
        page.write_text(
            TPL.format(H1=h1, SUB=sub, IMG=src.as_uri(), **params),
            encoding="utf-8")

        out = target / f"{slug}.png"
        env = dict(os.environ, SHOT_URL=page.as_uri())
        proc = subprocess.run(
            [sys.executable, str(HERE / "cdp.py"), "-", str(out),
             str(width), str(height), str(width), "3000"],
            env=env, capture_output=True, text=True)

        message = (proc.stdout or proc.stderr).strip()
        if proc.returncode != 0 or not out.exists():
            failures += 1
            print(f"  {profile_name}/{slug}: 失败 — {message[:180]}")
        else:
            print(f"  {profile_name}/{slug}  {out.stat().st_size} 字节")

    return failures


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[1:] or list(PROFILES)
    unknown = [p for p in wanted if p not in PROFILES]
    if unknown:
        print(f"未知 profile: {', '.join(unknown)}；可选 {', '.join(PROFILES)}")
        return 1

    total_failures = 0
    for name in wanted:
        print(f"[{name}] {PROFILES[name]['W']}x{PROFILES[name]['H']}")
        total_failures += build(name, PROFILES[name])

    print(f"\n{'全部完成' if not total_failures else f'{total_failures} 项失败'}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
