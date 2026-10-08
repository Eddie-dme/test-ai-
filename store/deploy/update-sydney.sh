#!/usr/bin/env bash
#
# Vesperine —— 更新悉尼实例上的服务端
#
# 适用：不改服务器架构、继续用悉尼那台，只把代码从 9/23 的版本
#       更新到最新（+1259 行：合规校验、编辑资料、首屏优化、
#       立绘压缩、401 修复、WAL 维护）。
#
# 为什么不直接在实例上手敲：数据库要按 WAL 规则备份 ——
# 直接 cp 会拿到过期快照（主库 356 KB / -wal 却有 4 MB）。
#
# 前置：SSH 可达（安全组要放行当前出口 IP，动态 IP 变了要重加）
#
# 用法：
#   ./update-sydney.sh check     # 只探连通性与线上版本，不改动
#   ./update-sydney.sh update    # 备份 → 拉代码 → 重启 → 验证
#
set -euo pipefail

HOST_IP="${HOST_IP:-3.107.124.47}"
RP_USER="${RP_USER:-ec2-user}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/vesperine-key.pem}"
APP_DIR="/opt/vesperine"
DB="$APP_DIR/floze_clone/floze.db"

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes
     "$RP_USER@$HOST_IP")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10)

c()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
no() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }

# 目录属主是 vesperine，ec2-user 直接跑 git 会报 dubious ownership。
# sudo 又不继承这个例外（它读的是调用者的 HOME），所以显式 -c 传进去。
GIT=(sudo git -C "$APP_DIR" -c safe.directory="$APP_DIR")

# 实例上**没有** sqlite3 命令行（实测），只能用 python3 的 sqlite3 模块。
# 好处是它本来就装着（服务就是 Python 跑的）。
BACKUP_PY='
import sqlite3, sys
src = sqlite3.connect("/opt/vesperine/floze_clone/floze.db")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
'

need_ssh() {
  "${SSH[@]}" true 2>/dev/null || die "SSH 不通。
  安全组 22 只放行了旧的管理员出口 IP，它变过。
  当前出口 IP 是：$(curl -fsS --max-time 8 https://checkip.amazonaws.com 2>/dev/null || echo '(取不到)')
  要修安全组需要先 aws login 旧账号 —— 凭据已过期。"
}

do_check() {
  c "1. SSH 连通性"
  need_ssh && ok "已连上 $HOST_IP"

  c "2. 实例上的代码版本"
  "${SSH[@]}" "
    ${GIT[*]} log --oneline -3
    echo '  --- 与远端 main 的差距 ---'
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR fetch -q origin main
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR rev-list --count HEAD..origin/main | xargs echo '  落后提交数:'
    echo '  --- 待部署的服务端改动 ---'
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR diff --stat HEAD..origin/main -- floze_clone/
  "

  c "3. 服务状态"
  "${SSH[@]}" "
    echo -n '  systemd: '; systemctl is-active vesperine
    echo -n '  监听地址: '; sudo ss -lnt 2>/dev/null | grep 8080 | awk '{print \$4}' | head -1
    echo '  ↑ 0.0.0.0 表示 8080 对公网开放（单元文件里的 Environment=HOST=0.0.0.0）'
  "

  c "4. 数据库现状（WAL 模式下主库大小会骗人）"
  "${SSH[@]}" "ls -l $DB $DB-wal 2>/dev/null"

  c "5. 当前后端是否含合规校验"
  "${SSH[@]}" "grep -c TERMS_NOT_ACCEPTED $APP_DIR/floze_clone/server.py || echo 0"
}

do_update() {
  need_ssh
  c "0. 前置：数据库备份"
  # sqlite3 的 backup API 是在线备份，会连同 WAL 里未落盘的内容一起快照。
  # 直接用 cp 只复制主库文件，会漏掉 -wal 中的新数据 —— 这个坑踩过。
  local stamp
  stamp=$(date +%Y%m%d-%H%M%S)
  "${SSH[@]}" "sudo -u vesperine python3 -c '$BACKUP_PY' /tmp/floze-$stamp.db && ls -l /tmp/floze-$stamp.db"
  ok "备份到实例上的 /tmp/floze-$stamp.db"

  # 顺手拉回本地一份 —— /tmp 会被清，且实例万一搞坏了本地还有
  mkdir -p "$(dirname "$0")/backups"
  "${SCP[@]}" "$RP_USER@$HOST_IP:/tmp/floze-$stamp.db" "$(dirname "$0")/backups/" >/dev/null
  ok "已取回本地 $(dirname "$0")/backups/floze-$stamp.db"

  c "1. 拉取最新代码"
  "${SSH[@]}" "
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR fetch -q origin main
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR reset --hard origin/main
    sudo git -C $APP_DIR -c safe.directory=$APP_DIR log --oneline -1
  "

  c "2. 重启服务"
  # store.py 的 _migrate() 在启动时跑，会把旧库补到新 schema。
  # 顺序上 _migrate 必须先于 executescript(SCHEMA)，否则索引引用新列会失败 ——
  # 这点代码里已处理，这里只要重启即可。
  "${SSH[@]}" "sudo systemctl restart vesperine && sleep 4 && systemctl is-active vesperine"

  c "3. 验证"
  "${SSH[@]}" "echo -n '  本机静态页 '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/;\
    echo -n '  合规校验代码存在: '; grep -c TERMS_NOT_ACCEPTED $APP_DIR/floze_clone/server.py;\
    echo -n '  8080 是否只监听本机: '; sudo ss -lnt 2>/dev/null | grep 8080 | awk '{print \$4}'"

  echo
  c "4. 公网验证"

  # /api/config 在服务端需要登录（所有 /api/* 都要求），拿它验证 key 会误判：
  # 未登录也是 401，与 key 错误无法区分。实测踩过这个坑。
  # 改用 /api/auth/login —— 它在 /api/auth/ 下不需登录，只需 key，
  # 于是三种结果泾渭分明：
  #   不带 key / 错 key  → UNAUTHORIZED
  #   带对 key           → INVALID_CREDENTIALS（业务逻辑跑到了）
  #   服务端未配 ACCESS_KEY → 也走到业务逻辑
  local key
  key=$(python3 - "$(dirname "$0")/../../android_shell/app/src/main/java/top/lurvy/vesperine/BuildConfig.java" <<'PY'
import re, sys
s = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'ACCESS_KEY\s*=\s*"([^"]*)"', s)
sys.stdout.write(m.group(1) if m else '')
PY
)
  if [ -z "$key" ]; then
    no "读不到 ACCESS_KEY，跳过公网验证"
  else
    local probe='{"email":"nobody@example.com","password":"whatever123"}'
    printf '  不带 key（应 UNAUTHORIZED）:       '
    curl -s --max-time 20 -X POST https://api.lurvy.top/api/auth/login \
      -H 'Content-Type: application/json' -d "$probe" | head -c 60; echo
    printf '  带 key（应 INVALID_CREDENTIALS）:  '
    curl -s --max-time 20 -X POST https://api.lurvy.top/api/auth/login \
      -H 'Content-Type: application/json' -H "X-Access-Key: $key" -d "$probe" | head -c 60; echo
    printf '  条款校验（应 TERMS_NOT_ACCEPTED）: '
    curl -s --max-time 20 -X POST https://api.lurvy.top/api/auth/register \
      -H 'Content-Type: application/json' -H "X-Access-Key: $key" \
      -d "{\"email\":\"probe$RANDOM@example.com\",\"password\":\"ProbeOnly2026x\",\"nickname\":\"p\"}" | head -c 60; echo
    printf '  首屏耗时:                          '
    curl -s -o /dev/null -w '%{time_total}s\n' --max-time 30 https://api.lurvy.top/
  fi

  echo
  c "5. 8080 是否已收敛"
  # curl 失败时 http_code 是 000，它同样匹配 ^[0-9] —— 不能只用正则判断，
  # 否则会把「连不上」误报成「仍可直达」。这里显式取码比对。
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "http://$HOST_IP:8080/" 2>/dev/null || echo 000)
  if [ "$code" = "000" ]; then
    ok "已收敛（直连 $HOST_IP:8080 连不上）"
  else
    no "仍可直达 $HOST_IP:8080（HTTP $code）—— 检查单元文件里的 Environment=HOST"
  fi

  echo
  ok "更新完成。日志：tail -f /var/log/vesperine.log"
}

case "${1:-}" in
  check)  do_check ;;
  update) do_update ;;
  *)      sed -n '2,18p' "$0"; exit 1 ;;
esac
