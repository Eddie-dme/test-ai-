#!/bin/zsh
#
# Vesperine —— APK 构建脚本（不依赖 Gradle，直接用 Android SDK 命令行工具）
#
# 流程：
#   aapt2 compile/link  →  资源编译与链接，生成 R.java
#   javac               →  编译 Java 源码
#   d8                  →  转成 dex
#   zipalign + apksigner→  对齐与签名
#
# 用法：  ./build_apk.sh
# 产物：  ./out/vesperine-debug.apk

set -e
cd "$(dirname "$0")"

# ---- 环境（按需修改）----
export JAVA_HOME="${JAVA_HOME:-/Users/mobvista/Downloads/test/toolchain/zulu17.50.19-ca-jdk17.0.11-macosx_aarch64/zulu-17.jdk/Contents/Home}"
export ANDROID_HOME="${ANDROID_HOME:-/Users/mobvista/Downloads/test/toolchain/android-sdk}"

BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-34/android.jar"
APP="app/src/main"
OUT="out"
# ⚠️ keystore 必须放在 out/ 之外：out/ 每次构建都会被 rm -rf，
# 放在里面会导致每次重新生成密钥 → 签名变化 → 手机无法覆盖安装。
KEYSTORE="$PWD/debug.keystore"
PKG_PATH="top/lurvy/vesperine"

echo "── 环境检查 ──"
for f in "$JAVA_HOME/bin/javac" "$BUILD_TOOLS/aapt2" "$BUILD_TOOLS/d8" "$BUILD_TOOLS/apksigner" "$BUILD_TOOLS/zipalign" "$PLATFORM"; do
  [ -e "$f" ] || { echo "❌ 缺少: $f"; exit 1; }
done
echo "✅ JDK / aapt2 / d8 / apksigner / zipalign / android.jar 就绪"

rm -rf "$OUT"; mkdir -p "$OUT"/{compiled,gen,classes,dex}

echo
echo "── 1/5 编译资源（aapt2 compile）──"
"$BUILD_TOOLS/aapt2" compile --dir "$APP/res" -o "$OUT/compiled/res.zip"
echo "✅ 资源已编译"

echo
echo "── 2/5 链接资源（aapt2 link，生成 R.java）──"
# -A 把 assets/ 打进去（内嵌的 Web 前端在这里）
# 注意：zsh 不做单词分割，所以用数组而不是字符串拼接
if [ -d "$APP/assets" ]; then
  echo "  包含 assets: $(find "$APP/assets" -type f | wc -l | tr -d ' ') 个文件"
  "$BUILD_TOOLS/aapt2" link \
    -o "$OUT/base.apk" \
    -I "$PLATFORM" \
    --manifest "$APP/AndroidManifest.xml" \
    --java "$OUT/gen" \
    -A "$APP/assets" \
    --min-sdk-version 21 --target-sdk-version 34 \
    --version-code 1 --version-name "0.1.0" \
    "$OUT/compiled/res.zip"
else
  "$BUILD_TOOLS/aapt2" link \
    -o "$OUT/base.apk" \
    -I "$PLATFORM" \
    --manifest "$APP/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --min-sdk-version 21 --target-sdk-version 34 \
    --version-code 1 --version-name "0.1.0" \
    "$OUT/compiled/res.zip"
fi
echo "✅ 资源已链接，R.java 生成于 $OUT/gen"

echo
 echo "── 3/5 编译 Java 源码（javac）──"
find "$APP/java" "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
# 注意：JDK 17 已废弃 -bootclasspath，Android 构建只需把 android.jar 放到 -classpath
"$JAVA_HOME/bin/javac" -nowarn -encoding UTF-8 \
  -classpath "$PLATFORM" \
  -d "$OUT/classes" @"$OUT/sources.txt"
CLS=$(find "$OUT/classes" -name '*.class' | wc -l | tr -d ' ')
if [ "$CLS" -eq 0 ]; then echo "❌ 没有生成任何 class，编译失败"; exit 1; fi
echo "✅ 编译完成（$CLS 个 class）"

echo
 echo "── 4/5 转 dex（d8）──"
"$BUILD_TOOLS/d8" --release --min-api 21 --lib "$PLATFORM" \
  --output "$OUT/dex" $(find "$OUT/classes" -name '*.class')
[ -f "$OUT/dex/classes.dex" ] || { echo "❌ dex 生成失败"; exit 1; }
echo "✅ dex 生成：$(ls -lh "$OUT/dex/classes.dex" | awk '{print $5}')"

echo
echo "── 5/5 打包 + 对齐 + 签名 ──"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q "../unsigned.apk" classes.dex)

# 生成 debug keystore（仅首次；持久化在 out/ 之外）
if [ ! -f "$KEYSTORE" ]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -v \
    -keystore "$KEYSTORE" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" > /dev/null 2>&1
  echo "  已生成 debug keystore"
fi

"$BUILD_TOOLS/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
"$BUILD_TOOLS/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --ks-key-alias androiddebugkey \
  --out "$OUT/vesperine-debug.apk" "$OUT/aligned.apk"

echo
echo "── 验证签名 ──"
"$BUILD_TOOLS/apksigner" verify --print-certs "$OUT/vesperine-debug.apk" | head -4

echo
echo "════════════════════════════════════════"
ls -lh "$OUT/vesperine-debug.apk"
echo "产物: $(pwd)/$OUT/vesperine-debug.apk"
echo "════════════════════════════════════════"
