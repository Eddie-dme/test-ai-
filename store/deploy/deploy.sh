#!/usr/bin/env bash
#
# Vesperine —— 本地编排：东京账号从零到服务可用
#
# 前置（必须全部满足，否则跑不起来）：
#   1. aws configure --profile vesperine   （region = ap-northeast-1）
#   2. export MINIMAX_API_KEY=sk-...        （服务端对话模型）
#   3. ACCESS_KEY 自动从 BuildConfig.java 读取
#   4. ~/.ssh/vesperine-key.pem 存在，或设 SSH_KEY 指向新私钥
#
# 用法：
#   export MINIMAX_API_KEY=sk-...
#   ./deploy.sh plan      # 只看要做什么，不创建任何东西
#   ./deploy.sh verify    # 只验账号权限（重建前必做）
#   ./deploy.sh up        # 真建
#
set -euo pipefail

PROFILE="${PROFILE:-vesperine}"
REGION="${REGION:-ap-northeast-1}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.small}"
KEY_NAME="${KEY_NAME:-vesperine-tokyo}"
SG_NAME="${SG_NAME:-vesperine}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_CONFIG="$ROOT_DIR/android_shell/app/src/main/java/top/lurvy/vesperine/BuildConfig.java"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/vesperine-key.pem}"

export AWS_PAGER=""
AWS=(aws --profile "$PROFILE" --region "$REGION"
     --cli-connect-timeout 10 --cli-read-timeout 30)

c()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
no() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }

# ── ACCESS_KEY 从 APK 构建常量里取，不落盘、不进环境变量历史
read_access_key() {
  [ -f "$BUILD_CONFIG" ] || die "找不到 $BUILD_CONFIG"
  python3 - "$BUILD_CONFIG" <<'PY'
import re, sys
s = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'ACCESS_KEY\s*=\s*"([^"]*)"', s)
if not m or not m.group(1):
    sys.exit("BuildConfig.java 里的 ACCESS_KEY 为空")
sys.stdout.write(m.group(1))
PY
}

# ── 动作

do_verify() {
  c "1. 凭据与身份"
  "$@" sts get-caller-identity || die "凭据不可用，先跑 aws configure --profile $PROFILE"
  echo

  c "2. 区域可见性（旧账号被 SCP 锁死过，这里必须过）"
  local regions
  regions=$("${AWS[@]}" ec2 describe-regions --query 'Regions[].RegionName' --output text 2>/dev/null || true)
  if [ -z "$regions" ]; then
    no "describe-regions 无返回 —— 账号可能仍受限制"
  else
    ok "可见 $(echo "$regions" | wc -w | tr -d ' ') 个区域"
  fi
  echo "$regions" | tr '\t' '\n' | grep -qx "$REGION" && ok "$REGION 在列" || no "$REGION 不可见"
  echo

  c "3. 目标区域能开实例吗（dry-run，不实际创建）"
  local ami
  ami=$("${AWS[@]}" ssm get-parameters \
        --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
        --query 'Parameters[0].Value' --output text 2>/dev/null || true)
  [ -n "$ami" ] && ok "AMI: $ami" || warn "取不到 AMI 参数，稍后建实例时再查"

  if [ -n "$ami" ]; then
    if "${AWS[@]}" ec2 run-instances --dry-run \
         --image-id "$ami" --instance-type "$INSTANCE_TYPE" \
         --query 'Instances[0].InstanceId' --output text >/dev/null 2>/tmp/_dryrun.txt; then
      ok "RunInstances 权限通过"
    else
      grep -q DryRunOperation /tmp/_dryrun.txt && ok "RunInstances 权限通过（DryRunOperation）" \
        || { no "RunInstances 被拒："; sed 's/^/      /' /tmp/_dryrun.txt | head -3; }
    fi
  fi
  echo

  c "4. 密钥齐备性"
  [ -n "${MINIMAX_API_KEY:-}" ] && ok "MINIMAX_API_KEY 已在环境里" || no "MINIMAX_API_KEY 未设置"
  local ak
  ak=$(read_access_key 2>/dev/null || true)
  [ -n "$ak" ] && ok "ACCESS_KEY 已从 BuildConfig.java 读到（${#ak} 字符）" || no "读不到 ACCESS_KEY"
  [ -f "$SSH_KEY" ] && ok "SSH 私钥 $SSH_KEY" || no "SSH 私钥缺失：$SSH_KEY"
}

do_plan() {
  cat <<'PLAN'
将要创建的资源（东京 ap-northeast-1）：

  密钥对      vesperine-tokyo     用现有公钥，不生成新的
  安全组      vesperine           22 仅本机出口 IP / 80 全网 / 443 全网
                                  ← 刻意不开 8080：服务监听 127.0.0.1，只给 nginx 用
  实例        t4g.small           2 vCPU / 2 GiB，Amazon Linux 2023 arm64
  磁盘        20 GiB gp3
  弹性 IP     新建并绑定          ← APK 与 Cloudflare 指向它

实例内（provision.sh）：
  python3 / git / nginx，建 vesperine 用户，clone 到 /opt/vesperine，
  systemd 单元 vesperine.service，nginx 80 → 127.0.0.1:8080 反代（关 SSE 缓冲）

随后注入：
  /etc/vesperine.env  600 root:root   MINIMAX_API_KEY + ACCESS_KEY

最后：
  验证 → 切 Cloudflare DNS → 确认无误后停掉悉尼实例
PLAN
}

do_up() {
  [ -n "${MINIMAX_API_KEY:-}" ] || die "先 export MINIMAX_API_KEY=sk-..."
  [ -f "$SSH_KEY" ] || die "SSH 私钥不存在：$SSH_KEY"
  local access_key; access_key=$(read_access_key)

  c "0. 前置自检"
  "${AWS[@]}" sts get-caller-identity --query Account --output text >/dev/null \
    || die "凭据不可用"

  # ── 密钥对：导入现有公钥，这样不用换 SSH 私钥
  c "1. 密钥对 $KEY_NAME"
  local pub
  pub=$(ssh-keygen -y -f "$SSH_KEY") || die "从 $SSH_KEY 导出公钥失败"
  if "${AWS[@]}" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    ok "已存在，跳过"
  else
    "${AWS[@]}" ec2 import-key-pair --key-name "$KEY_NAME" \
      --public-key-material "$(printf '%s' "$pub" | base64)" >/dev/null
    ok "已导入"
  fi

  # ── 安全组
  c "2. 安全组 $SG_NAME"
  local sg
  sg=$("${AWS[@]}" ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
  if [ -z "$sg" ] || [ "$sg" = "None" ]; then
    sg=$("${AWS[@]}" ec2 create-security-group --group-name "$SG_NAME" \
          --description "Vesperine: ssh(admin) + http/https(cloudflare origin)" \
          --query 'GroupId' --output text)
    ok "已创建 $sg"

    # 22：只放管理员出口 IP。这个 IP 是动态的，变了要回来加。
    MYIP="${MYIP:-$(curl -fsS --max-time 8 https://checkip.amazonaws.com 2>/dev/null | tr -d '\n' || true)}"
    [ -n "$MYIP" ] || die "取不到本机出口 IP，请手动 export MYIP=x.x.x.x"
    "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$sg" \
      --ip-permissions \
      "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MYIP/32,Description=admin}]" \
      "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=cloudflare-origin}]" \
      "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=https}]" >/dev/null
    ok "放行 22 ← $MYIP/32，80/443 ← 全网（不开 8080）"
  else
    ok "已存在 $sg"
  fi

  # ── 实例
  c "3. 实例"
  local ami
  ami=$("${AWS[@]}" ssm get-parameters \
        --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
        --query 'Parameters[0].Value' --output text)
  local iid
  iid=$("${AWS[@]}" ec2 run-instances \
        --image-id "$ami" --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" --security-group-ids "$sg" \
        --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3}' \
        --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=vesperine}]' \
        --query 'Instances[0].InstanceId' --output text)
  ok "已创建 $iid"
  "${AWS[@]}" ec2 wait instance-running --instance-ids "$iid"
  ok "running"

  # ── 弹性 IP
  c "4. 弹性 IP"
  local alloc ip
  alloc=$("${AWS[@]}" ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
  "${AWS[@]}" ec2 associate-address --instance-id "$iid" --allocation-id "$alloc" >/dev/null
  ip=$("${AWS[@]}" ec2 describe-addresses --allocation-ids "$alloc" --query 'Addresses[0].PublicIp' --output text)
  ok "$ip"

  # ── 等 SSH 起来
  c "5. 等待 SSH"
  for i in $(seq 1 30); do
    if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=6 \
         -o BatchMode=yes "ec2-user@$ip" true 2>/dev/null; then
      ok "SSH 通了（第 ${i} 次尝试）"; break
    fi
    [ "$i" = 30 ] && die "SSH 一直连不上 —— 检查安全组里的 22 是否放行了当前出口 IP"
    sleep 6
  done

  # ── 初始化
  c "6. 实例初始化"
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -q \
      "$(dirname "${BASH_SOURCE[0]}")/provision.sh" "ec2-user@$ip:/tmp/provision.sh"
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "ec2-user@$ip" "sudo bash /tmp/provision.sh"

  # ── 注入密钥
  c "7. 注入密钥"
  # 走 stdin，不落本地磁盘、不进 shell 历史
  {
    printf 'MINIMAX_API_KEY=%s\n' "$MINIMAX_API_KEY"
    printf 'ACCESS_KEY=%s\n'        "$access_key"
  } | ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "ec2-user@$ip" \
      'umask 077; sudo tee /etc/vesperine.env >/dev/null && sudo chmod 600 /etc/vesperine.env && sudo chown root:root /etc/vesperine.env && sudo systemctl restart vesperine'
  ok "写入并重启"

  # ── 验证
  c "8. 验证"
  sleep 3
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "ec2-user@$ip" \
    "systemctl is-active vesperine && curl -s -o /dev/null -w '  本机静态页 %{http_code}\n' http://127.0.0.1:8080/"
  printf '  公网无 key 应 401: '
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 "http://$ip/api/role/list"
  printf '  公网带 key 应 200: '
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 \
    -H "X-Access-Key: $access_key" "http://$ip/api/role/list"

  cat <<EOF

$(c "完成")
  实例        $iid
  弹性 IP     $ip
  下一步      切 Cloudflare DNS：api.lurvy.top → $ip（代理保持开启）
  记住        22 只放行当前出口 IP；换网/换 IP 后要回来更新安全组
EOF
}

case "${1:-}" in
  plan)   do_plan ;;
  verify) do_verify "${AWS[@]}" ;;
  up)     do_up ;;
  *)      sed -n '2,20p' "$0"; exit 1 ;;
esac
