#!/bin/zsh
# FlOZE 复刻 —— 测试服务启动器（双击运行）
#
# 双击本文件即可启动服务。首次可能需要在「系统设置 → 隐私与安全性」允许运行。

cd "$(dirname "$0")/floze_clone" || exit 1

echo "=============================================="
echo "  FlOZE 复刻 —— 启动测试服务"
echo "=============================================="
echo

# 读取 API key（优先环境变量，其次本地文件）
if [ -z "$MINIMAX_API_KEY" ] && [ -f "$HOME/.minimax_key" ]; then
  MINIMAX_API_KEY="$(cat "$HOME/.minimax_key")"
fi

if [ -z "$MINIMAX_API_KEY" ]; then
  echo "⚠️  未检测到 MINIMAX_API_KEY"
  echo "    → 服务将以 mock 模式运行（AI 回复是占位文本）"
  echo "    → 要用真实模型，请先执行："
  echo "        echo '你的key' > ~/.minimax_key && chmod 600 ~/.minimax_key"
  echo
else
  echo "✅ 已加载 API key（真实模型模式）"
fi

echo
echo "启动中…服务会一直运行，关闭本窗口即停止"
echo

export HOST=0.0.0.0
export PORT=8080
python3 -u server.py
