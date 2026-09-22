# Vesperine

AI 角色扮演对话应用 —— 服务端、Web 前端与 Android 客户端。

一个完整的端到端实现：从角色设定、流式对话、长期记忆，到好感度解锁、
虚拟货币经济与广告变现，全部跑通。

---

## 这是什么

用户与 AI 角色进行沉浸式剧情对话的移动应用。每个角色有独立的性格、
背景故事与开场白；对话会累积好感度，解锁隐藏章节；语音合成让角色"开口说话"。

技术上是一套**零外部框架依赖**的实现：服务端只用 Python 标准库，
前端是单个 HTML 文件，Android 端直接用 SDK 命令行构建，不经 Gradle。

---

## 功能

**角色系统**
- 角色创建：名称、身份标签（最多 5）、性格特质（最多 10）、背景故事（3000 字符）、
  开场白（1000 字符）、封面立绘、状态栏模板、可见性（公开 / 链接可见）
- 角色详情页、标签检索、创作者主页

**对话**
- SSE 流式输出，逐字返回
- 三档回复长度（classic / novel / epic），对应不同 token 预算与消耗
- 消息编辑、重新生成、朗读（TTS）

**记忆**
- Memories 面板：称呼、生日、喜好、其他，可锁定条目
- Chat Diary：自动生成对话摘要，作为长期上下文注入
- 上下文按模型窗口自动裁剪（超限时保留 system + 最近若干轮）

**好感度与章节**
- 好感度区间 -2000 ~ 2000，按对话情感走向累计
- Hidden Chapters：创作者设定解锁阈值，玩家达到后进入支线剧情
- 章节内发生的事自动同步回主对话记忆

**经济与变现**
- Hearts 虚拟货币：充值（IAP）、广告奖励、对话消耗
- 广告额度与免费额度分离计数，每日重置
- 掉单补偿：未处理交易落库，启动时补偿
- 12 档充值商品，消耗型（可重复购买）

**其他**
- 动态流（发帖 / 点赞 / 收藏 / 评论）
- 情景模式、相册（UGC 图集）、任务、成就、通知、贴纸
- 图片生成：文生图 / 图生图（角色头像）
- 语音合成：每个角色独立音色

---

## 架构

```
floze_clone/              服务端 + Web 前端
├── server.py             HTTP 路由层，24 个 API 端点，CORS / SSE
├── store.py              SQLite 数据层，25 张表
├── llm.py                模型适配层（流式对话、prompt 组装、上下文裁剪）
├── tts.py                语音合成
├── imagegen.py           图片生成
├── affinity.py           好感度计算（模型判定 + 启发式回退）
├── voices.py             角色音色映射
├── seed_data.py          6 个种子角色
└── static/
    ├── index.html        单文件 SPA（约 1150 行，零构建）
    └── install.html      移动端安装引导页

android_shell/            Android 客户端
├── build_apk.sh          直接调用 aapt2 / javac / d8 / apksigner
└── app/src/main/
    ├── java/.../MainActivity.java    WebView 壳，支持运行时切换服务端地址
    └── assets/www/                   内嵌前端与立绘

floze_reverse/            模型接入验证
├── minimax_rp_client.py  角色扮演场景下的模型质量评测脚本
└── 模型替换实测评估.md    多模型对比实测记录
```

### 几个实现上的选择

**为什么不用 Web 框架**
服务端只用 `http.server` + `sqlite3`。好处是部署时不需要 `pip install`，
一个 Python 文件就能跑；代价是路由和 CORS 要手写。

**为什么前端是单文件**
`index.html` 里内联了全部 CSS 和 JS，没有构建步骤。改完直接刷新就能看到效果，
打包进 APK 也只是复制文件。

**为什么 Android 不用 Gradle**
`build_apk.sh` 直接调用 SDK 的命令行工具链
（`aapt2 compile/link` → `javac` → `d8` → `zipalign` → `apksigner`）。
构建时间从分钟级降到秒级，依赖也只有一个 JDK 和 Android SDK。

**移动端适配**
窄屏（≤820px）下桌面双栏收敛为单栏，侧栏移入抽屉，由左上角按钮唤出；
顶部工具栏自动换行；底部输入区避开手势条（`env(safe-area-inset-bottom)`）。

---

## 运行

**服务端**

```bash
cd floze_clone
export MINIMAX_API_KEY="你的 key"     # 不设则走 mock 模式，链路仍可验证
HOST=0.0.0.0 PORT=8080 python3 -u server.py
```

启动后会打印所有可用地址（含局域网 IP），手机连同一 WiFi 即可访问。

**Android 客户端**

```bash
cd android_shell
./build_apk.sh                        # 产物在 out/vesperine-debug.apk
```

需先设置 `JAVA_HOME` 与 `ANDROID_HOME`（脚本内有说明）。

---

## 技术栈

- **服务端**：Python 3（标准库：`http.server`、`sqlite3`、`urllib`、`json`）
- **前端**：原生 JS + CSS，无框架、无构建
- **Android**：Java，WebView 容器，SDK 命令行工具链
- **模型**：MiniMax-M3（对话）、speech-2.8（TTS）、图像生成 API
- **存储**：SQLite（25 张表）

---

## 开发说明

`floze_reverse/模型替换实测评估.md` 记录了模型选型过程中的实测数据，
包括不同模型在角色扮演场景下的输出质量、思维链可控性与成本对比。

`floze_reverse/minimax_rp_client.py` 是可复现的评测脚本，
用于验证模型是否满足角色扮演场景的要求（人设一致性、记忆调用、
格式规范、长度控制）。
