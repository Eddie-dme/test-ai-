#!/usr/bin/env python3
"""
Vesperine —— 生成图片清理

问题
────
`static/generated/` 里的图片只会增加，从不删除：
  • 用户删除账号 → album_items 行级联删除 → 图片文件变成孤儿，永久占盘
  • 头像图（ugc）同理

设计取舍：只删孤儿，不按时间删
──────────────────────────────
图片本身就是产品价值。用户三个月前生成的图，他现在还想看，
按时间清理会让相册里的图突然变成裂图 —— 那是 bug 不是清理。

所以策略是：
  • 删「数据库里没有引用」的文件（安全的，用户看不到它们）
  • 保留所有仍被 album_items / users.avatar 引用的文件
  • 每天报告目录大小，涨得不正常时能及时发现

安全阀
──────
  • 只处理 mtime 超过 1 小时的文件 —— 正在生成、DB 还没写完的图不动
  • 内置角色立绘（portrait-*.jpeg）永不删除，它们是 seed 数据
  • --dry-run 先看会删什么

用法
────
  python3 cleanup_images.py --dry-run   # 只看，不删
  python3 cleanup_images.py             # 真删
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB = "/opt/vesperine/floze_clone/floze.db"
GEN_DIR = "/opt/vesperine/floze_clone/static/generated"
LOG = "/var/log/vesperine-cleanup.log"

GRACE_SECONDS = 3600          # 1 小时内新生成的不动
KEEP_PATTERNS = ("portrait-",)  # 内置角色立绘，永远保留


def log(msg: str, echo: bool = True) -> None:
    line = "[%s] %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    if echo:
        print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def referenced_files(conn: sqlite3.Connection) -> set[str]:
    """收集所有仍被引用的文件名。"""
    refs: set[str] = set()

    for (path,) in conn.execute("SELECT file_path FROM album_items"):
        if path:
            refs.add(os.path.basename(str(path)))

    # users.avatar 可能是 URL、相对路径或空
    for (av,) in conn.execute("SELECT avatar FROM users WHERE avatar IS NOT NULL AND avatar != ''"):
        av = str(av)
        if "/" in av:
            refs.add(av.rstrip("/").split("/")[-1])
        else:
            refs.add(av)

    return refs


def main() -> int:
    dry = "--dry-run" in sys.argv

    if not os.path.isdir(GEN_DIR):
        log("ERROR 目录不存在: %s" % GEN_DIR)
        return 1
    if not os.path.isfile(DB):
        log("ERROR 数据库不存在: %s" % DB)
        return 1

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    except Exception as e:
        log("ERROR 打开数据库失败: %s" % e)
        return 1

    try:
        refs = referenced_files(conn)
    finally:
        conn.close()

    files = [f for f in os.listdir(GEN_DIR)
             if os.path.isfile(os.path.join(GEN_DIR, f))]

    total_bytes = sum(os.path.getsize(os.path.join(GEN_DIR, f)) for f in files)
    now = time.time()

    orphans: list[tuple[str, int]] = []
    for f in files:
        if any(f.startswith(p) for p in KEEP_PATTERNS):
            continue
        if f in refs:
            continue
        path = os.path.join(GEN_DIR, f)
        st = os.stat(path)
        if now - st.st_mtime < GRACE_SECONDS:
            continue          # 刚生成，可能 DB 还没写完
        orphans.append((f, st.st_size))

    log("扫描 %d 个文件 / %.2f MB；引用 %d 个；待清理 %d 个 / %.2f MB%s"
        % (len(files), total_bytes / 1024 / 1024, len(refs),
           len(orphans), sum(s for _, s in orphans) / 1024 / 1024,
           "（dry-run，未删除）" if dry else ""))

    freed = 0
    for f, size in orphans:
        if dry:
            print("  would delete: %-42s %8d B" % (f, size))
            freed += size
            continue
        try:
            os.remove(os.path.join(GEN_DIR, f))
            freed += size
            print("  deleted: %s" % f)
        except Exception as e:
            log("WARN 删除失败 %s: %s" % (f, e))

    if not dry and orphans:
        log("清理完成，释放 %.2f MB" % (freed / 1024 / 1024))

    # 目录大小告警：超过 500MB 说明清理跟不上生成速度
    if total_bytes > 500 * 1024 * 1024:
        log("WARN generated/ 已达 %.1f MB，考虑移到对象存储或限制单用户配额"
            % (total_bytes / 1024 / 1024))

    return 0


if __name__ == "__main__":
    sys.exit(main())
