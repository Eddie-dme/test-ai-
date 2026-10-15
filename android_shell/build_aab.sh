#!/bin/zsh
#
# Vesperine —— AAB 构建脚本（上架 Google Play 用；不依赖 Gradle）
#
# 与 build_apk.sh 的区别：
#   - APK 走普通 aapt2 link；AAB 走 aapt2 link --proto-format + bundletool
#     （aapt2 帮助原文：--proto-format "Suitable as input to the bundle tool"）
#   - AAB 用 upload keystore 做 JAR 签名（jarsigner），不是 apksigner
#   - 不再需要 zipalign（对齐是 Play 生成 APK 时做的）
#
# 用法：
#   export UPLOAD_KEYSTORE_PASS='<口令>'      # 口令不落盘，只用环境变量传递
#   ./build_aab.sh
#
# 产物： ./out/vesperine-release.aab
#
# 前提： upload-keystore.jks 已存在（已被 .gitignore 排除，不会进仓库）

set -e
cd "$(dirname "$0")"

# ---- 环境：优先用外部传入的；否则自动在仓库的 toolchain/ 里找 ----
# 目的：整个仓库拷到任何路径 / 任何用户名下都能直接构建，不必改脚本。
TOOLCHAIN="$PWD/../toolchain"
if [ -z "${JAVA_HOME:-}" ]; then
  JAVA_HOME="$(ls -d "$TOOLCHAIN"/zulu*/*.jdk/Contents/Home 2>/dev/null | head -1)"
fi
export JAVA_HOME
export ANDROID_HOME="${ANDROID_HOME:-$TOOLCHAIN/android-sdk}"

BUILD_TOOLS="$ANDROID_HOME/build-tools/36.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-36/android.jar"
BUNDLETOOL="${BUNDLETOOL:-$PWD/../toolchain/bundletool-all-1.18.3.jar}"
APP="app/src/main"
OUT="release"                     # 独立目录：build_apk.sh 会 rm -rf out/，不能共用
WORK="$OUT/.aab-build"          # 中间产物；不动 out/ 里已有的 APK
KEYSTORE="${UPLOAD_KEYSTORE:-$PWD/upload-keystore.jks}"
ALIAS="${UPLOAD_KEY_ALIAS:-upload}"
AAB="$OUT/vesperine-release.aab"
# 构建到临时文件，全部验证通过后才替换 $AAB。
# 原因：失败时若直接就地覆盖，会留下一个「看起来存在、实际没签名」的 AAB，
# 而 bundletool validate 照样能过 —— 这种垃圾产物比构建失败更难察觉。
AAB_TMP="$OUT/.vesperine-release.aab.tmp"
# 任何非正常退出（如签名失败）都清掉临时文件，不留下半个产物
trap 'rm -f "$AAB_TMP"' EXIT

echo "── 环境检查 ──"
for f in "$JAVA_HOME/bin/javac" "$JAVA_HOME/bin/jarsigner" "$BUILD_TOOLS/aapt2" "$BUILD_TOOLS/d8" "$PLATFORM"; do
  [ -e "$f" ] || { echo "❌ 缺少: $f"; exit 1; }
done
[ -e "$BUNDLETOOL" ] || { echo "❌ 缺少 bundletool: $BUNDLETOOL"; exit 1; }
[ -e "$KEYSTORE" ]   || { echo "❌ 缺少 upload keystore: $KEYSTORE"; exit 1; }
if [ -z "$UPLOAD_KEYSTORE_PASS" ]; then
  echo "❌ 未设置 UPLOAD_KEYSTORE_PASS"
  echo "   用法：export UPLOAD_KEYSTORE_PASS='<口令>' && ./build_aab.sh"
  exit 1
fi
echo "✅ JDK / aapt2 / d8 / bundletool / keystore / android.jar 就绪"

# jarsigner 真的能跑吗？文件存在 ≠ 能执行：换机后整个 toolchain 常被 macOS
# 打上 com.apple.quarantine，Gatekeeper 会让 jarsigner 直接 SIGKILL（退出码 137）
# 且毫无输出。那种情况下会一直跑到第 7 步才失败，产物还是个无签名的 AAB，
# 而 bundletool validate 照样会过 —— 错误信息完全指不到根因。这里提前拦下来。
if ! "$JAVA_HOME/bin/jarsigner" -help >/dev/null 2>&1; then
  echo "❌ jarsigner 无法运行（常见原因：macOS quarantine 拦截）"
  echo "   修复： xattr -dr com.apple.quarantine \"$TOOLCHAIN\""
  exit 1
fi

rm -rf "$WORK"; mkdir -p "$OUT"; mkdir -p "$WORK"/{compiled,gen,classes,dex,module}

echo
echo "── 1/7 编译资源（aapt2 compile）──"
"$BUILD_TOOLS/aapt2" compile --dir "$APP/res" -o "$WORK/compiled/res.zip"
echo "✅ 资源已编译"

echo
echo "── 2/7 链接资源（普通模式，生成 R.java 供 javac）──"
"$BUILD_TOOLS/aapt2" link -o "$WORK/ref.apk" -I "$PLATFORM" \
  --manifest "$APP/AndroidManifest.xml" --java "$WORK/gen" \
  --min-sdk-version 21 --target-sdk-version 36 \
  --version-code 1 --version-name "0.1.0" \
  "$WORK/compiled/res.zip"
echo "✅ R.java 生成于 $WORK/gen"

echo
echo "── 3/7 编译 Java 源码（javac）──"
find "$APP/java" "$WORK/gen" -name '*.java' > "$WORK/sources.txt"
"$JAVA_HOME/bin/javac" -nowarn -encoding UTF-8 \
  -classpath "$PLATFORM" -d "$WORK/classes" @"$WORK/sources.txt"
CLS=$(find "$WORK/classes" -name '*.class' | wc -l | tr -d ' ')
[ "$CLS" -gt 0 ] || { echo "❌ 没有生成任何 class"; exit 1; }
echo "✅ 编译完成（$CLS 个 class）"

echo
echo "── 4/7 转 dex（d8）──"
"$BUILD_TOOLS/d8" --release --min-api 21 --lib "$PLATFORM" \
  --output "$WORK/dex" $(find "$WORK/classes" -name '*.class')
[ -f "$WORK/dex/classes.dex" ] || { echo "❌ dex 生成失败"; exit 1; }
echo "✅ dex：$(ls -lh "$WORK/dex/classes.dex" | awk '{print $5}')"

echo
echo "── 5/7 链接资源（--proto-format，AAB 用）──"
"$BUILD_TOOLS/aapt2" link --proto-format -o "$WORK/proto.apk" -I "$PLATFORM" \
  --manifest "$APP/AndroidManifest.xml" \
  -A "$APP/assets" \
  --min-sdk-version 21 --target-sdk-version 36 \
  --version-code 1 --version-name "0.1.0" \
  "$WORK/compiled/res.zip"
echo "✅ proto 资源已生成"

echo
echo "── 6/7 组装 base 模块 + 生成 bundle ──"
# base 模块结构：manifest/(proto) + dex/ + res/ + assets/ + resources.pb
cd "$WORK/module"
unzip -q "../proto.apk"
mkdir -p manifest dex
mv AndroidManifest.xml manifest/
cp "../dex/classes.dex" dex/
rm -f "../base.zip"
zip -q -r -X "../base.zip" .
cd - > /dev/null
rm -f "$AAB_TMP"          # bundletool 拒绝覆盖已存在的输出文件
"$JAVA_HOME/bin/java" -jar "$BUNDLETOOL" build-bundle \
  --modules="$WORK/base.zip" --output="$AAB_TMP"
echo "✅ bundle 已生成：$(ls -lh "$AAB_TMP" | awk '{print $5}')"

echo
echo "── 7/7 签名 + 验证（jarsigner / JAR 签名）──"
"$JAVA_HOME/bin/jarsigner" -keystore "$KEYSTORE" \
  -storepass "$UPLOAD_KEYSTORE_PASS" -keypass "$UPLOAD_KEYSTORE_PASS" \
  -sigalg SHA256withRSA -digestalg SHA-256 \
  "$AAB_TMP" "$ALIAS"

echo
echo "── 验证 ──"
"$JAVA_HOME/bin/jarsigner" -verify "$AAB_TMP" >/dev/null 2>&1 \
  && echo "✅ JAR 签名有效" || { echo "❌ 签名验证失败"; rm -f "$AAB_TMP"; exit 1; }
"$JAVA_HOME/bin/java" -jar "$BUNDLETOOL" validate --bundle="$AAB_TMP" >/dev/null 2>&1 \
  && echo "✅ bundle 结构有效（bundletool validate）" || { echo "❌ 结构校验失败"; rm -f "$AAB_TMP"; exit 1; }
"$JAVA_HOME/bin/jarsigner" -verify -verbose -certs "$AAB_TMP" 2>/dev/null \
  | grep -m1 "CN=" | sed 's/^ */  签名者: /'

# 到这里才算真的成功 —— 切换到正式产物（失败时旧产物原封不动）
mv -f "$AAB_TMP" "$AAB"
rm -rf "$WORK"

echo
echo "════════════════════════════════════════"
ls -lh "$AAB"
echo "产物: $(pwd)/$AAB"
echo "下一步: Play Console → 生产 → 创建新版本 → 上传此 .aab"
echo "════════════════════════════════════════"
