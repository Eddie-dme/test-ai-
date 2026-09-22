# 上架素材

Vesperine 的 Google Play / App Store 上架图与文案。

## 内容

```
store/
├── listing.md              商店文案（标题 / 简短说明 / 完整说明）
├── icon-512.png            应用图标 512×512
├── feature-graphic.png     特色图片 1024×500
├── screenshots/            手机截图 1080×1920
│   ├── 01-chat.png         She remembers what you said last night
│   ├── 02-scenario.png     Pick a scene. Step inside.
│   ├── 03-album.png        See the story, not just read it.
│   ├── 04-feed.png         A feed of characters, not influencers.
│   └── 05-detail.png       Depth you can fall into.
└── tools/
    ├── cdp.py              用 CDP 精确控制 headless Chrome 截图
    └── make_screenshots.py 批量生成上架图
```

## 截图是怎么来的

不是真机截图，也不是手工拼的图。流程是：

1. 起本地服务（`floze_clone/server.py`），用 `tools/cdp.py` 驱动 headless
   Chrome，以 500 CSS px 视口 + 2.16× DPR 渲染出 1080×1920 的真实应用界面。
   —— 之所以不用 `--window-size`：Chrome 有 500px 最小窗口宽度，设 360px
   会被钳制，导致页面按 500px 排版却只截左 360px，右侧被裁。
2. 截图内容是真的：角色对话由 MiniMax 实时生成（`epic` 档），
   情景 / 相册 / 动态来自数据库里的种子数据。
3. 再用 `tools/make_screenshots.py` 把界面截图套进模板，叠上卖点文案。

## 复现

```bash
# 1. 起服务（需要 MINIMAX_API_KEY，不设则走 mock）
cd floze_clone && HOST=127.0.0.1 PORT=8080 python3 -u server.py &

# 2. 抓界面截图（会自动生成真实对话）
python3 store/tools/cdp.py @store/tools/chat_script.js \
    /tmp/ui-chat.png 1080 1920 500 800

# 3. 合成上架图
python3 store/tools/make_screenshots.py
```

## 设计约定

- 画布 1080×1920（宽高比 1.78，Google Play 上限为 2:1）
- 背景 `radial-gradient` 深紫，与 App 主色一致
- 标题白→金渐变，副标题灰紫
- 截图带 34px 圆角和淡金描边，底部自然延伸出画布

改动 App 前端后，重跑上面两步即可刷新全部素材。
