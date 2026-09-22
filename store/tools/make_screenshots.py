#!/usr/bin/env python3
"""生成上架图：把 captures/ 里的界面截图套进模板，叠上卖点文案。

输出 store/screenshots/*.png（1080x1920，Google Play 手机截图规格）。

用法:
    cd <仓库根>
    python3 store/tools/make_screenshots.py

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
  .head{{flex:0 0 auto;padding:92px 66px 58px;text-align:center}}
  h1{{font-size:64px;line-height:1.17;letter-spacing:-1.2px;font-weight:700;
     background:linear-gradient(180deg,#ffffff 12%,#e6c98d 78%,#d8b46a 100%);
     -webkit-background-clip:text;background-clip:text;
     -webkit-text-fill-color:transparent;color:#e6c98d}}
  .sub{{margin-top:24px;font-size:28px;line-height:1.5;color:#9a92b8}}
  .shot{{flex:1 1 auto;overflow:hidden;padding:0 54px;position:relative}}
  .shot img{{width:100%;display:block;border-radius:34px 34px 0 0;
            border:1px solid rgba(216,180,106,.18);border-bottom:none;
            box-shadow:0 -10px 70px rgba(216,180,106,.07),
                       0 34px 90px rgba(0,0,0,.62)}}
</style></head>
<body>
  <div class="head"><h1>{H1}</h1><div class="sub">{SUB}</div></div>
  <div class="shot"><img src="{IMG}" alt=""></div>
</body></html>
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    width, height = 1080, 1920
    failures = 0

    for slug, img, h1, sub in CARDS:
        src = CAPS / img
        if not src.exists():
            print(f"  跳过 {slug}：缺少 {src}")
            failures += 1
            continue

        page = TMP / f"{slug}.html"
        page.write_text(
            TPL.format(W=width, H=height, H1=h1, SUB=sub, IMG=src.as_uri()),
            encoding="utf-8")

        out = OUT / f"{slug}.png"
        env = dict(os.environ, SHOT_URL=page.as_uri())
        proc = subprocess.run(
            [sys.executable, str(HERE / "cdp.py"), "-", str(out),
             str(width), str(height), str(width), "3000"],
            env=env, capture_output=True, text=True)

        message = (proc.stdout or proc.stderr).strip()
        if proc.returncode != 0 or not out.exists():
            failures += 1
            print(f"  {slug}: 失败 — {message[:200]}")
        else:
            print(f"  {slug}: {out.relative_to(ROOT)}  {out.stat().st_size} 字节")

    print(f"\n{'全部完成' if not failures else f'{failures} 项失败'} → {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
