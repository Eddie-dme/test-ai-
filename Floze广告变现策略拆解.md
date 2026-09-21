# Floze 广告变现策略拆解

> **对象**：`com.printage.floze` Android v1.2.32
> **日期**：2026-09-20
> **证据来源**：Web 前端 bundle（`.probe/web/assets_index-Cb5KGz0z.js`）、APK dex（`floze_analysis/dex/*.dex`）、解码 manifest、Play 页面快照
> **配套**：`Floze复刻开发规格书.md`（工程规格）· `Floze竞品技术分析报告.md`（竞品视角）

---

## 0. 结论速览

FlOZE 的广告体系是**双轨并行 + 服务端可调 + 重防作弊**：

- **双轨**：三方联盟（AdMob / Pangle / Meta AN）与**自营广告系统**（OWNED）并行，互相回退
- **两种奖励**：`CHAT`（广告换一条聊天消息）与 `DAILY_HEART`（广告换 hearts），**都不发通用可囤积货币**
- **服务端驱动**：广告流量在联盟与自营之间的分配由服务端 `policy.unit` 下发，可远程调控
- **定位**：广告是**免费用户的留存工具**，只覆盖聊天这一个场景；生成类消耗（头像/相册）不开放广告入口，逼向 IAP

---

## 1. 双轨结构

### 1.1 轨道 A：三方联盟（原生 SDK）

```
AdMob     app id: ca-app-pub-3455649885778345~4184682196   [manifest 确证]
Pangle    8.1.0.4（字节跳动）
Meta AN   assets/audience_network/classes2.dex（5.2MB）
```

三网并行通常意味着追求 eCPM 最大化与分地区填充率互补。

### 1.2 轨道 B：自营广告（OWNED）

容易被忽略但很关键——FlOZE **自己也是广告位的销售/分发方**：

```
/ad/owned                     自建投放接口
provider: "OWNED"             独立 provider 标识
ad-player ... z-ad-player     自建全屏视频播放器 UI（minified CSS class）
ad.mediaUrl                   视频素材地址
ad.landingUrl                 点击跳转（window.open(landingUrl, "_blank")）
owned-ad-muted                静音偏好，localStorage 持久化
watchedDuration               观看时长统计（toFixed(2)）
```

自营链路意味着其广告收入**不全来自联盟分成**。

---

## 2. 奖励类型：只有两种，且都不可囤积

```js
Gy = ["CHAT", "DAILY_HEART"]
```

### 2.1 `CHAT` — 广告换一条聊天消息（设计最精巧）

```js
CHAT(chatroomId) {
  const unredeemed = quota.CHAT.unredeemedChatroomIds || []
  if (index === -1 || !pending.CHAT[chatroomId] || !chat.getMsgs(chatroomId).length) return false
  chat.setInputText(chatroomId, pending.CHAT[chatroomId])   // 恢复草稿
  chat.sendMsg(chatroomId, { isAdFunded: true })            // 标记「广告资助」
  unredeemed.splice(index, 1)
}
```

三个设计要点：

1. **不发通用货币，发场景化额度**——拿到的是"下一条消息免费"，而非可囤积的 hearts。避免广告奖励沉淀成余额、侵蚀 IAP。
2. **按聊天室绑定**（`unredeemedChatroomIds`）——额度挂在具体会话上。
3. **保留输入框草稿**（`pending.CHAT[chatroomId]`）——播放广告时暂存用户已输入内容，返回后原样恢复。

`unredeemedChatroomIds` **跨会话保留**：看完广告立即退出 App，下次回来额度仍在。纯留存导向。

### 2.2 `DAILY_HEART` — 广告换 hearts

每日额度制，是免费用户的保底入口。

---

## 3. provider 优先级：双向回退

```js
De(type) {
  if (policy.unit === "OWNED")
     → 先走自营 t.serve({usePolicy:true})
        若返回 INVALID_POLICY → 清空 policy，回退 He()（原生三方）
  else
     → 先走原生 showAd()（三方 SDK）
        若 flag === "3"（失败）→ 回退自营 t.serve()
}
```

- `policy` 由服务端 `/ad/times` 随配额一起下发，`policy.unit` 决定主路径
- `INVALID_POLICY` 是显式错误码，说明策略校验是服务端驱动的
- 结论：**广告流量分配比例可远程实时调控**

---

## 4. 频次控制：三层时间窗

| 参数 | 值 | 含义 |
|---|---|---|
| 默认冷却 | **90 秒**（`h = M(90)`） | 单次广告后最短间隔，可被服务端 `nextAvailableTime` 覆盖 |
| count floor 有效期 | **10 分钟**（`P = 10*6e4`） | 本地已看次数记录的保鲜期 |
| pending 过期 | **60 分钟**（`A = 60*6e4`） | 未完成广告流的作废时限 |
| 点击节流 | 800ms（`x = 800`） | 防连点 |

配额模型：

```
quota[type] = {
  count, limit,                       // 服务端下发
  unredeemedChatroomIds: [],          // CHAT 独有的未兑换池
  cooldown: { nextAvailableTime, remainingTime, displayText, timer }
}
```

用户评论反映的"每天 3 次"即 `limit` 的典型值（来源：Play 评论区，非二进制确证）。

---

## 5. 防作弊：本地与云端双记账

这部分工程投入很重，说明广告收入体量足以支撑专门开发。

### 5.1 业务日切分

```js
ve(t) = new Date(t + 3*3600*1000).toISOString().slice(0,10)   // UTC+3
```

按 **UTC+3** 划分业务日——与目标市场（欧洲/中东/美东）自然日对齐，同时规避改时区刷额度。

### 5.2 三层防护

1. **只增不减**：`count = Math.max(count, snapshot + 1)`，防止回滚本地值
2. **floor 带有效期与日期**：`{count, until: now + 10min, adDay}`；读取时校验 `now <= until` **且** `adDay` 匹配，否则归零 → 改系统时间/改 localStorage 均无效
3. **pending 快照 + 超时作废**：广告开始前把全类型 count 快照写入 `flz-ad-pending`；超过 60 分钟未完成则丢弃

### 5.3 orphan close 兜底

监听 `flag === "2"` 的异常关闭事件（`Vt` 函数），区分：

- "看完了但窗口异常关闭" → 补偿计数
- "没看完" → 作废

对应埋点 `ad_pending_threshold_met`，日志 `🚩 ad ‣ orphan close`。

### 5.4 多账号隔离

所有 localStorage 键按 userId 隔离：`${key}:${userId}`；首读时会把旧的全局键迁移到用户维度。

```
flz-ad-cooldown      冷却状态
flz-ad-pending       未完成广告流快照
flz-ad-count-floor   本地已看次数下限
owned-ad-muted       自营广告静音偏好
```

---

## 6. 广告形式矩阵（dex 类名统计）[硬证据]

```
NativeAd                27   ← 主力形态
InterstitialAd          19
BannerAd                12
AppOpenAd / Activity    12   ← 开屏
RewardedInterstitialAd  10
RewardedAd              10
RewardedVideoAd          6
NativeBannerAdView       3
```

覆盖开屏 / 插屏 / 激励 / 原生 / 横幅全形态。

激励广告有**预加载缓冲池**（`ad_rewarded_buffer_size`、`ad_rewarded_queue_size`、`ad_rewarded_entries`）→ 提升完播率与填充率。

> 注：上述缓冲池键名出现在 APK 字符串表，归属可能是三方 SDK 内部实现，未见自研调用点。

---

## 7. 埋点与全链路追踪

```
曝光/点击   ad_impression(3) · ad_click(6)
奖励        heartReward · ad_reward · h5adsEvent
自营        OWNED_AD_RECORD(mediaUrl, clicked, watchedDuration)
状态机      ad reward → ad reward failed → ad reward lost → ad reward settled
追踪链      ad_pending_flow_id / event_id / trace_id / response_id / provider / presented_at
日志        🚩 ad ‣ flow start / flow refused / native adUnit / reward received / watch counted / orphan close
```

- `flowId` 由 Web 侧生成 **ULID**，贯穿 Web → 原生 → 服务端
- `responseId` 为联盟侧标识，两者共同构成端到端归因链
- 自营广告记录 `watchedDuration.toFixed(2)` → 按观看时长计费/分成的基础数据

---

## 8. 边界条件与开关

- **Web 浏览器版不投广告**：`showAd` 首行 `if (this.inBrowserMode) return {flag:"1", msg:"browserMode"}` → 广告只在 App 内触达
- **开发模式绕过配额**：`flzWV/getIgnoreQuotaMode`
- **广告只服务聊天**：`heart` store 中仅 `chat` 使用 `["heart","ad"]` 策略；`suggestReply`、`ugcPhoto`、`albumImage` 等均为 `["free","heart"]`，**无广告入口**

### 广告策略在消耗配置中的位置

| 行为 | 策略 | adType |
|---|---|---|
| 发送消息 | `["heart","ad"]` | `CHAT` |
| 建议回复 | `["free","heart"]` | — |
| UGC 图片/文本 | `["free","heart"]` | — |
| 动态发帖/评论 | `["free","heart"]` | — |
| 复制会话 | `["free","heart"]` | — |
| 相册图片/视频 | 仅 hearts | — |

---

## 9. 策略解读

1. **广告定位为免费用户的留存工具，而非全量变现手段**。只有聊天——最核心、最易上瘾的行为——开广告口子；生成类消耗不给广告入口，逼向 IAP。
2. **奖励场景化、即时化、不可囤积**，避免与 IAP 形成替代关系。
3. **三网并行 + 自营回退 + 服务端可调策略**，把广告流量当作可实时优化的库存池，追求整体 eCPM/填充率最大化。
4. **防作弊投入很重**（双记账、日切分、快照、孤儿兜底），与"三网并行"互相印证：**广告是与 IAP 并列的营收支柱**。

---

## 10. 待验证项（诚实标注）

| 项 | 状态 | 确认方式 |
|---|---|---|
| `limit` 的实际档位（是否确为 3 次/日） | 未确证 | 需抓包 `/ad/times` 响应 |
| 自营广告的定价与计费方式 | 未确证 | 商务条款，客户端不可见 |
| 三方联盟 ad unit ID | 未确证 | dex 中仅见全零占位符，真实 ID 可能服务端下发 |
| 广告位在 UI 中的具体落位 | 部分推断 | 由状态与文案推理，未做界面截图分析 |

---

*本文档所有机制性结论均可由 `.probe/web/assets_index-Cb5KGz0z.js` 复核（搜索 `ownedAd`、`ad_pending`、`adType`、`flow refused`）。标注为待验证的内容请以抓包为准，不要直接当作事实使用。*
