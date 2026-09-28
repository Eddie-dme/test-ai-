#!/usr/bin/env bash
#
# Vesperine —— 实例端初始化
#
# 在一台全新的 Amazon Linux 2023 实例上执行（用户 ec2-user）。
# 装环境、建用户、拉代码、装 systemd 单元与 nginx 反代。
#
# 密钥**不在这里** —— 由调用方通过 /etc/vesperine.env 注入，见 deploy.sh。
# 这个脚本刻意可以安全地公开：它只描述结构，不含任何凭证。
#
# 用法（在实例上）：
#   curl -fsSL <raw-url> | bash
#   或  scp provision.sh ec2-user@<ip>: && ssh ec2-user@<ip> 'sudo bash provision.sh'
#
set -euo pipefail

REPO="${REPO:-https://github.com/Eddie-dme/test-ai-.git}"
APP_DIR="/opt/vesperine"
RUN_USER="vesperine"
ENV_FILE="/etc/vesperine.env"
LOG_FILE="/var/log/vesperine.log"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# ── 1. 基础软件
# python3 走标准库即可（server.py 零第三方依赖）；
# nginx 负责 80 → 127.0.0.1:8080 反代，让 Cloudflare 能回源。
say "安装 python3 / git / nginx"
sudo dnf install -y python3 git nginx >/dev/null

# ── 2. 运行用户
# 非特权、无登录 shell。服务以它运行，即使被拿下也拿不到 root。
say "创建运行用户 $RUN_USER"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /sbin/nologin "$RUN_USER"
fi

# ── 3. 代码
say "拉取代码到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" fetch --depth 1 origin main
  sudo git -C "$APP_DIR" reset --hard origin/main
else
  sudo git clone --depth 1 "$REPO" "$APP_DIR"
fi
sudo chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

# ── 4. 日志文件
# systemd 用 append: 直写文件，所以文件必须先存在且属主正确。
say "准备日志文件"
sudo touch "$LOG_FILE"
sudo chown "$RUN_USER:$RUN_USER" "$LOG_FILE"
sudo chmod 640 "$LOG_FILE"

# ── 5. systemd 单元
say "安装 systemd 单元"
sudo tee /etc/systemd/system/vesperine.service >/dev/null <<'UNIT'
[Unit]
Description=Vesperine backend (FlOZE-compatible AI roleplay server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vesperine
Group=vesperine
WorkingDirectory=/opt/vesperine/floze_clone

# 密钥在这里：MINIMAX_API_KEY + ACCESS_KEY
# 权限 600 root:root，服务以 vesperine 身份也能读到，因为 systemd 以 root 解析
EnvironmentFile=/etc/vesperine.env

# HOST 刻意不设成 0.0.0.0 —— 默认 127.0.0.1，只有本机 nginx 能连。
# 旧部署用了 HOST=0.0.0.0，导致 8080 直接暴露在公网上。
ExecStart=/usr/bin/python3 -u server.py

Restart=always
RestartSec=3
StandardOutput=append:/var/log/vesperine.log
StandardError=append:/var/log/vesperine.log

# 轻量加固：服务只需要读代码、写自己的 sqlite 与日志
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

# ── 6. nginx 反代
# Cloudflare 是 Flexible 模式，回源走 HTTP 80，所以这里只监听 80。
say "配置 nginx"
sudo tee /etc/nginx/conf.d/vesperine.conf >/dev/null <<'NGINX'
server {
    listen 80 default_server;
    server_name _;

    # 立绘与静态页可以大一点，但别无限
    client_max_body_size 12m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        # server.py 从 X-Forwarded-For 取真实来源 IP 写访问日志
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ── SSE 必须 ──
        # 开缓冲会让流式输出攒够一块才吐，表现为「卡住半天然后一次刷出来」。
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        chunked_transfer_encoding on;
    }
}
NGINX

# AL2023 自带的默认站点会在 80 上抢 default_server，挪开
if [ -f /etc/nginx/conf.d/default.conf ]; then
  sudo mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.disabled
fi

# ── 7. 启动
say "启动服务"
sudo systemctl daemon-reload
sudo systemctl enable --now nginx
sudo systemctl enable vesperine.service

if [ ! -f "$ENV_FILE" ]; then
  cat <<'WARN'

  ⚠️  /etc/vesperine.env 还不存在 —— 服务现在起不来（这是故意的）。
      请在本地跑 deploy.sh 注入密钥，或手工创建：

        sudo install -m 600 -o root -g root /dev/null /etc/vesperine.env
        sudo tee /etc/vesperine.env >/dev/null <<'EOF'
        MINIMAX_API_KEY=sk-...
        ACCESS_KEY=...
        EOF

      然后：sudo systemctl start vesperine

WARN
else
  sudo systemctl restart vesperine
  sleep 2
  sudo systemctl --no-pager --lines=8 status vesperine || true
fi

say "完成"
echo "  代码      $APP_DIR"
echo "  密钥      $ENV_FILE  (600 root:root)"
echo "  日志      $LOG_FILE"
echo "  本机验证  curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8080/"
