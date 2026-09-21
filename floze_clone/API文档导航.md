# MiniMax API 文档导航

> 背景：MiniMax 文档站把「**本地部署**」和「**API 调用**」放在两个不同区块，
> 从 `guides/local-deploy-m2-7` 进去会一直困在自部署路线里，找不到 API 文档。
> 本页给出 API 路线的确切地址（均已验证可达）。

---

## 一、为什么会找不到

文档站的两大区块：

```
platform.minimax.io/docs/
├── api-reference/     ← 👈 你要找的 API 调用文档在这里
│   ├── api-overview           API 能力总览
│   ├── text-post              Text Generation（原生端点）
│   ├── text-chat-openai       Chat Completions（OpenAI 兼容）★
│   ├── text-anthropic-api     Anthropic 兼容
│   └── speech-* / video-* / image-* / music-*   其他模态
│
└── guides/            ← 你点的 local-deploy 在这里
    ├── local-deploy-m2-7      ⚠️ 这是「自己跑 GPU 推理服务」
    ├── models-intro           模型列表 ★
    ├── pricing-paygo          按量定价
    ├── rate-limits            限流规格
    └── quickstart-preparation 快速开始
```

**`local-deploy-*` 是给要自己拿权重跑 GPU 的人看的**，和调 API 完全是两条路。

---

## 二、API 路线核心三页（建议直接收藏）

| 页面 | 地址 | 内容 |
|---|---|---|
| **模型总览** ★ | `platform.minimax.io/docs/guides/models-intro` | 有哪些模型、上下文长度、能力 |
| **Chat Completions** ★ | `platform.minimax.io/docs/api-reference/text-chat-openai` | OpenAI 兼容接口，最省事 |
| **按量定价** | `platform.minimax.io/docs/guides/pricing-paygo` | 各模型价格 |

其他有用的：

```
快速开始      /docs/guides/quickstart-preparation
限流规格      /docs/guides/rate-limits
API 总览      /docs/api-reference/api-overview
原生端点      /docs/api-reference/text-post
Anthropic兼容 /docs/api-reference/text-anthropic-api
```

---

## 三、一个实用技巧：直接拿 Markdown 原文

文档站每个页面**加 `.md` 后缀就是纯 Markdown 原文**，比看网页清爽得多：

```bash
curl https://platform.minimax.io/docs/guides/models-intro.md
curl https://platform.minimax.io/docs/api-reference/text-chat-openai.md
curl https://platform.minimax.io/docs/guides/pricing-paygo.md
```

还有全文索引，适合一次性喂给 AI 或检索：

```bash
curl https://platform.minimax.io/docs/llms.txt        # 页面索引（25KB）
curl https://platform.minimax.io/docs/llms-full.txt   # 全文
```

---

## 四、直接可用的调用语法

### 4.1 端点

```
Base URL    https://api.minimax.io/v1
接口        POST /chat/completions      （OpenAI 兼容）
鉴权        Authorization: Bearer <API_KEY>
```

### 4.2 curl 示例

```bash
curl https://api.minimax.io/v1/chat/completions \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "MiniMax-M2.7",
    "messages": [
      {"role": "system", "content": "You are a vampire duke in a romantasy novel."},
      {"role": "user",   "content": "Why do you keep the clocks stopped?"}
    ],
    "temperature": 1.0,
    "max_completion_tokens": 500,
    "stream": false
  }'
```

### 4.3 Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.minimax.io/v1",
    api_key="你的 MINIMAX_API_KEY",
)

resp = client.chat.completions.create(
    model="MiniMax-M2.7",
    messages=[{"role": "user", "content": "hello"}],
    max_completion_tokens=500,
)
print(resp.choices[0].message.content)
```

> 注：本项目（floze_clone）**不依赖 openai 包**，用标准库 `urllib` 实现，
> 零依赖即可运行。上面这段只是给习惯用 SDK 的人参考。

### 4.4 可用模型（`/v1/models` 实测返回）

```
MiniMax-M3              1,000,000 上下文
MiniMax-M2.7              204,800
MiniMax-M2.7-highspeed    204,800   （更快、更贵）
MiniMax-M2.5              204,800
MiniMax-M2.5-highspeed    204,800
MiniMax-M2.1              204,800
MiniMax-M2.1-highspeed    204,800
MiniMax-M2                204,800
```

**注意：M2-her 不在这个列表里**（已停产，仅历史存在）。

---

## 五、价格与限流（速查）

**定价**（per 1M tokens）

| 模型 | 输入 | 输出 | 缓存读取 |
|---|---|---|---|
| MiniMax-M3 | $0.30 | $1.20 | $0.06 |
| MiniMax-M2.7 | $0.30 | $1.20 | $0.06 |
| MiniMax-M2.7-highspeed | $0.60 | $2.40 | $0.06 |

（M2.7/M2.5/M2.1/M2 同价；M3 现为永久 5 折）

**限流**

| 模型 | RPM | TPM |
|---|---|---|
| MiniMax-M3 | 200 | 10,000,000 |
| MiniMax-M2.7 | 500 | 20,000,000 |

> 实测提醒：除表中配额，还存在 **`rate growth limit`（错误码 2045）** ——
> 限制请求速率的**增长速度**，突发高并发会被拒。必须平滑爬坡 + 退避重试。

---

## 六、获取 API Key

```
platform.minimax.io → 右上角账号 → API Keys → Create new secret key
```

- **Pay-as-you-go**：标准 API Key，按量扣余额
- **Token Plan / Credits**：订阅制，用单独的 Subscription Key（两者不通用）

余额与用量：

```
https://platform.minimax.io/user-center/payment/balance
```

---

## 七、本项目怎么配

```bash
cd floze_clone
export MINIMAX_API_KEY="你的key"
python3 server.py
```

代码默认就走官方 API（`https://api.minimax.io/v1`），档位自动映射到
`MiniMax-M2.7`（主力）与 `MiniMax-M3`（epic）。

环境变量速查：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINIMAX_API_KEY` | — | 必填 |
| `MINIMAX_BASE_URL` | `https://api.minimax.io/v1` | 不用改 |
| `MINIMAX_MODEL_PREFIX` | `""` | API 模式留空 |
| `MINIMAX_EPIC_MODEL` | `MiniMax-M3` | epic 档模型 |

> 详见同目录 `M2.7接入指南.md`（含本地部署路线与商业授权提醒）。
