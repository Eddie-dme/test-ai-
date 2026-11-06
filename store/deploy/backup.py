#!/usr/bin/env python3
"""
Vesperine —— 数据库每日备份

为什么是这个方案
────────────────
服务器上没有 sqlite3 命令行（只有 Python 的 sqlite3 模块），也没有装 cron。
所以：
  • 备份用 Python 的 Connection.backup() —— 它做的就是 `.backup` 那件事：
    在线热备，不锁库，不需要停服务。直接 cp 一个正在写的 SQLite 文件
    会得到损坏的副本。
  • 定时用 systemd timer —— 系统自带，不依赖 cron。

保留策略
────────
  本地  7 天（快速回滚，重启不丢，因为放在 /var 而非 /tmp）
  S3    90 天（防机器整个没了 / EBS 卷损坏）

每次备份后做 integrity_check + 行数核对 —— 坏的备份比没有备份更危险。

用法
────
  python3 backup.py            # 正常备份
  python3 backup.py --list     # 查看现有备份
  python3 backup.py --restore <文件名>   # 打印恢复命令（不自动执行）
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DB = "/opt/vesperine/floze_clone/floze.db"
LOCAL_DIR = "/var/backups/vesperine"
BUCKET = "vesperine-backups-029938039244"
REGION = "ap-southeast-2"
LOG = "/var/log/vesperine-backup.log"

LOCAL_KEEP_DAYS = 7
S3_KEEP_DAYS = 90


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def sh(cmd: str, timeout: int = 120) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出)。"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 1, str(e)


def do_list() -> None:
    print("=== 本地备份 (%s) ===" % LOCAL_DIR)
    if os.path.isdir(LOCAL_DIR):
        files = sorted(f for f in os.listdir(LOCAL_DIR) if f.endswith(".db"))
        if files:
            for f in files:
                p = os.path.join(LOCAL_DIR, f)
                print("  %-32s %8.1f KB  %s" % (
                    f, os.path.getsize(p) / 1024,
                    datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")))
        else:
            print("  (空)")
    else:
        print("  (目录不存在)")
    print()
    print("=== S3 备份（最近 10 个）===")
    rc, out = sh("aws s3 ls s3://%s/ --region %s" % (BUCKET, REGION))
    lines = [l for l in out.splitlines() if l.strip()][-10:]
    print("\n".join("  " + l for l in lines) if lines else "  (空)")


def do_backup() -> int:
    if not os.path.isfile(DB):
        log("ERROR 数据库不存在: %s" % DB)
        return 1

    os.makedirs(LOCAL_DIR, exist_ok=True)
    os.chmod(LOCAL_DIR, 0o700)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = "floze-%s.db" % stamp
    tmp = os.path.join(LOCAL_DIR, ".tmp-" + name)
    final = os.path.join(LOCAL_DIR, name)

    # ---- 1. 在线热备
    try:
        src = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:
        log("ERROR 备份失败: %s" % e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return 1

    # ---- 2. 校验：完整性 + 行数
    try:
        c = sqlite3.connect(tmp)
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
        if integ != "ok":
            log("ERROR 完整性校验失败: %s" % integ)
            c.close()
            os.remove(tmp)
            return 1
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        c.close()
    except Exception as e:
        log("ERROR 校验异常: %s" % e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return 1

    os.replace(tmp, final)
    os.chmod(final, 0o600)
    size_kb = os.path.getsize(final) / 1024
    log("OK 本地备份 %s (%.1f KB, users=%d, messages=%d)" % (name, size_kb, users, msgs))

    # ---- 3. 上传 S3
    rc, out = sh("aws s3 cp %s s3://%s/%s --region %s --only-show-errors"
                 % (final, BUCKET, name, REGION))
    if rc == 0:
        log("OK 已上传 s3://%s/%s" % (BUCKET, name))
    else:
        log("WARN S3 上传失败（本地备份仍保留）: %s" % out[:200])

    # ---- 4. 本地清理
    cutoff = datetime.now() - timedelta(days=LOCAL_KEEP_DAYS)
    removed = 0
    for f in os.listdir(LOCAL_DIR):
        if not f.startswith("floze-") or not f.endswith(".db"):
            continue
        p = os.path.join(LOCAL_DIR, f)
        if datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
            os.remove(p)
            removed += 1
    kept = len([f for f in os.listdir(LOCAL_DIR) if f.startswith("floze-")])
    log("INFO 本地清理 %d 个，保留 %d 个" % (removed, kept))

    # ---- 5. S3 清理（按文件名里的日期，不看 LastModified）
    rc, out = sh("aws s3 ls s3://%s/ --region %s" % (BUCKET, REGION))
    if rc == 0:
        limit = (datetime.now(timezone.utc) - timedelta(days=S3_KEEP_DAYS)).strftime("%Y%m%d")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            key = parts[3]
            if not key.startswith("floze-"):
                continue
            day = key[6:14]
            if day.isdigit() and day < limit:
                sh("aws s3 rm s3://%s/%s --region %s --only-show-errors"
                   % (BUCKET, key, REGION))
                log("INFO 删除过期 S3 备份 %s" % key)

    log("DONE")
    return 0


def do_restore_hint(name: str) -> None:
    print("恢复步骤（不会自动执行，确认后再跑）：")
    print()
    print("  1. 停服务")
    print("     sudo systemctl stop vesperine")
    print()
    print("  2. 现库改名备份（别直接删）")
    print("     sudo mv %s %s.corrupt-$(date +%%s)" % (DB, DB))
    print()
    print("  3. 从 S3 取回")
    print("     sudo aws s3 cp s3://%s/%s %s --region %s"
          % (BUCKET, name, DB, REGION))
    print("     # 或从本地：sudo cp %s/%s %s" % (LOCAL_DIR, name, DB))
    print()
    print("  4. 改属主 + 起服务")
    print("     sudo chown vesperine:vesperine %s" % DB)
    print("     sudo systemctl start vesperine")
    print()
    print("  5. 验证")
    print("     curl -s -o /dev/null -w '%%{http_code}' http://127.0.0.1:8080/api/role/list")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--list":
        do_list()
        sys.exit(0)
    if arg == "--restore" and len(sys.argv) > 2:
        do_restore_hint(sys.argv[2])
        sys.exit(0)
    sys.exit(do_backup())
