# 上架素材

Vesperine 的 Google Play / App Store 上架图与文案。

## 内容

```
store/
├── listing.md                商店文案（Play + App Store 元数据）
├── icon-1024.png             App Store 图标 1024×1024（无 alpha）
├── icon-512.png              Google Play 图标 512×512
├── feature-graphic.png       Google Play 特色图片 1024×500
├── screenshots/
│   ├── play/                 1080×1920     Google Play 手机截图
│   ├── ios-6.7/              1290×2796     App Store 6.7"
│   └── ios-6.9/              1320×2868     App Store 6.9"
└── tools/
    ├── cdp.py                用 CDP 精确控制 headless Chrome 截图
    ├── make_screenshots.py   批量生成上架图（多平台 profile）
    ├── capture_chat.js       抓聊天页时用的脚本（会真实生成一轮对话）
    ├── icon_source.html      图标生成源（星芒孕心）
    └── captures/             界面原图 1080×1920
```

三套截图内容相同、只是画布与留白不同 —— 界面截图本身复用 `captures/`。

五个卖点（三个平台一致）：长期记忆 → 情景模式 → 图像生成 → 角色动态 → 角色深度。

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
python3 store/tools/cdp.py @store/tools/capture_chat.js \
    /tmp/ui-chat.png 1080 1920 500 800

# 3. 合成上架图（省略 profile 则生成全部三套）
python3 store/tools/make_screenshots.py
python3 store/tools/make_screenshots.py play        # 只出 Play 一套

# 4. 重出图标（改了 icon_source.html 之后）
#    Chrome headless 截图即可，输出 1024×1024
```

## 设计约定

- 背景 `radial-gradient` 深紫，与 App 主色一致
- 标题白→金渐变，副标题灰紫
- 截图带圆角和淡金描边，底部自然延伸出画布
- 排版参数按平台分别定义（`PROFILES`）：Play 比例 1.78、App Store 2.17，
  留白不能等比缩放，否则要么顶部太空、要么截图被裁太多

改动 App 前端后，重跑第 2、3 步即可刷新全部素材。
