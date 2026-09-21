# Floze 1:1 复刻开发规格书

> **对象**：`com.printage.floze` Android v1.2.32（versionCode 1023200）
> **文档版本**：v1.0 · 2026-09-20
> **读者**：客户端 / 前端 / 服务端 / 数据 / 商业化开发
> **用途**：作为可直接开工的复刻依据。每条规格标注证据强度与来源，**不臆造**。
> **配套文档**：`Floze竞品技术分析报告.md`（竞品视角）；本文件是**工程视角**的落地规格。

---

## 0. 阅读须知：证据分级与三项重要修正

### 0.1 证据分级（全篇沿用）

- **[硬证据]** 从二进制 / 官方前端产物 / 官方披露中直接提取，可复现。
- **[强推断]** 由多项硬证据逻辑推导，可靠性高但非直接确认。
- **[待验证]** 需抓包、运行时脱壳或服务端介入才能确认。

### 0.2 三项必须知晓的修正（相对既有分析）

**修正 1 — 客户端架构不是 "Kotlin + Jetpack Compose 原生应用"。**

实测（`floze_analysis/dex/*.dex` 类描述符统计）：

```
Lcom/printage（自有代码）  = 7 个类描述符
Lio/flutter                = 0        → 排除 Flutter
Landroidx/compose          = 2        → 几乎没有 Compose 业务代码
Lcom/google                = 4894     ← 第三方 SDK
Lcom/bytedance             = 3772
Lcom/appsflyer             = 555
Lorg/webrtc                = 461
Lcom/facebook              = 446
```

自有原生类仅：`FlozeApplication`、`MainActivity`、`FlozeMessagingService`、`NetworkCheckActivity` 等。
同时 dex 中存在大量 WebView 调用点：`loadUrl`(20)、`WebViewClient`(11)、`WebChromeClient`(9)、`evaluateJavascript`(4)、`addJavascriptInterface`(3)。

→ **真实架构是「极轻原生外壳 + WebView 承载业务 UI + JSBridge RPC」**（见 §3）。这直接决定复刻方式。
→ 副产物：Web 前端 bundle（2.9MB，未混淆）**暴露了完整后端 API 契约与业务数据模型**，是本规格书最重要的证据来源。

**修正 2 — 既有报告中被标为「从二进制验证」的若干字段，实际来源是 Web 前端而非 Android dex。**

逐字段核验（`modelUrl` / `fetchModelData` / `previewPosterUrl` / `hasBlowRegion` / `ttsSeconds` / `voiceChatroomId` / `updateVoiceChatroom` / `getTtsUrl` / `bgm` / `text-search`）：

```
在 APK 的 4 个 dex 中      : 全部 0 命中
在 .probe/web/...index.js  : 全部命中
```

原因是 App 把业务逻辑放在 WebView 与服务端，**这些字段属于 Web 前端契约**。它们对复刻依然有效（同后端），但归类应为「Web 契约证据」。

**修正 3 — 「8 种聊天模式」的构成需重新表述。**

`.probe/web/` 中的模式卡片图资源为：`chitchat`、`classic`、`lite`、`novel`、`smooth`、`story`，另有 `mode`、`model`、`notice` 三张（疑为说明页配图）。而 `chatMode` store 显示产品实际是**「情景类型 × 模型档位」二维结构**（见 §5.3），**模式列表由服务端 `/chatMode` 返回，客户端不硬编码**。

---

## 1. 产品定义（复刻目标锚点）

- **品类**：AI 角色扮演 / 沉浸式叙事创作（Romantasy：Romance + Fantasy）
- **一句话**：用户作为主角，与 AI 角色进行长期、有记忆、可语音、可视化的剧情对话
- **核心体验闭环**：
  `发现角色 → 加入 / 创建角色 → 进入聊天室对话 → 消耗 hearts → 记忆累积（diary/summary）→ 解锁隐藏章节 → 生成头像/相册 → 分享到动态流（moment）`
- **与纯陪伴型产品（Replika 类）的分野**：主打「**你是主角的叙事创作**」，而非「被陪伴」
- **目标用户**：英语市场成年女性（Mature 17+ / Sexual Themes），高频、价格敏感、期待订阅制
- **复刻范围界定**：
  - **必须 1:1**：功能集、hearts 经济数值、API 语义、核心流程与状态机
  - **可等价替换**：第三方 SDK（广告/归因/风控）、自研后端、LLM 供应商
  - **不可确证**：LLM 型号与服务端实现（见 §10.3）

---

## 2. 功能范围与优先级

### 2.1 功能矩阵

| 模块 | 关键能力 | 优先级 | 证据 |
|---|---|---|---|
| **账号** | 游客/邮箱/Google/Apple 登录、账号切换、注销、用户名生成与查重 | **P0** | `/user/*`、`/auth/*` |
| **角色（Role）** | 角色库、推荐、搜索、标签/分类、收藏、隐藏、UGC 版本、翻译 | **P0** | `/role/*`、`/tag/*`、`/category/*` |
| **聊天（Chatroom/Message）** | 创建/复制/删除会话、插入消息与标记、重生成、**回滚（revert）+ RAG 状态** | **P0** | `/chatroom/*`、`/message/*` |
| **记忆系统** | 聊天日记、星标摘要、长期记忆召回（RAG）、按档位差异化 | **P0** | `/message/revert/rag-status`、官方博客 |
| **hearts 经济** | 消耗/充值/免费额度/历史/推荐返利/抽奖 | **P0** | `/heart/*`、store 消耗表 |
| **广告变现** | 激励视频换 hearts、每日次数上限、三网并行 | **P0** | `/ad/*`、AdMob/Pangle/Meta |
| **IAP** | 12 档 hearts 包、掉单补偿 | **P0** | `floze.heart.plan1..12`、`heart-unhandle` |
| **角色形象（RoleModel）** | 可交互 2D 形象、模型数据、表情/动作 | **P1** | `/user-role-model/*`、Rive |
| **语音（TTS/通话）** | 角色配音、BGM 音轨、语音消息、实时通话 | **P1** | `/tts/*`、`/chat/voice/*`、WebRTC |
| **头像生成（UGC）** | 风格选择、Photo-to-Avatar、Text-to-Avatar | **P1** | `/ugc/style`、`/ugc/generate` |
| **相册（Album）** | 图像/视频生成、重生成、未读 | **P1** | `/album/*` |
| **情景（Scenario）** | 情景库、地点、搜索、点赞、我的情景 | **P1** | `/scenario/*` |
| **动态流（Moment）** | 发帖、评论、点赞、解锁、收藏、管理端生成 | **P1** | `/moment/*` |
| **创作者生态** | Creator Studio、关注/粉丝、屏蔽、角色文件管理 | **P1** | `/creator/*`、`/follower/*`、`/following/*` |
| **隐藏章节** | 正/负向分类、批量应用 | **P1** | `/hidden-chapter/batch` |
| **任务/成就** | 每日任务、目标、成就贴纸、快捷指令 | **P2** | `/quest/*`、`/achievement`、`/shortcut/*` |
| **通知** | 分类未读、角色/帖子/动态/评论分别已读 | **P2** | `/notification/*` |
| **举报合规** | 角色/消息/创作者/语音/相册/动态/情景/隐藏章节举报 | **P1** | `/report/*` |
| **persona** | 用户自称身份（多 persona、默认 persona） | **P2** | `/persona/*` |

### 2.2 P0 定义（MVP 必须先跑通）

账号 → 角色浏览 → 开聊 → 消耗 hearts → 广告/IAP 补充 → 记忆召回。
**这条链路任一环缺失，产品不成立。**

---

## 3. 系统架构

### 3.1 原品实测架构 [硬证据]

```
┌──────────────────────────────────────────────────────┐
│ Android 原生壳（Kotlin，自有类仅 7 个）                │
│  MainActivity · FlozeApplication · NetworkCheckActivity│
│  FlozeMessagingService（FCM 推送）                     │
│  ┌────────────────────────────────────────────────┐  │
│  │ WebView  ← 主 UI 载体                          │  │
│  │   Vue 3 + Vite SPA（app.floze.ai，2.9MB bundle）│  │
│  │   ← JSBridge RPC (callNativeAPI) →             │  │
│  └────────────────────────────────────────────────┘  │
│  原生能力插件（经 JSBridge 暴露）：                    │
│   WebRTC 通话 · Rive 动画 · TFLite · ML Kit           │
└──────────────────────────────────────────────────────┘
        │ HTTPS                                    │ WSS
        ▼                                          ▼
  api.floze.ai                            wsapi.floze.ai
  （11 套环境 /dev/../dev10/）             （流式对话长连接）
        │
   Cloudflare · AWS S3 · Firebase Storage · Sentry
```

### 3.2 JSBridge RPC 协议（复刻必备）[硬证据]

`.probe/web/assets_index-Cb5KGz0z.js` 中 `callNativeAPI` 出现 **207 次**，是实现主 UI 与原生能力通信的核心。规格：

```
callNativeAPI(api, params, apiType, opts) → Promise
  apiType                : 业务分组键，决定串行队列
  opts = {
    timeout: 10000,      // 默认 10s（源码 1e4）
    retry:   -1,         // 默认不重试
    cancelOnConfict: false,  // 同名回调冲突时取消
    showModal: ...       // 错误时是否弹窗
  }
  eventId = generateRandomString(12)   // 12 位随机事件 ID
```

- **回调注册**：`registerResolve(eventId, cb)`；重复 eventId 抛 `duplicate event callback`
- **串行队列**：同一 `apiType` 的消息进入 `apiQueueMap`，逐条 `processApiQueue` 执行（保证顺序）
- **响应信封**：`{ data: { flag, ... } }`，`flag === 0` 表示业务成功（全站统一约定）
- **生命周期**：Web 侧在就绪后调用 `sendAppLoaded()`
- **WebView 侧 store 命名空间**：`flzWV/*`（Floze WebView）、`cmWV/*`、`vivisticker/*` —— 桥接层按产品线分区

> **复刻要点**：若你同样采用「Web UI + 原生壳」，这套 RPC 是骨架；若走纯原生，则需把 207 个 `callNativeAPI` 调用点逆向为原生接口。

### 3.3 复刻建议架构（两条路线）

**路线 A — WebView 壳（与原品同构，推荐）**
- 优点：与 Web 端复用一套 UI；迭代快；原品已验证
- 代价：首屏依赖 WebView 性能；需自建 JSBridge 与原生能力插件
- 适用：需要快速对齐功能面、团队有前端能力

**路线 B — 纯原生（Kotlin/Compose 或 Flutter）**
- 优点：性能与体验上限更高
- 代价：需把 Web 契约全量翻译为原生实现，工作量大
- 适用：把体验作为差异化竞争点

---

## 4. 信息架构与页面清单

> 由 Pinia store 清单（`jt("...")`）与 API 域反推 [强推断]，命名沿用原品以便对照。

### 4.1 前端状态模块（原品 store → 页面映射）

```
flzUser          账号与用户态        → 登录页 / 我的
chatMode         聊天模式与模型档位  → 开聊前的模式选择
currentChat      当前会话指针        → 会话路由
chat             会话消息流          → 聊天主界面
role / roleSave  角色详情与收藏      → 角色详情页 / 收藏夹
myRoles          我的角色            → 创作中心
roleModel        角色形象            → 形象交互页
scenario         情景库              → 情景列表 / 情景详情
persona          用户人设            → 设置-人设
moment           动态流              → 广场 / 动态详情
privateAlbum     私密相册            → 相册页
hiddenChapter(+) 隐藏章节            → 章节选择
creator / follow 创作者与关注         → 创作者主页 / 关注流
notification     通知中心            → 通知页
heart / lottery  hearts 与抽奖        → 钱包 / 抽奖页
webPayment       支付（Web）          → 支付页
sticker          贴纸                → 贴纸面板
quest/cronjobs   任务               → 任务页
bottomPanel      底板容器            → 全局导航
tutorial/marketingPopup  引导与营销弹窗
nsfw / lock / userSetting / theme / i18nKey  全局策略
```

### 4.2 页面清单（复刻 checklist）

- **P0**：启动/引导 · 登录 · 首页广场 · 角色详情 · 聊天模式选择 · 聊天主界面 · hearts 钱包 · 充值页 · 激励视频弹窗 · 设置
- **P1**：情景列表/详情 · 相册 · 头像生成 · 角色形象 · 创作者主页 · 关注流 · 动态流 · 隐藏章节 · 举报入口
- **P2**：任务/成就 · 贴纸面板 · 通知中心 · persona 管理 · 抽奖页

### 4.3 深链（Deeplink）

- **原生 scheme**：`flz://` [硬证据，dex]
- **Web scheme**：`floze://app?act=/notification/{id}` [硬证据，Web bundle]
- **资源 scheme**：`flz://`、`flz-video://`（本地媒体引用）、`flz-audio`、`flz-video`、`flz_pin`
- **分享落地**：`public.floze.ai/link`

---

## 5. 数据模型

> 字段名来自 Web 契约（Web/App 同后端），标注为 [硬证据] 者为源码可见的**确切键名**。

### 5.1 Chatroom（会话）

```
id, name, chatroomId
chatMode（情景类型）, model（模型档位）
avatar（/chatroom/avatar/update）
bgm（/chatroom/bgm/update）
sound（/chatroom/sound/update）
voicePlay（/chatroom/voice-play/update）
```

### 5.2 Message（消息）

```
id, chatroomId
content / segments（语音分段：segments[n].url）
status: generating | ready | idle | ...   [硬证据：messageSpeech 状态机]
revert / revert-undo（回滚）  · rag-status（记忆召回状态）
regenerate-history（重生成历史：list / commit）
speech（/message/speech/generate → TTS）
```

### 5.3 聊天模式（chatMode）[硬证据，源码结构]

服务端 `/chatMode?type=` 返回，客户端按两维映射：

```
情景类型（scenario）：novel（小说）· short_talk（短对话）· story（故事）
模型档位（model）   ：standard · quick · smooth · sparkline
徽标（badge）       ：popular · beta
版本链              ：支持 upgradeOf / versionChain（同一模型的多版本）
```

- 名称与描述均为 i18n key（如 `NN1184`/`NN1282`），分 `tw` 等地区变体 → **文案走多语言系统，不硬编码**
- 模式卡片图：`chitchat / classic / lite / novel / smooth / story`（`mode / model / notice` 为说明图）

### 5.4 角色（Role）

```
id, name, avatar, description, tags[], category
creator, ugcVersion
hidden / hide（隐藏与查看权限）
savedRole（收藏：/role/save/*）
file[]（/role/file/list、/process → 创作者上传素材，最多 10 张图/视频）
translation（/role/update-translation）
```

### 5.5 Heart（经济实体）[硬证据]

```
heartInfo: { amount, unlimitedTo, unlimitedHeartsExpiredAt }
history:   { reward: {...}, cost: {...} }   // GET /heart/history
freeQuota: { suggestReply, roleUgc, momentPost, momentComment, duplicateChatroom }
```

### 5.6 其他实体

- **Persona**：`id, name, ..., isDefault`（`/persona/set-default`）
- **Scenario**：`id, title, location, category, likes, mine`
- **Moment**：`id, author, roleTags[], locked/unlock, saves, likes, comments[]`
- **Album**：`/album/generate`（图像/视频）、`mark-read`、`has-unread`
- **HiddenChapter**：`type: positive | negative`，`listHiddenChapters` / `batchHiddenChapters`

---

## 6. API 契约（190 个端点）

> 来源：Web 前端 bundle 的 `.call("<path>", {method})` 定义；**全部为 HTTP 相对路径 + 统一鉴权**。
> 统一响应约定：`{ flag, msg, data }`，`flag === 0` 为成功。

### 6.1 账号与鉴权

```
POST /user/login              POST /user/internal-login
POST /user/apple-login        POST /user/google-login
POST /user/sign-up            POST /user/sign-up-v2
POST /user/generate-code      POST /user/exchange-token
GET  /user/check-username     GET  /user/generate-username
POST /auth/refresh            POST /auth/logout
POST /user/delete             POST /user/request-delete
GET  /user/settings           GET  /user/switchable
POST /user/switch             POST /user/registerFid
GET  /user                    POST /event              （埋点上报）
GET  /user/saved-card         POST /user/saved-card/delete
GET  /user/billing-invoice-info
```

### 6.2 角色 / UGC / 角色形象

```
GET  /role/search             POST /role/suggest         GET /role/list-recommendation
GET  /role/suggested-roles    GET /role/mine             GET /role/hidden
POST /role/update             POST /role/update-translation
POST /role/hide/update        GET  /role/hide/list
GET  /role/file/list          POST /role/file/process
GET  /role/ugc-version        POST /role/save/*（add/remove/check/list/remove-by-creator）
POST /user/roles/overview     GET  /user/search
GET  /ugc/style               GET  /ugc/style/prompt      POST /ugc/generate
POST /user-role-model/update  POST /user-role-model-action/item
POST /role-model-wish         POST /role-model-apply
POST /role-recommendation-analytics/create
```

### 6.3 会话与消息（含记忆）

```
POST /chatroom                GET  /chatroom/list         GET  /chatroom/listScenario
POST /chatroom/copy           POST /chatroom/delete       POST /chatroom/batch-delete
POST /chatroom/update         POST /chatroom/insert-marker
POST /chatroom/insert-message
POST /chatroom/avatar/update  POST /chatroom/bgm/update
POST /chatroom/sound/update   POST /chatroom/voice-play/update
GET  /message/list            POST /message/update        POST /message/delete
POST /message/revert          POST /message/revert/undo
GET  /message/revert/rag-status                       ← 记忆/RAG 召回状态
GET  /message/regenerate-history/list
POST /message/regenerate-history/commit               ← 重新生成回复
POST /message/speech/generate                          ← TTS 生成
GET  message/text-search/date
POST message/text-search/chatroom
POST message/text-search/chatrooms                     ← 会话内全文检索
GET  /chatMode
```

### 6.4 语音 / 通话 / BGM

```
GET  /tts/vocal/list          GET  /tts/bgm/list        POST /tts/bgm/upload
POST /tts/bgm/update          POST /tts/bgm/delete
POST /chat/voice              POST /chat/voice/update   POST /chat/voice/charge
GET  /chat/voice/list         GET  /chat/voice/message/list
```

### 6.5 hearts / 广告 / 支付

```
GET  /heart/list              GET  /heart/list/web      GET  /heart/history
GET  /heart/free-times        POST /heart/purchase      POST /heart/purchase/web-purchase
GET  /heart/lottery           GET  /heart/referral-list
GET  /heart/check-referral    POST /heart/redeem-referral
GET  /heart/tutorials         POST /heart/tutorial-eligibility
POST /heart/tutorial-reward
GET  /ad/times                POST /ad/reward           POST /ad/owned
GET  /order/list
```

### 6.6 社交 / 动态 / 社区

```
GET  /moment/list             GET  /moment/detail       POST /moment/create
POST /moment/like             POST /moment/unlike       POST /moment/unlock
POST /moment/save             POST /moment/unsave       POST /moment/delete
GET  /moment/saved-list       GET  /moment/locked-list
GET  /moment/tag-role-candidates                      POST /moment/admin/generate
POST /moment/comment/create   POST /moment/comment/delete
POST /moment/comment/like     POST /moment/comment/unlike
GET  /follower/list           GET  /following/list
POST /following/request       POST /following/delete    POST /follower/delete
POST /follower/accept         GET  /follower/requestList
GET  /follower/hasUnreadRequests  POST /follower/markAsRead
```

### 6.7 情景 / 相册 / 隐藏章节 / 任务 / 贴纸 / 通知

```
GET  /scenario                GET  /scenario/location    GET  /scenario/search
GET  /scenario/category       GET  /scenario/mine        GET  /scenario/mine-liked
POST /scenario/like           POST /scenario/unlike      POST /scenario/batch-liked
POST /scenario/update         POST /scenario/delete
POST /album/generate          POST /album/update         POST /album/list
POST /album/mark-read         POST /album/has-unread
POST /hidden-chapter/batch
GET  /quest/dashboard         GET  /quest/list?type=daily|goal
GET  /sticker/category-list   GET  /sticker              POST /achievement
POST /shortcut/create         GET  /shortcut/list         POST /shortcut/update
POST /shortcut/delete         POST /shortcut/resort
GET  /tag/list                GET  /tag/search-list      GET  /category/list
GET  /notification/list       GET  /notification/has-unread
POST /notification/mark-roles-as-read      POST /notification/mark-posts-as-read
POST /notification/mark-moment-posts-as-read
POST /notification/mark-moment-comments-as-read
```

### 6.8 persona / creator / 举报

```
GET  /persona/list            GET  /persona              POST /persona/update
POST /persona/delete          POST /persona/set-default
POST /creator/update          POST /creator/block/update GET /creator/block/list
POST /report/{role|message|creator|voice|album|post|create|scenario|hidden-chapter}
GET  /report/{...}/check      GET  /report/{role|creator|scenario}/list
```

### 6.9 实时通道

- **WebSocket**：`wss://wsapi.floze.ai`（生产）、`wss://wsapidev.floze.ai`（开发）
- 连接参数：`?accessToken=&clientType=`，`clientType ∈ {app, payment, studio}` → 后端按业务隔离
- 用途：流式对话；**客户端无 SSE**，长连接即流式通道

### 6.10 后端环境与域名

```
API       api.floze.ai  （/dev/ … /dev10/ 共 11 套环境）
WS        wsapi.floze.ai / wsapidev.floze.ai
支付      payment.floze.ai
Web/Studio app.floze.ai（含 /studio）
分享      public.floze.ai/link
官网      floze.ai（WordPress）
模板      template.vivipic.com
基础设施  Cloudflare（前置）· AWS S3（x-amz-meta-floze）· Firebase Storage · Sentry
```

---

## 7. 商业化系统（复刻核心，数值为硬证据）

### 7.1 货币与消耗表 [硬证据，源码常量]

`heart` store 中 `c` 对象即**全站消耗配置**：

| 行为 | 策略 | 免费额度类型 | 消耗 |
|---|---|---|---|
| 发送消息（chat） | `["heart","ad"]` | — | **动态**：由 `chatMode + model` 决定 |
| 建议回复 | `["free","heart"]` | `suggestReply` | 2 |
| UGC 图片生成 | `["free","heart"]` | `roleUgc` | 6 |
| UGC 文本生成 | `["free","heart"]` | `roleUgc` | 6 |
| 动态发帖 | `["free","heart"]` | `momentPost` | 3 |
| 动态评论 | `["free","heart"]` | `momentComment` | 2 |
| 复制会话 | `["free","heart"]` | `duplicateChatroom` | 2 |
| 相册图片 | — | — | 21 |
| 相册图片重生成 | — | — | 21 |
| 相册图片转视频 | — | — | 25 |
| 相册视频 | — | — | 9 |
| 相册视频重生成 | — | — | 9 |

**消耗决策算法**（`preCheck`）：

```
1. 若存在未兑换的抽奖/奖励  → 免费（isFreeGen）
2. 否则按策略顺序尝试：
   "free"  → 查免费额度（/heart/free-times），有则扣免费额度
   "heart" → 扣 hearts
   "ad"    → 播激励视频（adType: "CHAT"）
3. 任一策略成功即停止
```

### 7.2 免费额度与恢复

- 免费额度类型：`chatSuggest`、`genUgc`、`momentPost`、`momentComment`、`duplicateChatroom`
- 各类型独立计数：`{ count, limit }`，通过 `/heart/free-times` 拉取
- 广告次数：`/ad/times`（每日上限，用户实测约 **3 次/日**）
- **无限 hearts**：`unlimitedHeartsExpiredAt` → 存在限时无限额度机制（抽奖 `UNLIMITED` 类型产出）
- 地区化常量（语义待确认）：`{ tw:40, cn:40, us:200, jp:200, ko:200 }`、`60`、`800`、`1200`

### 7.3 抽奖与推荐（增长）

- **抽奖** `/heart/lottery`：类型 `LIMITED_HEARTS`、`HOLIDAY`、`UNLIMITED`；状态 `isClaimed`；有 `pokeTime` 触发机制
- **推荐返利**：`/heart/referral-list`、`/heart/check-referral`、`/heart/redeem-referral`
- **新手引导奖励**：`/heart/tutorials`、`/heart/tutorial-eligibility`、`/heart/tutorial-reward`

### 7.4 IAP 商品 [硬证据]

```
商品 ID : floze.heart.plan1 … floze.heart.plan12   （12 档，纯 hearts 包）
价格区间 : $2.99 – $99.99（Play 页面）
订阅    : 不存在任何订阅类 SKU
掉单补偿 : "heart-unhandle" —— 以 { txId, planId } 记录未处理交易，启动时补偿
支付 SDK : Google Play Billing 8.3.0；Web 端另有 TapPay（台湾）
```

> **复刻决策点**：原品**没有订阅**，且用户评论明确要求「无限聊天订阅」。
> 若以竞争为目标，低价订阅是已被验证的未满足需求；若以 1:1 复刻为目标，则沿用 hearts 制。

### 7.5 广告变现

| 网络 | 证据 |
|---|---|
| Google AdMob | `ca-app-pub-3455649885778345~4184682196` |
| 字节 Pangle 8.1.0.4 | `com.bytedance.sdk.openadsdk`、`api16-access-ttp.tiktokpangle.us` |
| Meta Audience Network | `assets/audience_network/classes2.dex`（5.2MB） |

- 广告奖励状态机：`ad reward` → `ad reward failed` → `ad reward lost` → `ad reward settled`
- 埋点：`heartReward`、`h5adsEvent`、`ad_pending_rewarded`、`ad_reward`
- 合规：Google UMP（GDPR/广告同意）

---

## 8. 原生能力与技术选型

### 8.1 原生库清单（arm64 split，18 个）[硬证据]

```
libjingle_peerconnection_so.so  11.4MB  WebRTC 实时音视频
libmlkitcommonpipeline.so       11.0MB  ML Kit 视觉管线
librive-android.so               5.7MB  Rive 实时动画引擎
libtensorflowlite_jni.so         4.3MB  TFLite 端侧推理
libc++_shared.so                 1.3MB  C++ 运行时
libcrashlytics*.so (×3)          1.1MB  Crashlytics
libpairipcore.so                 608KB  Google Play PairIP 保护
libtt_ugen_layout.so             441KB  字节相关
libnms.so                        302KB  待确认（字节系）
libapminsight{a,b}.so            226KB  阿里云 APM
libpglarmor/libbuffer_pgl/libfile_lock_pgl  79KB  数美风控
libdatastore_shared_counter.so     7KB  DataStore
```

### 8.2 端侧模型

- `face_eye_640_float16.tflite`（5.4MB）— 人脸/眼睛检测（float16 量化）
- ML Kit `mobile_ica_8bit_with_metadata_tflite`（3MB）— 图像分类
- 20+ GLSL 着色器（`fragment_shader_{transformation,hsl,lut,separable_convolution,...}`）→ 端侧图像处理管线
- **对话推理不在端侧**，全部服务端完成

### 8.3 第三方 SDK 全清单

| 类别 | SDK |
|---|---|
| 内购 | Google Play Billing 8.3.0 |
| 广告 | AdMob · Pangle · Meta Audience Network |
| 归因 | AppsFlyer（`af_purchase` 等标准事件）· Play Install Referrer · 小米 referrer · 华为 |
| 分析 | Firebase Analytics · Google Analytics · TikTok Business SDK |
| 崩溃 | Firebase Crashlytics · Sentry（project `o976922`） |
| 推送 | Firebase Cloud Messaging（`FlozeMessagingService`） |
| 存储 | Firebase Storage（`floze-ec3a4`）· AWS S3 |
| 风控 | 数美 SSDK（`com.pgl.ssdk`） |
| 监控 | 阿里云 APM（`com.apm.insight`） |
| 登录 | Google Sign-In · Facebook SDK · Apple Sign-In |
| 保护 | Google Play PairIP（`libpairipcore.so`） |
| 合规 | Google UMP |

### 8.4 权限（复刻最小集）

```
INTERNET · ACCESS_NETWORK_STATE · POST_NOTIFICATIONS
RECORD_AUDIO · MODIFY_AUDIO_SETTINGS        ← 语音/通话
WAKE_LOCK · VIBRATE · FOREGROUND_SERVICE    ← 通话前台
USE_BIOMETRIC · USE_FINGERPRINT             ← 应用锁
com.android.vending.BILLING                  ← IAP
com.google.android.gms.permission.AD_ID      ← 广告
```

配置特征：`largeHeap=true`、`extractNativeLibs=false`、`minSdk 32`、`targetSdk 36`

---

## 9. 合规与安全（复刻必须同步）

- **内容分级**：Mature 17+ / Sexual Themes → 渠道与投放地区受限，需在商店声明
- **数据安全**：原品声明「传输加密」「可请求删除数据」「不向第三方共享数据」
- **举报体系**（8 类，全部为独立端点）：角色 / 消息 / 创作者 / 语音 / 相册 / 动态 / 情景 / 隐藏章节，每类均带 `check` 与 `list` → 说明后台有完整审核队列
- **内容风控**：`assets/dic`（4KB 高熵文件，疑为敏感词/审核词典）；数美 SSDK 提供设备风控
- **账号安全**：生物识别应用锁；`/user/delete` 与 `/user/request-delete` 双通道
- **网络配置**：`network_security_config.xml` 存在（需注意证书配置）[待验证：具体 pin 策略]

---

## 10. 落地路线与风险

### 10.1 建议里程碑

**M1 — 骨架打通（P0）**
账号体系 + 角色列表/详情 + 会话创建 + 消息收发（WebSocket 流式）+ hearts 扣减 + 广告补 hearts。

**M2 — 变现与记忆（P0/P1）**
12 档 IAP + 掉单补偿 + 免费额度体系 + 记忆系统（diary/summary + RAG 召回）+ 消息回滚/重生成。

**M3 — 沉浸体验（P1）**
TTS 角色配音 + BGM 双音轨 + WebRTC 通话 + Rive 角色形象 + 头像生成（UGC）+ 相册。

**M4 — 生态与增长（P1/P2）**
情景库 + 动态流 + 创作者生态 + 关注/粉丝 + 隐藏章节 + 任务/成就 + 举报体系。

### 10.2 已知风险

- **成本结构**：hearts 分档本质是**推理成本分层**；Lite/Quick 档必须路由到更便宜的模型，否则毛利不可控
- **记忆系统是真正壁垒**：不是模型选型，而是上下文压缩 + 长期记忆召回 + 按档位差异化
- **付费墙反噬**：原品「每日 3-5 条消息」已引发大量负面评论 → 复刻时需重新校准免费额度
- **买量成本**：三广告网 + 专业归因说明其投放成熟，正面竞争买量成本高

### 10.3 不可确证项（诚实标注）

| 项 | 状态 | 确认方式 |
|---|---|---|
| LLM 供应商与型号 | **无法从客户端确证** | 客户端无任何 LLM SDK / API 域名；需抓包 |
| 服务端记忆实现 | 待验证 | 需抓包 + 行为实验 |
| 22 个加密 assets（3.6MB，魔数 `00 49 41 50 02 00 00 00 08 07`，熵 7.89） | 未解 | 需 Frida 运行时脱壳 |
| `assets/dic` 内容 | 未解 | 高熵，疑为审核词典 |
| `network_security_config` 具体策略 | 待确认 | AXML 已编译，需解码 |

---

## 11. 证据附录（可复现）

**本规格书引用的原始素材**

```
floze_analysis/xapk/com.printage.floze.apk      base APK（dex ×2）
floze_analysis/xapk/config.arm64_v8a.apk        原生库 ×18
floze_analysis/dex/classes.dex, classes2.dex    类描述符统计来源
floze_analysis/strings/all.txt                  dex 字符串表（100,876 行）
floze_analysis/manifest_decoded.txt             解码后的 AndroidManifest
floze_reverse/api_contract.txt                  190 个 API 端点提取结果（本次生成）
.probe/web/assets_index-Cb5KGz0z.js             Web bundle（API / store / JSBridge 来源）
.probe/blog/*.html                              官方功能发布原文
.probe/play.html                                Google Play 页面快照
```

**关键复核命令**

```bash
# 架构判定：自有类规模 vs 第三方 SDK
python3 - <<'PY'
import re, collections
tot=collections.Counter()
for f in ['floze_analysis/dex/classes.dex','floze_analysis/dex/classes2.dex']:
    d=open(f,'rb').read()
    for m in re.finditer(rb'L(androidx|io|com|org)/[A-Za-z0-9/$_]+;', d):
        tot['/'.join(m.group(0)[1:].decode().split('/')[:2])]+=1
print(tot.most_common(12))
PY
# 期望输出：com/google 4894、com/bytedance 3772、com/printage 仅 7、io/flutter 0、androidx/compose 2

# API 契约复现
python3 -c "
import re
raw=open('.probe/web/assets_index-Cb5KGz0z.js',encoding='utf-8',errors='replace').read()
print(len({m.group(1) for m in re.finditer(r'\.call\(\s*\"([^\"]{2,80})\"', raw)}))"
# 期望输出：190
```

**样本指纹**：APK MD5 `80a398246b0f9e9ef1a3ceeca69f72cb`

---

*本规格书所有硬证据均可由 §11 命令复核。标注 [强推断] / [待验证] 的内容请在开发前用抓包或运行时手段确认，不要直接当作事实实现。*
