# Vesperine — 1:1 复刻实现

> 依据：`Vesperine复刻开发规格书.md`、`floze_reverse/api_contract.txt`、
> `Vesperine广告变现策略拆解.md`、`floze_reverse/模型替换实测评估.md`
> 技术栈：Python 3 标准库（**零依赖**）+ 原生 HTML/JS（无构建）

> ⚠️ **模型变更（重要）**
> M2-her 已停产（MiniMax 商务确认；OpenRouter 早已标记 deprecated，计划 2026-01-20 移除）。
> 当前默认模型已切换为 **MiniMax-M2.7**（官方描述含 "Character-rich interaction"），
> long-form 档位用 **MiniMax-M3**。
> 同时实现了**协议自动降级**：M2-her 的专有 role（`user_system` / `sample_message_*`）
> 在 M2.7/M3 上不被支持，代码会自动把 persona 与示例对话合并进 `system`。

---

## 快速开始

```bash
cd floze_clone
python3 server.py                    # mock 模式，无需 API key
# 打开 http://localhost:8080

# 接入真实模型（M2-her / M3）
export MINIMAX_API_KEY="你的key"
python3 server.py
```

首次启动自动建库（`floze.db`）并灌入 4 个种子角色。

---

## 已实现（P0 核心链路）

规格书 §2.2 定义：**「账号 → 角色浏览 → 开聊 → 消耗 hearts → 广告/IAP 补充 → 记忆召回」这条链路任一环缺失，产品不成立。**

| 环节 | 状态 | 实现要点 |
|---|---|---|
| 账号 | ✅ | 本地用户 + persona（对齐 `/user/settings`） |
| 角色库 | ✅ | 4 个 romantasy 角色，字段对齐 Creator Studio（name/description/personality/background/first_message/status_bar/tags） |
| 开聊 | ✅ | 创建会话 + 角色开场白自动注入 |
| 流式对话 | ✅ | SSE 逐 token 推送，前端实时渲染 |
| hearts 消耗 | ✅ | 按 chatMode 档位计费（lite 1 / classic 2 / smooth 3 / epic 5） |
| 广告换额度 | ✅ | **按会话绑定 + 可延迟兑换**（复刻 `unredeemedChatroomIds` 机制） |
| 广告额度上限 | ✅ | 3 次/日，超限返回 `over limit` |
| hearts 不足拦截 | ✅ | 返回 `NOT_ENOUGH_HEARTS` |
| 长期记忆 | ✅ | 记忆摘要写入 + 注入后续对话（对应 chat diary） |
| 聊天档位 | ✅ | 6 档，映射到 M2.7 / M3 与不同采样参数 |
| **动态流（P1）** | ✅ | 发帖/点赞/评论/收藏，含 hearts 消耗（发帖 3 / 评论 2）与免费额度 |

### 端到端验证结果（本机实测）

```
hearts 30 → 扣 3 → 27                     smooth 档位计费正确
看广告   → unredeemedChatroomIds=[1]       额度绑定到会话
再发送   → cost:0, isAdFunded:true → 27   广告抵扣生效
hearts=1 → NOT_ENOUGH_HEARTS              余额不足拦截
广告 x4  → 3 次成功，第 4 次 over limit    额度上限生效
发帖 x4  → 前 3 次免费，第 4 次扣 3 💖     免费额度链路正确
评论     → 扣 2 💖                         P1 经济系统接入
记忆摘要 → 写入并可在后续对话注入
全端点   → 11/11 返回 flag=0
首页     → HTTP 200
```

---

## 未实现（及原因）

| 模块 | 原因 |
|---|---|
| IAP 真实支付 | 需 Google Play 开发者账号与商品配置 |
| WebRTC 语音通话 | 需信令服务 + TURN；客户端逆向已确认用 `libjingle_peerconnection` |
| Rive 角色形象 | 需 Rive 运行时与美术资源 |
| 头像生成（UGC） | 需图像生成模型接入 |
| 创作者生态 / 动态流 | P1 功能，非最小闭环 |
| 真实广告 SDK | 需 AdMob/Pangle/Meta AN 账号；当前为服务端模拟奖励 |

---

## 项目结构

```
floze_clone/
├── server.py        HTTP 服务 + API 路由 + SSE 流式（标准库）
├── store.py         数据层（SQLite），字段对齐逆向出的真实契约
├── llm.py           模型接入：档位映射 + 实测踩坑固化 + mock 降级
├── seed_data.py     种子角色
└── static/
    └── index.html   前端（单文件，原生 JS）
```

## API 端点（命名对齐 `api_contract.txt`）

```
GET  /api/user/settings        用户 + persona + heartInfo
GET  /api/role/list            角色库
GET  /api/role/{id}            角色详情
GET  /api/chatMode             聊天档位（对应 /chatMode）
GET  /api/chatroom/list        我的会话
POST /api/chatroom             创建会话
GET  /api/message/list         消息列表
POST /api/message/send         发送消息（SSE 流式）
GET  /api/heart/list           hearts + 免费额度
GET  /api/heart/history        收支流水
GET  /api/ad/times             广告配额
POST /api/ad/reward            广告奖励（CHAT / DAILY_HEART）
GET  /api/memory/list          记忆列表
POST /api/memory/summarize     生成记忆摘要
```

统一响应 `{flag, msg, data}`，`flag=0` 为成功（FlOZE 全站约定）。

---

## 从实测中固化进代码的关键决策

1. **协议自动降级** — `llm.py:build_messages()` 按模型自动选择 native / fallback 协议，
   M2-her 停服后无需改业务代码即可切换
2. **档位分模型** — `lite/quick/classic/smooth/story` → M2.7；`epic` → M3
3. **上下文预算管理** — `trim_to_budget()`，因实测超限是硬失败 400 而非截断
4. **429 指数退避** — `_post()` 内置，应对实测到的 `rate growth limit`
5. **广告额度按会话绑定** — 复刻 `unredeemedChatroomIds` 的延迟兑换语义
6. **免费额度 → hearts 策略链** — 复刻 FlOZE 的 `free → heart (→ ad)` 消耗顺序

---

## 已知限制

- SQLite 每请求新建连接（避开跨线程限制），生产应换连接池
- mock 模式的回复是占位文本，仅用于验证链路
- 无限 hearts（`unlimitedHeartsExpiredAt`）字段已建模但未实现业务逻辑
- 未做鉴权：所有请求视为同一本地用户
