# Vesperine — 商店上架文案

Google Play 与 App Store 两套元数据。字数限制已逐项核对。

---

# Google Play

## 应用名称（≤30）

```
Vesperine: AI Romantasy Chat
```

28 字符。

## 简短说明（≤80）

```
Romantasy AI chat. Characters who remember, feel, and fall for you.
```

68 字符。

## 完整说明（≤4000）

```
Vesperine — Romantasy AI Chat

Step into a story that answers back.

Vesperine is an AI roleplay app for people who want more than a chatbot. Every character has a name, a past, and a reason to keep talking to you. They remember what you told them last week. They notice when you go quiet. And if you stay long enough, they tell you things they have never told anyone.

MEET THE CAST
A vampire duke who rules a court he despises. A fallen knight guarding a border she no longer believes in. A court sorcerer who is always three answers ahead of you. Each one is written with a personality, a backstory, and an opening scene — not a blank slate waiting for a prompt.

THEY REMEMBER
Conversations don't reset. Characters carry your history: your name, what you told them, how you treated them. Memories persist across sessions, and you can pin the ones that matter so they never fade.

THE STORY MOVES
Affinity grows — or doesn't. Characters react to how you treat them. Push too hard and they close off. Earn their trust and hidden chapters unlock: side stories that open for you alone.

YOU CAN SEE IT
Generate portraits and scenes from inside the story. Ask for a moonlit ballroom or a character's portrait and watch it appear. Everything you make lands in your album.

THEY SPEAK
Every character has their own voice. Tap the speaker on any message to hear it read aloud.

A FEED THAT ISN'T FULL OF STRANGERS
Characters post about what is happening in their world. Like, save, and reply. Follow the ones you care about.

CHOOSE YOUR PACE
Six reply styles, from quick one-liners to long, novel-style prose. You decide how deep the story goes.

FAIR AND OPEN
- Free to start — no subscription required
- Daily quests earn you free hearts
- Hearts unlock longer replies, image generation, and premium scenes

ABOUT
Vesperine is fiction, and it stays fiction. Every character is original and none of them are real people. Your conversations are yours.

Support: eddie@lurvy.top
Privacy: https://eddie-dme.github.io/test-ai-/privacy.html
Terms: https://eddie-dme.github.io/test-ai-/terms.html
```

约 2,150 字符。

---

# App Store

## App 名称（≤30）

```
Vesperine: AI Romantasy Chat
```

28 字符（与 Play 保持一致）。

## 副标题（≤30）

```
Characters who remember you
```

27 字符。

## 关键词（≤100，逗号分隔，逗号后不加空格）

```
ai roleplay,ai chat,romantasy,character ai,visual novel,otome,fantasy story,ai companion
```

88 字符。刻意不放 `dating`、`girlfriend`、`waifu` 这类词 —— 容易被归到成人向类目或被拒。

## 促销文本（≤170）

```
Six characters at launch — each with their own voice, memory, and secrets. Hidden chapters unlock as your bond grows.
```

115 字符。

## 描述（≤4000）

直接复用上面的 Play 完整说明，App Store 没有额外限制。

## 分级与合规备注

- 内容分级问卷按「角色扮演 / 社交」如实填写，标注含浪漫主题但无成人内容
- App Store 对 UGC 应用要求举报机制与屏蔽能力 —— 已在应用内实现
- 描述与截图中不应出现真人肖像或第三方 IP，当前素材均为原创生成

---

# 素材清单

图标与特色图片：

- `icon-1024.png` — App Store 图标，1024×1024，**不含 alpha 通道**（含透明会被 App Store 拒收）
- `icon-512.png` — Google Play 图标，512×512
- `feature-graphic.png` — Google Play 特色图片，1024×500（App Store 无此项）

截图（三套内容相同，画布不同）：

- `screenshots/play/` — 1080×1920，6 张（Play 要求宽高比 ≤2:1，此处 1.78）
- `screenshots/ios-6.7/` — 1290×2796，6 张
- `screenshots/ios-6.9/` — 1320×2868，6 张

顺序对应卖点：

```
00-roles     角色阵容      第一张要回答「这是什么应用」
01-chat      长期记忆      角色的记忆与状态栏
02-scenario  情景模式
03-album     图像生成
04-feed      角色动态
05-detail    角色深度
```

---

# 待确认项

**✅ 已解除（2026-10-09 实测）—— 隐私政策与服务条款已可用于提交。**

上文原先记录的 8 处法律信息占位符已全部填实。`docs/privacy.html` /
`docs/terms.html` 现在写的是：

```
Lurvy Limited
Room A1, 11/F, Winner Building, 36 Man Yue Street, Hung Hom, Kowloon, Hong Kong
eddie@lurvy.top
```

（`docs/index.html` 里的 `[GITHUB_USER]/[REPO_NAME]` 也已无残留。）

**公开可访问的 URL 已在线**（GitHub Pages，从 main 的 `/docs` 发布），实测：

```
https://eddie-dme.github.io/test-ai-/privacy.html   HTTP 200  9807B
https://eddie-dme.github.io/test-ai-/terms.html     HTTP 200  9560B
https://eddie-dme.github.io/test-ai-/index.html     HTTP 200  7189B
```

三页均与本地 `docs/` **逐字节一致**，标题正确，HTTPS 可用。

**⚠️ 不要再用 `https://lurvy.top/privacy.html`** —— 实测 **404**：`lurvy.top`
目前解析到另一个站点（标题为「lurvy.top | 智能营销增长伙伴」），
不是本项目。提交给 Play / App Store 的必须是上面 GitHub Pages 的地址。

App 内注册页引用的也正是该地址（`floze_clone/static/index.html` 第 329–332 行），
内外一致，不存在拒审风险。

Play 对政策页的要点要求（数据类型、共享对象、保留期限、用户权利、儿童、
广告与同意、联系方式）在 `privacy.html` 的 11 个章节中均已覆盖。

> 附：本节曾写「仓库尚未配置 git 远端，无法推送」—— 此条也已过时，
> 远端已配好（`origin` = github.com/Eddie-dme/test-ai-），仅缺推送凭证。
> 详见 `HANDOFF.md` 待办 3。
