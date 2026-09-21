# 测试环境 APK 安装说明

> APK 路径：`android_shell/out/floze-clone-debug.apk`（2.1 MB）
> 构建日期：2026-09-21

---

## 一、它是什么

一个 **WebView 壳 + 内嵌前端**。

```
┌──────────────────────────────────┐
│  Floze Clone (2.1 MB)            │
│  ├─ MainActivity   原生壳          │
│  ├─ assets/www/    内嵌的 Web 前端  │  ← 界面与立绘在本地
│  │   index.html                    │     启动不依赖网络
│  │   generated/*.jpeg  6 张立绘    │
│  └─ 系统 WebView                  │
└───────────┬──────────────────────┘
            │ HTTP（仅 API 与语音）
            ▼
   http://192.168.71.82:8080
```

**与原品 FlOZE 架构一致** —— 它也是「轻原生壳 + WebView 承载业务 UI」
（见 `Floze复刻开发规格书.md` §3.1）。

体积对比：

```
我们的复刻                  2.1 MB  （含 6 张立绘、内嵌前端）
├─ 纯壳版（不内嵌）         137 KB
FlOZE 原版 base              31 MB
FlOZE 含 split               66 MB
```

---

## 二、安装步骤

### 1. 先启动服务端（在电脑上）

```bash
cd floze_clone
export MINIMAX_API_KEY="你的key"        # 不设也能跑，回复变成 mock
HOST=0.0.0.0 PORT=8080 python3 -u server.py
```

启动后会打印可用地址：

```
可用访问地址：
  http://192.168.71.82:8080          ← 手机用这个
```

**自检**：用手机浏览器打开这个地址，能看到角色列表就说明网络通了。

### 2. 装 APK 到手机

**方法 A：数据线（推荐）**

```bash
export PATH="/Users/mobvista/Downloads/test/toolchain/android-sdk/platform-tools:$PATH"
adb install -r android_shell/out/floze-clone-debug.apk
```

**方法 B：直接传文件**

把 `floze-clone-debug.apk` 发到手机（微信/AirDrop/网盘）点击安装。
首次安装需在系统设置里允许「安装未知来源应用」。

### 3. 打开 App

**界面立即出现**（内嵌资源），标题栏会显示当前服务器地址：

```
Floze Clone  ·  http://192.168.71.82:8080
```

若服务端未启动或 IP 不对，界面能打开但 API 请求会失败。
**长按标题栏**可临时改服务器地址（不用重新打包）。

---

## 三、换网络环境怎么办

APK 内嵌的默认地址是构建时的本机 IP（当前为 `192.168.71.82:8080`）。
换 WiFi 或换电脑后 IP 会变，两种处理：

**方法 1：App 内改（不用重打包）**

长按标题栏 → 输入新的 `http://<IP>:8080` → 确定。
地址存在 SharedPreferences 里，重启仍生效。

**方法 2：重新打包**

```bash
# 改 app/src/main/java/com/floze/clone/BuildConfig.java 里的 SERVER_URL
cd android_shell && ./build_apk.sh
```

**换前端代码后需要同步内嵌资源**：

```bash
cd /Users/mobvista/Downloads/test
cp floze_clone/static/index.html android_shell/app/src/main/assets/www/
cp floze_clone/static/generated/*.jpeg android_shell/app/src/main/assets/www/generated/
cd android_shell && ./build_apk.sh
```

---

## 四、重新构建

```bash
cd android_shell && ./build_apk.sh
```

脚本用 **Android SDK 原生命令行工具**，不依赖 Gradle：

```
aapt2 compile/link  →  资源编译与链接（-A 打入 assets）
javac               →  编译 Java 源码
d8                  →  转成 dex
zipalign + apksigner→  对齐与签名
```

**工具链位置**（已装好）：

```
toolchain/zulu17.../          JDK 17（Azul Zulu）
toolchain/android-sdk/
  ├── build-tools/34.0.0/     aapt2 / d8 / apksigner / zipalign
  ├── platforms/android-34/   android.jar
  └── platform-tools/         adb（可直接用来装包）
```

---

## 五、已知限制（诚实标注）

| 项 | 状态 |
|---|---|
| **真机安装验证** | **未做** — 构建环境无连接设备，也未装模拟器。装包前请先用手机浏览器确认服务地址可达 |
| API 依赖网络 | 前端在本地，但**所有数据与 AI 回复仍来自服务端**，断网不可用 |
| HTTPS | 未配置 —— 测试包 `usesCleartextTraffic=true` 允许 http |
| 应用签名 | debug keystore，**不可用于上架** |
| 内嵌资源需手动同步 | 改动 `floze_clone/static/` 后需重新复制到 assets（见 §三） |

---

## 六、为什么不做成完全独立的 App

当前形态的意义：**界面与立绘本地化（启动快、无网也能看）+ 后端仍在服务器（便于快速迭代）**。

要变成完全独立的 App 需要：

1. **后端上公网** —— 否则出门就打不开
2. **API 地址改为域名 + HTTPS**
3. **接入真实 IAP / 广告**（需 Play 账号）

这三步是上架前的必经之路，建议等产品验证通过后再做。

---

## 七、构建过程中的坑（记录备查）

1. **JDK 17 已废弃 `-bootclasspath`** — 报错「目标 17 不允许选项 --boot-class-path」。
   Android 构建只需把 `android.jar` 放到 `-classpath`。

2. **zsh 的 `set -- $var` 不做单词分割** — 生成多尺寸图标时拆分 `"mdpi 48"` 失败
   （bash 可以，zsh 不行）。改成显式函数参数调用。

3. **zsh 下不能靠字符串拼接传 aapt2 参数** — `ARGS="-A path"` 会整个当成
   一个参数。改用 if/else 分支写完整命令。

4. **`file://` 协议访问 http API 需双向放行** — 服务端加 CORS 头，
   WebView 侧开 `setAllowUniversalAccessFromFileURLs(true)`。
