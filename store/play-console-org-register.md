# 组织账号重新注册 —— 信息对照清单

> 用途：用一个**新的 Google 账号**重新注册 Google Play **组织**开发者账号。
> 生成：2026-10-10
>
> 背景：上一个账号注册成了**个人账号**（账号 ID 5629837799880395589）。
> 个人账号（2023-11-13 之后创建）发布生产版前必须跑 **12 人 × 14 天**封闭测试，
> 且会公开个人法定姓名。组织账号豁免封闭测试。
> **账号类型注册后只读、无法更改**，所以只能换新 Google 账号重来。

---

## ⚠️ 最重要的一条：别在第一步又选错

Play Console 注册流程会问账号类型，两个选项：

- **组织 / 商家（Organization）** ← **选这个**
- 个人（Individual）

**这一步选定后不可更改**（官方原文：`Account type (read only)`）。
上一个账号就是栽在这里。

**怎么确认选对了**：注册完成后，在 `开发者账号 → 关于你`
（Developer account → About you）页面看 —— 应显示 **组织账号**，
而不是「个人账号」。

---

## 一、先准备：Google Payments 资料

组织账号的 **Organization name 和 address 是从 Google Payments 资料里取的**，
不是单独填的。所以要先有一份**组织类型**的 Payments 资料：

- 组织名称：`Lurvy Limited`
- 组织地址：`Room A1, 11/F, Winner Building, 36 Man Yue Street, Hung Hom, Kowloon, Hong Kong`
- 可能需要：公司注册证明 / 商业登记号（香港公司）

⚠️ Payments 资料里的**国家、账号类型、D-U-N-S 号无法事后修改** ——
建的时候就要选对「组织」。

---

## 二、注册时要填的信息

| 项目 | 值 | 状态 |
|---|---|---|
| **账号类型** | 组织 / 商家 | ⚠️ 别选成个人 |
| **D-U-N-S 号** | 已取得，号码：`__________` | ✅ 请填入 |
| Organization name | `Lurvy Limited` | ✅ |
| Organization address | `Room A1, 11/F, Winner Building, 36 Man Yue Street, Hung Hom, Kowloon, Hong Kong` | ✅ |
| Organization website | `https://lurvy.top` | ✅ 实测 200 |
| Organization phone | `__________` | ⚠️ **待确认** |
| Contact name | `__________` | ⚠️ **待确认** |
| Contact email | `eddie@lurvy.top` | ✅ |
| Contact phone | `__________` | ⚠️ **待确认** |
| Developer name | `Lurvy Limited`（显示在 Play 商店） | ✅ 建议 |
| Developer email | `eddie@lurvy.top` | ✅ |
| Developer phone | `__________` | ⚠️ **待确认（会公开）** |

**三个电话的区别**（别搞混）：

- **Organization phone** — 公司主电话，注册用
- **Contact phone** — Google 联系你用，**不公开**
- **Developer phone** — **公开显示在 Play 开发者资料页**，别填私人号

---

## 三、DUNS 一致性（最容易失败的环节）

D&B 记录里的**公司名称与地址，必须与 Payments 资料里的完全一致**。
中文名 vs 英文名、有没有 `Co., Ltd.`、省市区写法不同，都会导致匹配失败。

**注册前去核对一遍**：

```
https://www.dnb.com/duns-number/lookup.html
```

查你自己那份 DUNS 记录，把名称和地址的**确切写法**抄下来，
Payments 资料里按同样的写法填。

---

## 四、注册时的那个核查问题

流程中会问：「过去 6 个月内，您是否使用过 **<当前账号>** 以外的任何其他
Google 账号访问 Play 管理中心？」

**答案很可能是「是」** —— 你刚用另一个 Google 账号注册过个人开发者账号。

判断标准是「是否用其他账号登录过 `play.google.com/console`」，
不是「用过几个 Gmail」。**如实填**，并把那个账号列出来。
官方承诺其他用户看不到这些信息。

---

## 五、注册后还要完成的验证

上一个账号的截图显示这三项都还是「需要采取行动」状态，
新账号同样会有：

1. **身份验证** — 上传身份/组织文件，Google 审核几天
2. **验证联系电话号码** — 需 OTP
3. **验证 Android 移动设备访问权限** — 需在一台真实 Android 设备上登录

**三项不做完，应用发不出去。**

---

## 六、注册完成后

准备好这些就能建应用了（材料都已就绪）：

- 上架包：`android_shell/release/vesperine-release.aab`
  （targetSdk 36、versionCode 1、已用 upload key 签名）
- 商店文案：`store/listing.md`
- 表单答案：`store/play-console-forms.md`
- 隐私政策 URL：`https://eddie-dme.github.io/test-ai-/privacy.html`
- 审核账号：`reviewer@lurvy.top`（密码见密码管理器 ——
  **原密码已因明文进公开仓库而泄露，需先更换**，见 `play-console-forms.md`）

### 组织账号的好处（对比个人账号）

- ✅ **不需要** 12 人 × 14 天的封闭测试
- ✅ 公开显示的是公司名与地址，不是个人法定姓名
- ⚠️ 但需要 DUNS + 组织验证，材料更多

---

## 七、旧的那个个人账号怎么处理

- **不要删**（可能影响已提交的验证记录），留着不用即可
- 它已花的 $25 拿不回来
- 在新账号注册时它会被作为「其他 Google 账号」申报
- 如果以后要注销，走 Play Console → 设置 → 账号设置里的删除流程，
  **但在新账号完全跑通之前别动它**
