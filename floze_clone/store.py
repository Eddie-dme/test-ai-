"""
Vesperine 复刻 —— 数据层

表结构与字段名严格对齐从 Web bundle 逆向出的真实契约
（见 floze_reverse/api_contract.txt 与 Vesperine复刻开发规格书.md §5）。

  实体        对齐来源
  ---------   -----------------------------------------------------
  users       /user/settings, /persona/*        （含 persona 字段）
  roles       /role/*, Creator Studio 可编辑字段（角色卡）
  chatrooms   /chatroom/*                       （含 chatMode / model）
  messages    /message/*                        （含 isAdFunded 标记）
  hearts      /heart/list, /heart/history       （heartInfo 结构）
  ad_quota    /ad/times                         （count/limit/cooldown）
  memories    chat diary + starred summaries
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time

import auth
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "floze.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname        TEXT NOT NULL DEFAULT 'Traveller',
    avatar          TEXT DEFAULT '',
    persona_name    TEXT DEFAULT '',      -- /persona/*  用户自称
    persona_desc    TEXT DEFAULT '',
    hearts          INTEGER NOT NULL DEFAULT 0,
    unlimited_until TEXT DEFAULT '',      -- unlimitedHeartsExpiredAt
    email           TEXT,                 -- 登录邮箱（归一化后的小写）
    password_hash   TEXT,                 -- scrypt$salt$hash，见 auth.py
    terms_accepted_at REAL DEFAULT 0,     -- 同意服务条款的时间（合规凭证）
    created_at      REAL NOT NULL
);

-- 邮箱唯一。用部分索引，只为有邮箱的行建约束 ——
-- 改造前的匿名用户 email 为 NULL，不能互相冲突。
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
    ON users(email) WHERE email IS NOT NULL;

-- 访问令牌。独立成表而非塞进 users，这样一个账号可多端登录、
-- 也能单独吊销某一端。
CREATE TABLE IF NOT EXISTS auth_tokens (
    token           TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_used_at    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);

-- 密码重置令牌。
--
-- 只存 sha256(token)，**不存明文**：邮件里那个链接一旦被看到就已经生效了，
-- 而数据库泄露是另一种情况 —— 存明文的话，拿到库就能直接重置任意账号密码。
-- 存 hash 后，泄露的库里没有任何可用的重置凭证。
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    used_at         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);

CREATE TABLE IF NOT EXISTS roles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    personality     TEXT DEFAULT '',
    background      TEXT DEFAULT '',
    first_message   TEXT DEFAULT '',
    status_bar      TEXT DEFAULT '',      -- 官方「Status Bar」功能
    tags            TEXT DEFAULT '[]',    -- 最多 20 个
    creator         TEXT DEFAULT 'system',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chatrooms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    role_id         INTEGER NOT NULL,
    title           TEXT DEFAULT '',
    chat_mode       TEXT NOT NULL DEFAULT 'classic',   -- novel/short_talk/story
    model           TEXT NOT NULL DEFAULT 'standard',  -- standard/quick/smooth/sparkline
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chatroom_id     INTEGER NOT NULL,
    speaker         TEXT NOT NULL,        -- user | assistant
    content         TEXT NOT NULL,
    heart_cost      INTEGER NOT NULL DEFAULT 0,
    is_ad_funded    INTEGER NOT NULL DEFAULT 0,   -- 对应 sendMsg({isAdFunded:true})
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    chatroom_id     INTEGER,
    model           TEXT NOT NULL DEFAULT '',
    chat_mode       TEXT NOT NULL DEFAULT '',
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    estimated       INTEGER NOT NULL DEFAULT 0,  -- 1=上游没回 usage，本行是估算
    mock            INTEGER NOT NULL DEFAULT 0,  -- 1=mock 模式，无真实成本
    blocked         INTEGER NOT NULL DEFAULT 0,  -- 1=输出被安全审核拦下
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS heart_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    delta           INTEGER NOT NULL,
    reason          TEXT NOT NULL,        -- chat/ugc/moment/ad_reward/purchase
    created_at      REAL NOT NULL
);

-- 一次性发放的唯一索引不在 SCHEMA 里建，见 connect() 调用的 _post_schema()。
--
-- 原因：漏洞存在期间，线上可能已经产生了重复的 checkin:<day> 记录。
-- 直接在 SCHEMA 里 CREATE UNIQUE INDEX，遇到那些历史数据会抛 IntegrityError，
-- 而 executescript 是整段执行的 —— 结果是服务直接起不来。
-- 建索引前必须先清理重复，所以拆成单独一步。
--
-- 这个约束本身是必需的：daily_checkin 之前是 has_checked_in() 先 SELECT、
-- add_hearts() 再 INSERT，两步之间没有任何互斥。实测 12 个并发请求能拿到
-- 2 次发放（余额 30→40→50，流水里两条 checkin:2026-11-07）——
-- 一个循环就能把每天 +10 放大成任意倍数。

CREATE TABLE IF NOT EXISTS ad_quota (
    user_id         INTEGER NOT NULL,
    type            TEXT NOT NULL,        -- CHAT | DAILY_HEART
    count           INTEGER NOT NULL DEFAULT 0,
    limit_          INTEGER NOT NULL DEFAULT 3,
    next_available  TEXT DEFAULT '',
    unredeemed      TEXT DEFAULT '[]',    -- unredeemedChatroomIds
    PRIMARY KEY (user_id, type)
);

CREATE TABLE IF NOT EXISTS free_quota (
    user_id         INTEGER NOT NULL,
    type            TEXT NOT NULL,        -- suggestReply/roleUgc/momentPost...
    count           INTEGER NOT NULL DEFAULT 0,
    limit_          INTEGER NOT NULL,
    PRIMARY KEY (user_id, type)
);

CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chatroom_id     INTEGER NOT NULL,
    kind            TEXT NOT NULL,        -- diary | summary | fact
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL
);

-- 动态流（Moment）—— 对齐 /moment/* 契约
CREATE TABLE IF NOT EXISTS moments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    role_id         INTEGER,              -- 可选：关联角色（倒应 /moment/tag-role-candidates）
    tags            TEXT DEFAULT '[]',
    like_count      INTEGER NOT NULL DEFAULT 0,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    is_locked       INTEGER NOT NULL DEFAULT 0,   -- 付费解锁帖
    unlock_cost     INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS moment_likes (
    user_id         INTEGER NOT NULL,
    moment_id       INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, moment_id)
);

CREATE TABLE IF NOT EXISTS moment_saves (
    user_id         INTEGER NOT NULL,
    moment_id       INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, moment_id)
);

CREATE TABLE IF NOT EXISTS moment_comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id       INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL
);

-- 隐藏章节（HiddenChapter）—— 对齐官方说明：
--   每个角色最多 42 章；Affinity 范围 -2000 ~ 2000；分正/负向里程碑；
--   同一个角色的 Affinity 跨所有会话合并计算；章节内发生的事会同步回记忆。
CREATE TABLE IF NOT EXISTS role_affinity (
    user_id         INTEGER NOT NULL,
    role_id         INTEGER NOT NULL,
    value           INTEGER NOT NULL DEFAULT 0,   -- -2000 ~ 2000
    updated_at      REAL NOT NULL,
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS hidden_chapters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id         INTEGER NOT NULL,
    seq             INTEGER NOT NULL DEFAULT 0,   -- 章序，决定展示顺序
    type            TEXT NOT NULL DEFAULT 'positive',  -- positive | negative
    affinity_threshold INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    intro           TEXT NOT NULL DEFAULT '',     -- 未解锁时可见的无剧透简介
    content         TEXT NOT NULL DEFAULT '',     -- 解锁后的正文（创作者填）
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter_unlocks (
    user_id         INTEGER NOT NULL,
    chapter_id      INTEGER NOT NULL,
    unlocked_at     REAL NOT NULL,
    entered_at      REAL,
    PRIMARY KEY (user_id, chapter_id)
);

-- Affinity 变化流水（便于回溯哪个对话造成了变动）
CREATE TABLE IF NOT EXISTS affinity_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    role_id         INTEGER NOT NULL,
    delta           INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);

-- 情景（Scenario）—— 对齐 /scenario/*
-- 产品含义：可被其他用户发现、点赞、开聊的「剧本/情景卡」
CREATE TABLE IF NOT EXISTS scenarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',     -- 对应 /scenario/location
    category        TEXT NOT NULL DEFAULT '',     -- 对应 /scenario/category
    cover           TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    opener          TEXT NOT NULL DEFAULT '',     -- 开场设定，开聊时用
    is_public       INTEGER NOT NULL DEFAULT 1,   -- Public / Unlisted（对齐官方）
    like_count      INTEGER NOT NULL DEFAULT 0,
    play_count      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_likes (
    user_id         INTEGER NOT NULL,
    scenario_id     INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, scenario_id)
);

-- 创作者（Creator）与关注关系（Follower / Following）
CREATE TABLE IF NOT EXISTS creators (
    user_id         INTEGER PRIMARY KEY,
    display_name    TEXT NOT NULL DEFAULT '',
    bio             TEXT NOT NULL DEFAULT '',
    role_count      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS creator_blocks (
    user_id         INTEGER NOT NULL,
    blocked_id      INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (user_id, blocked_id)
);

-- status: pending（待同意）| accepted（已关注）
CREATE TABLE IF NOT EXISTS follows (
    follower_id     INTEGER NOT NULL,
    following_id    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'accepted',
    created_at      REAL NOT NULL,
    PRIMARY KEY (follower_id, following_id)
);

-- 关注通知的已读状态
CREATE TABLE IF NOT EXISTS follow_reads (
    user_id         INTEGER NOT NULL,
    read_at         REAL NOT NULL,
    PRIMARY KEY (user_id)
);

-- 相册（Album）—— 对齐 /album/generate, /album/list
-- 同时兼作 UGC 头像生成的结果存放（roleId 为空时是头像）
CREATE TABLE IF NOT EXISTS album_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    role_id         INTEGER,
    kind            TEXT NOT NULL DEFAULT 'image',   -- image | video
    source          TEXT NOT NULL DEFAULT 'ugc',     -- ugc(头像) | album(相册)
    file_path       TEXT NOT NULL,
    prompt          TEXT NOT NULL DEFAULT '',
    style           TEXT NOT NULL DEFAULT '',
    heart_cost      INTEGER NOT NULL DEFAULT 0,
    is_unread       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

-- 任务（Quest）—— 对齐 /quest/dashboard, /quest/list
CREATE TABLE IF NOT EXISTS quests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,        -- daily | goal
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    target          INTEGER NOT NULL DEFAULT 1,   -- 需完成次数
    reward_hearts   INTEGER NOT NULL DEFAULT 1,
    event_key       TEXT NOT NULL DEFAULT ''     -- 统计哪类行为（chat/comment/ugc…）
);

CREATE TABLE IF NOT EXISTS user_quests (
    user_id         INTEGER NOT NULL,
    quest_id        INTEGER NOT NULL,
    progress        INTEGER NOT NULL DEFAULT 0,
    claimed         INTEGER NOT NULL DEFAULT 0,
    day             TEXT NOT NULL DEFAULT '',    -- 日常任务按天重置
    updated_at      REAL NOT NULL,
    PRIMARY KEY (user_id, quest_id, day)
);

-- 成就（Achievement）
CREATE TABLE IF NOT EXISTS achievements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    icon            TEXT NOT NULL DEFAULT '★',
    threshold       INTEGER NOT NULL DEFAULT 1,
    metric          TEXT NOT NULL DEFAULT ''     -- 统计维度
);

CREATE TABLE IF NOT EXISTS user_achievements (
    user_id         INTEGER NOT NULL,
    achievement_id  INTEGER NOT NULL,
    unlocked_at     REAL NOT NULL,
    PRIMARY KEY (user_id, achievement_id)
);

-- 通知（Notification）—— 对齐 /notification/*
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'system',  -- role|post|moment|comment|system
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

-- 贴纸（Sticker）—— 对齐 /sticker/category-list
CREATE TABLE IF NOT EXISTS stickers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,
    name            TEXT NOT NULL,
    emoji           TEXT NOT NULL DEFAULT '★'
);

CREATE TABLE IF NOT EXISTS user_stickers (
    user_id         INTEGER NOT NULL,
    sticker_id      INTEGER NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1,
    obtained_at     REAL NOT NULL,
    PRIMARY KEY (user_id, sticker_id)
);

-- 内容安全事件留证。
-- 只记被拦截的请求，不记正常内容；excerpt 截断后存，
-- 目的是事发时能自证已尽合理义务，不是收集用户数据。
CREATE TABLE IF NOT EXISTS moderation_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    field           TEXT NOT NULL DEFAULT '',   -- 触发位置：message | role_bg | moment | prompt ...
    severity        TEXT NOT NULL DEFAULT '',   -- csam | other
    reason          TEXT NOT NULL DEFAULT '',
    excerpt         TEXT NOT NULL DEFAULT '',   -- 截断的触发片段（≤200 字符）
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moderation_user ON moderation_events(user_id, created_at);

-- IAP 订单留痕。
-- purchase_token 加 UNIQUE：同一个 token 只能兑换一次，
-- 防客户端重放同一个 token 反复领取。
CREATE TABLE IF NOT EXISTS iap_purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    plan_id         TEXT NOT NULL,
    purchase_token  TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT '',   -- verified | failed | compensated
    note            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iap_user ON iap_purchases(user_id, created_at);

-- 举报（UGC 产品的上架硬门槛）。
-- Apple Review Guidelines 与 Google Play 政策都要求 UGC 应用
-- 提供举报与屏蔽机制，否则直接拒审。
--
-- snapshot 存举报当时的内容快照：事后内容可能被删除或修改，
-- 没有快照就无法复核工单。截断存储，避免完整落库违规内容。
-- UNIQUE 约束：同一人对同一目标重复举报只记一次，防刷。
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id     INTEGER NOT NULL,
    target_type     TEXT NOT NULL,            -- role | moment | comment | user | album | scenario
    target_id       INTEGER NOT NULL,
    reason          TEXT NOT NULL,            -- csam | sexual | violence | hate | harassment | spam | ip | other
    detail          TEXT NOT NULL DEFAULT '',
    snapshot        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | reviewed | actioned | dismissed
    priority        TEXT NOT NULL DEFAULT 'normal',   -- normal | urgent（csam 自动升级）
    created_at      REAL NOT NULL,
    UNIQUE(reporter_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, priority, created_at);

-- 官网线索（lurvy.top 联系表单）
-- 独立于用户体系：填表的人不是 App 用户，不需要账号。
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    company         TEXT NOT NULL DEFAULT '',
    contact         TEXT NOT NULL,
    need            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'website',  -- website | api
    ip              TEXT NOT NULL DEFAULT '',
    ua              TEXT NOT NULL DEFAULT '',
    mail_status     TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | skipped
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at DESC);
"""


def now() -> float:
    return time.time()


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的旧库补列。

    CREATE TABLE IF NOT EXISTS 对已建好的表不会生效，所以新增字段
    必须显式 ALTER。每次启动跑一遍，幂等。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if not cols:
        # 空库：users 表还不存在。SCHEMA 里的 CREATE TABLE 会直接建成
        # 含全部字段的完整表，不需要补列。
        # 不早退的话，下面第一句 ALTER 就会失败（no such table），
        # 而这次失败发生在建表之前 —— 结果是新机器 / 删库后无法启动。
        return
    for col, ddl in (("email", "TEXT"), ("password_hash", "TEXT"),
                     ("terms_accepted_at", "REAL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    conn.commit()


def _dedupe_checkin_ledger(conn: sqlite3.Connection) -> int:
    """把历史上重复的签到流水改名，为唯一索引让路。返回改名条数。

    为什么是「改名」而不是「删除」：多发放的 hearts 已经计入
    users.hearts 了，删掉流水会让账目两边对不上，也没了审计痕迹。
    改成 checkin:<day>#dup2 这样的后缀，既保留完整历史，
    又让 (user_id, reason) 重新唯一。

    保留 id 最小（最早）的那一条原样不动 —— 它是合法的那次签到；
    has_checked_in() 用 reason == 'checkin:'+day 精确匹配，
    改名后的记录不会被误当成「已签到」。
    """
    dup = conn.execute(
        "SELECT user_id, reason, COUNT(*) AS c FROM heart_ledger"
        " WHERE reason GLOB 'checkin:*'"
        " GROUP BY user_id, reason HAVING c > 1").fetchall()
    renamed = 0
    for user_id, reason, _cnt in dup:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM heart_ledger WHERE user_id=? AND reason=? ORDER BY id",
            (user_id, reason)).fetchall()]
        for i, rid in enumerate(ids[1:], start=2):
            conn.execute("UPDATE heart_ledger SET reason=? WHERE id=?",
                         (f"{reason}#dup{i}", rid))
            renamed += 1
    if renamed:
        conn.commit()
    return renamed


def _post_schema(conn: sqlite3.Connection) -> None:
    """建表之后才能做的事：清理历史重复数据，再建立一次性发放的唯一索引。

    单独成一步而不是写进 SCHEMA，是因为历史重复数据会让
    CREATE UNIQUE INDEX 抛 IntegrityError，而 executescript 是整段执行，
    一旦抛就是服务起不来 —— 删库或新机器没问题，升级旧库必炸。
    """
    try:
        _dedupe_checkin_ledger(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_heart_ledger_once"
            " ON heart_ledger(user_id, reason) WHERE reason GLOB 'checkin:*'")
        conn.commit()
    except sqlite3.Error as e:
        # 建不上索引就意味着 add_hearts_once 的判重退化成「靠运气」，
        # 这必须被看见，不能默默继续跑。
        print(f"⚠️  heart_ledger 唯一索引建立失败：{e} "
              f"—— 签到并发防护未生效，需人工检查", file=sys.stderr)


def connect(init_schema: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if init_schema:
        # 必须先补列：SCHEMA 里的 idx_users_email 索引引用 email 列，
        # 而 CREATE TABLE IF NOT EXISTS 对已存在的表不会生效 ——
        # 顺序倒过来的话，旧库会在建索引时报 no such column。
        _migrate(conn)
        conn.executescript(SCHEMA)          # 幂等
        _post_schema(conn)                  # 去重 + 唯一索引
    return conn


# ------------------------------------------------------------------ 响应封装

def ok(data: Any = None) -> dict:
    """对齐 Vesperine 响应约定：{flag, msg, data}，flag=0 为成功。"""
    return {"flag": 0, "msg": "", "data": data}


def err(msg: str, flag: int = 1) -> dict:
    return {"flag": flag, "msg": msg, "data": None}


# ------------------------------------------------------------------ 业务方法

class Store:
    def __init__(self, conn: sqlite3.Connection, user_id: int | None = None):
        self.c = conn
        # 当前请求所属用户。None 表示未指定 —— 用于本地开发与改造前的
        # 匿名数据，此时 ensure_user() 回退到第一个用户。
        self.user_id = user_id

    # ---- user -------------------------------------------------------
    def ensure_user(self) -> dict:
        """返回当前请求的用户。

        已登录（self.user_id 有值）时按 id 取；否则回退到第一个用户，
        保持与改造前一致的行为，旧的匿名数据仍可访问。
        """
        if self.user_id is not None:
            row = self.c.execute(
                "SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()
            if row:
                return dict(row)
        row = self.c.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
        if row:
            return dict(row)
        self.c.execute(
            "INSERT INTO users (nickname, persona_name, persona_desc, hearts, created_at)"
            " VALUES (?,?,?,?,?)",
            ("Traveller", "Elara",
             "A mortal cartographer, bold and unafraid of things that should frighten her.",
             30, now()))
        self.c.commit()
        return self.ensure_user()

    # ---- 认证 -------------------------------------------------------

    def user_by_email(self, email: str) -> dict | None:
        row = self.c.execute(
            "SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

    def create_user(self, email: str, password_hash: str,
                    nickname: str = "Traveller", persona_name: str = "",
                    persona_desc: str = "",
                    free_quota: dict | None = None,
                    starter_hearts: int = 30) -> dict:
        """新建账号。

        `free_quota` 必须传：免费额度行不会「懒初始化」—— 原注释这么写，
        但代码里并不存在那段逻辑，结果是**注册用户一条免费额度都没有**，
        发第一条动态就要付 3 hearts、第一条评论付 2，
        而 bootstrap 只给 `ensure_user()` 那一个用户建了行。
        """
        cur = self.c.execute(
            "INSERT INTO users (nickname, persona_name, persona_desc, hearts,"
            " email, password_hash, terms_accepted_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (nickname, persona_name, persona_desc, starter_hearts,
             email, password_hash, now(), now()))
        uid = cur.lastrowid
        for t, lim in (free_quota or {}).items():
            self.c.execute(
                "INSERT OR IGNORE INTO free_quota (user_id, type, count, limit_)"
                " VALUES (?,?,0,?)", (uid, t, lim))
        self.c.commit()
        return dict(self.c.execute(
            "SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def update_user_profile(self, user_id: int, nickname: str | None = None,
                            persona_name: str | None = None,
                            persona_desc: str | None = None) -> dict:
        """更新用户资料。只动传入的字段，None 表示不修改。

        persona_name 是「用户自称」—— 角色扮演里模型会用它称呼用户
        （实测回复里出现过 *She looks at Elara...*，那个 Elara 就来自这里）。
        """
        sets, args = [], []
        if nickname is not None:
            sets.append("nickname=?")
            args.append(nickname)
        if persona_name is not None:
            sets.append("persona_name=?")
            args.append(persona_name)
        if persona_desc is not None:
            sets.append("persona_desc=?")
            args.append(persona_desc)
        if sets:
            args.append(user_id)
            self.c.execute("UPDATE users SET %s WHERE id=?" % ", ".join(sets), args)
            self.c.commit()
        return dict(self.c.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def set_password(self, user_id: int, password_hash: str) -> None:
        self.c.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (password_hash, user_id))
        self.c.commit()

    def create_token(self, user_id: int, ttl_seconds: int) -> str:
        token = auth.new_token()
        t = now()
        self.c.execute(
            "INSERT INTO auth_tokens (token, user_id, created_at, expires_at,"
            " last_used_at) VALUES (?,?,?,?,?)",
            (token, user_id, t, t + ttl_seconds, t))
        self.c.commit()
        return token

    def user_id_by_token(self, token: str) -> int | None:
        """校验令牌，返回 user_id。过期即删除并返回 None。"""
        if not token:
            return None
        row = self.c.execute(
            "SELECT user_id, expires_at FROM auth_tokens WHERE token=?",
            (token,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < now():
            self.revoke_token(token)
            return None
        self.c.execute("UPDATE auth_tokens SET last_used_at=? WHERE token=?",
                       (now(), token))
        self.c.commit()
        return row["user_id"]

    def revoke_token(self, token: str) -> None:
        self.c.execute("DELETE FROM auth_tokens WHERE token=?", (token,))
        self.c.commit()

    # ---- 密码重置 ---------------------------------------------------
    @staticmethod
    def _reset_hash(token: str) -> str:
        """重置令牌的存储形式。发出去的是明文，库里只有这个。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_password_reset(self, user_id: int, ttl_seconds: int = 3600) -> str:
        """生成一次性重置令牌，返回明文（只在这一刻存在于内存/邮件里）。

        先作废该用户之前未用的令牌：用户点了两次「发邮件」时，
        只有最新的那封能用，不至于邮件箱里每一封都是有效凭证。
        """
        self.c.execute(
            "DELETE FROM password_resets WHERE user_id=? AND used_at=0",
            (user_id,))
        token = auth.new_token()
        t = now()
        self.c.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at,"
            " used_at) VALUES (?,?,?,?,0)",
            (self._reset_hash(token), user_id, t, t + ttl_seconds))
        self.c.commit()
        return token

    def consume_password_reset(self, token: str) -> int | None:
        """校验并**一次性**消费重置令牌，返回 user_id。

        标记已用与返回必须原子：先标 used_at，用 rowcount 判断自己是不是
        第一个，再返回 user_id。否则同一个链接并发点两下会重置两次。
        """
        if not token:
            return None
        h = self._reset_hash(token)
        row = self.c.execute(
            "SELECT user_id, expires_at, used_at FROM password_resets"
            " WHERE token_hash=?", (h,)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now():
            return None
        cur = self.c.execute(
            "UPDATE password_resets SET used_at=? WHERE token_hash=? AND used_at=0",
            (now(), h))
        won = cur.rowcount == 1
        self.c.commit()
        return row["user_id"] if won else None

    def set_password(self, user_id: int, password_hash: str) -> None:
        self.c.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (password_hash, user_id))
        self.c.commit()

    # 删除账号时要清空的表。顺序无关（都是按 user_id 平铺删除），
    # 但 messages 例外 —— 它没有 user_id，得靠 chatroom 间接定位。
    _USER_SCOPED_TABLES = (
        "auth_tokens", "heart_ledger", "ad_quota", "free_quota",
        "moments", "moment_likes", "moment_saves", "moment_comments",
        "role_affinity", "chapter_unlocks", "affinity_log", "scenario_likes",
        "creators", "creator_blocks", "follow_reads", "album_items",
        "user_quests", "user_achievements", "notifications", "user_stickers",
        "moderation_events", "iap_purchases", "llm_usage", "password_resets",
    )

    def delete_user(self, user_id: int) -> dict:
        """删除账号及其全部关联数据，返回各表删除行数。

        商店审核要求应用内提供账号删除能力，且必须真的清掉数据 ——
        只把 users 行删掉、留下孤儿数据既不合规也会留下隐私残留。
        """
        counts: dict = {}

        # messages 没有 user_id，先按该用户的 chatroom 定位并删除，
        # 否则 chatrooms 删掉后就再也找不到它们了。
        cur = self.c.execute(
            "DELETE FROM messages WHERE chatroom_id IN"
            " (SELECT id FROM chatrooms WHERE user_id=?)", (user_id,))
        if cur.rowcount:
            counts["messages"] = cur.rowcount

        cur = self.c.execute("DELETE FROM chatrooms WHERE user_id=?", (user_id,))
        if cur.rowcount:
            counts["chatrooms"] = cur.rowcount

        for table in self._USER_SCOPED_TABLES:
            cur = self.c.execute(
                "DELETE FROM %s WHERE user_id=?" % table, (user_id,))
            if cur.rowcount:
                counts[table] = cur.rowcount

        # 用户创建的情景用 creator_id 而非 user_id
        cur = self.c.execute("DELETE FROM scenarios WHERE creator_id=?", (user_id,))
        if cur.rowcount:
            counts["scenarios"] = cur.rowcount

        cur = self.c.execute("DELETE FROM users WHERE id=?", (user_id,))
        counts["users"] = cur.rowcount
        self.c.commit()
        return counts

    def revoke_all_tokens(self, user_id: int) -> None:
        """吊销某用户全部令牌 —— 改密码后应调用。"""
        self.c.execute("DELETE FROM auth_tokens WHERE user_id=?", (user_id,))
        self.c.commit()

    def prune_expired_tokens(self) -> int:
        cur = self.c.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now(),))
        self.c.commit()
        return cur.rowcount

    def add_hearts(self, user_id: int, delta: int, reason: str) -> int:
        self.c.execute("UPDATE users SET hearts = MAX(0, hearts + ?) WHERE id=?",
                       (delta, user_id))
        self.c.execute(
            "INSERT INTO heart_ledger (user_id, delta, reason, created_at) VALUES (?,?,?,?)",
            (user_id, delta, reason, now()))
        self.c.commit()
        r = self.c.execute("SELECT hearts FROM users WHERE id=?", (user_id,)).fetchone()
        return r["hearts"]

    def add_hearts_once(self, user_id: int, delta: int, reason: str) -> dict:
        """只在流水里没有相同 (user_id, reason) 时发放，保证「一天只能领一次」。

        与 add_hearts 的区别：那个无条件发，这个由唯一索引兜底。
        调用方不再需要先查一遍再写 —— 「先查后写」正是并发双发的根因。

        实现要点：
          - 先 INSERT 流水，用 rowcount 判断是不是真的首次。
            **不能先写余额再写流水**：那样并发时两边都会加余额，
            只是最后流水被索引挡住，钱已经多发了。
          - 插入与余额更新在同一个隐式事务里，中间不 commit。
          - 返回值带 granted，调用方据此决定回「已签到」还是发成功。

        依赖 idx_heart_ledger_once（见 _post_schema）。若索引意外缺失，
        INSERT OR IGNORE 会真的插入重复行，所以 _post_schema 建失败时会告警。
        """
        try:
            cur = self.c.execute(
                "INSERT OR IGNORE INTO heart_ledger"
                " (user_id, delta, reason, created_at) VALUES (?,?,?,?)",
                (user_id, delta, reason, now()))
            granted = cur.rowcount == 1
            if granted:
                self.c.execute(
                    "UPDATE users SET hearts = MAX(0, hearts + ?) WHERE id=?",
                    (delta, user_id))
            self.c.commit()
        except sqlite3.IntegrityError:
            # OR IGNORE 本不该抛，但索引缺失时唯一约束行为可能不同，兜底。
            self.c.rollback()
            granted = False
        r = self.c.execute("SELECT hearts FROM users WHERE id=?", (user_id,)).fetchone()
        return {"granted": granted, "balance": r["hearts"]}

    def heart_info(self, user_id: int) -> dict:
        r = self.c.execute(
            "SELECT hearts, unlimited_until FROM users WHERE id=?", (user_id,)).fetchone()
        return {"amount": r["hearts"], "unlimitedHeartsExpiredAt": r["unlimited_until"]}

    # ---- LLM 用量 -----------------------------------------------------
    def record_usage(self, user_id: int, chatroom_id: int | None, chat_mode: str,
                     usage: dict, blocked: bool = False) -> None:
        """记一条模型调用用量。

        失败时静默 —— 计量是旁路，不能因为它写不进去就让一次聊天失败。
        （写入失败会在 stdout 留下告警，不至于无影无踪。）
        """
        try:
            self.c.execute(
                "INSERT INTO llm_usage (user_id, chatroom_id, model, chat_mode,"
                " input_tokens, output_tokens, cached_tokens, estimated, mock,"
                " blocked, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, chatroom_id, usage.get("model", ""), chat_mode,
                 int(usage.get("input_tokens") or 0),
                 int(usage.get("output_tokens") or 0),
                 int(usage.get("cached_tokens") or 0),
                 1 if usage.get("estimated") else 0,
                 1 if usage.get("mock") else 0,
                 1 if blocked else 0,
                 now()))
            self.c.commit()
        except sqlite3.Error as e:
            print(f"⚠️  llm_usage 写入失败：{e}", file=sys.stderr)

    # ---- role -------------------------------------------------------
    def list_roles(self) -> list[dict]:
        rows = self.c.execute("SELECT * FROM roles ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out

    def get_role(self, role_id: int) -> dict | None:
        r = self.c.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    # ---- chatroom ---------------------------------------------------
    def list_chatrooms(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT c.*, r.name AS role_name"
            " FROM chatrooms c JOIN roles r ON r.id=c.role_id"
            " WHERE c.user_id=? ORDER BY c.updated_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def create_chatroom(self, user_id: int, role_id: int, chat_mode: str, model: str,
                        with_intro: bool = True) -> dict:
        role = self.get_role(role_id)
        ts = now()
        cur = self.c.execute(
            "INSERT INTO chatrooms (user_id, role_id, title, chat_mode, model,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, role_id, role["name"] if role else "", chat_mode, model, ts, ts))
        cid = cur.lastrowid
        # 开场白：对齐 Vesperine 的 first_message
        if with_intro and role and role["first_message"]:
            self.c.execute(
                "INSERT INTO messages (chatroom_id, speaker, content, created_at)"
                " VALUES (?,?,?,?)", (cid, "assistant", role["first_message"], ts))
        self.c.commit()
        return {"id": cid, "roleId": role_id, "chatMode": chat_mode, "model": model}

    def find_chatroom(self, user_id: int, role_id: int) -> dict | None:
        """同一用户 + 同一角色最近一次会话。

        用于「点开始对话 = 继续聊」而不是「再开一个窗口」。
        按 updated_at 倒序取最新的一条：即使历史数据里已经有重复会话
        （旧版本每次点击都新建），也总是进入用户最近在用的那一个。
        """
        row = self.c.execute(
            "SELECT * FROM chatrooms WHERE user_id=? AND role_id=?"
            " ORDER BY updated_at DESC, id DESC LIMIT 1",
            (user_id, role_id)).fetchone()
        return dict(row) if row else None

    def set_chatroom_mode(self, cid: int, chat_mode: str) -> None:
        """切换会话档位。

        档位决定实际计费（prepare_send 读 chatrooms.chat_mode），
        所以这里必须真的落库 —— 只改前端显示就是「显示 5 扣 2」。
        """
        self.c.execute("UPDATE chatrooms SET chat_mode=?, updated_at=? WHERE id=?",
                       (chat_mode, now(), cid))
        self.c.commit()

    def touch_chatroom(self, cid: int) -> None:
        self.c.execute("UPDATE chatrooms SET updated_at=? WHERE id=?", (now(), cid))
        self.c.commit()

    # ---- message ----------------------------------------------------
    def list_messages(self, chatroom_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT * FROM messages WHERE chatroom_id=? ORDER BY id",
            (chatroom_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_message(self, chatroom_id: int, speaker: str, content: str,
                    cost: int = 0, is_ad_funded: int = 0) -> int:
        cur = self.c.execute(
            "INSERT INTO messages (chatroom_id, speaker, content, heart_cost,"
            " is_ad_funded, created_at) VALUES (?,?,?,?,?,?)",
            (chatroom_id, speaker, content, cost, is_ad_funded, now()))
        self.c.commit()
        return cur.lastrowid

    def history_for_llm(self, chatroom_id: int, max_turns: int = 40) -> list[tuple]:
        rows = self.c.execute(
            "SELECT speaker, content FROM messages WHERE chatroom_id=?"
            " ORDER BY id DESC LIMIT ?", (chatroom_id, max_turns)).fetchall()
        return [(r["speaker"], r["content"]) for r in reversed(rows)]

    # ---- ad ---------------------------------------------------------
    def ad_state(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT * FROM ad_quota WHERE user_id=?", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def ensure_ad_quota(self, user_id: int) -> None:
        for t, lim in (("CHAT", 3), ("DAILY_HEART", 3)):
            self.c.execute(
                "INSERT OR IGNORE INTO ad_quota (user_id, type, count, limit_)"
                " VALUES (?,?,0,?)", (user_id, t, lim))
        self.c.commit()

    def bump_ad(self, user_id: int, ad_type: str, next_available: str = "") -> dict:
        row = self.c.execute(
            "SELECT * FROM ad_quota WHERE user_id=? AND type=?",
            (user_id, ad_type)).fetchone()
        if not row:
            return {"flag": 1, "msg": "over limit", "data": None}
        if row["count"] >= row["limit_"]:
            return {"flag": 1, "msg": "over limit", "data": None}
        self.c.execute(
            "UPDATE ad_quota SET count=count+1, next_available=? WHERE user_id=? AND type=?",
            (next_available, user_id, ad_type))
        self.c.commit()
        return ok({"type": ad_type, "count": row["count"] + 1, "limit": row["limit_"],
                   "nextAvailableTime": next_available})

    # ---- memory -----------------------------------------------------
    def add_memory(self, chatroom_id: int, kind: str, content: str) -> None:
        self.c.execute(
            "INSERT INTO memories (chatroom_id, kind, content, created_at) VALUES (?,?,?,?)",
            (chatroom_id, kind, content, now()))
        self.c.commit()

    def memory_block(self, chatroom_id: int, limit: int = 8) -> str:
        rows = self.c.execute(
            "SELECT kind, content FROM memories WHERE chatroom_id=?"
            " ORDER BY id DESC LIMIT ?", (chatroom_id, limit)).fetchall()
        if not rows:
            return ""
        lines = ["[Long-term memory — treat as established facts]"]
        for r in reversed(rows):
            prefix = {"diary": "Diary", "summary": "Key moment", "fact": "Fact"}.get(
                r["kind"], "Note")
            lines.append(f"- {prefix}: {r['content']}")
        return "\n".join(lines)

    # ---- moment（动态流，对齐 /moment/*）------------------------------
    def list_moments(self, user_id: int, limit: int = 30) -> list[dict]:
        rows = self.c.execute(
            "SELECT m.*, r.name AS role_name FROM moments m"
            " LEFT JOIN roles r ON r.id = m.role_id"
            " ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            d["liked"] = bool(self.c.execute(
                "SELECT 1 FROM moment_likes WHERE user_id=? AND moment_id=?",
                (user_id, d["id"])).fetchone())
            d["saved"] = bool(self.c.execute(
                "SELECT 1 FROM moment_saves WHERE user_id=? AND moment_id=?",
                (user_id, d["id"])).fetchone())
            out.append(d)
        return out

    def create_moment(self, user_id: int, content: str,
                      role_id: int | None, tags: list[str]) -> int:
        cur = self.c.execute(
            "INSERT INTO moments (user_id, content, role_id, tags, created_at)"
            " VALUES (?,?,?,?,?)",
            (user_id, content, role_id, json.dumps(tags or []), now()))
        self.c.commit()
        return cur.lastrowid

    def get_moment(self, moment_id: int) -> dict | None:
        r = self.c.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    def toggle_like(self, user_id: int, moment_id: int, on: bool) -> int:
        if on:
            self.c.execute("INSERT OR IGNORE INTO moment_likes (user_id, moment_id,"
                           " created_at) VALUES (?,?,?)", (user_id, moment_id, now()))
        else:
            self.c.execute("DELETE FROM moment_likes WHERE user_id=? AND moment_id=?",
                           (user_id, moment_id))
        n = self.c.execute("SELECT COUNT(*) c FROM moment_likes WHERE moment_id=?",
                           (moment_id,)).fetchone()["c"]
        self.c.execute("UPDATE moments SET like_count=? WHERE id=?", (n, moment_id))
        self.c.commit()
        return n

    def toggle_save(self, user_id: int, moment_id: int, on: bool) -> bool:
        if on:
            self.c.execute("INSERT OR IGNORE INTO moment_saves (user_id, moment_id,"
                           " created_at) VALUES (?,?,?)", (user_id, moment_id, now()))
        else:
            self.c.execute("DELETE FROM moment_saves WHERE user_id=? AND moment_id=?",
                           (user_id, moment_id))
        self.c.commit()
        return on

    def purchase_seen(self, token: str) -> bool:
        """该 purchaseToken 是否已处理过（幂等）。"""
        row = self.c.execute(
            "SELECT 1 FROM iap_purchases WHERE purchase_token=? LIMIT 1",
            (token,)).fetchone()
        return row is not None

    def record_purchase(self, user_id: int, plan_id: str, token: str,
                        status: str, note: str = "") -> None:
        """记录一次 IAP 处理结果。

        token 已存在时忽略（UNIQUE 约束）—— 并发重复回调不会重复发放。
        """
        try:
            self.c.execute(
                "INSERT OR IGNORE INTO iap_purchases"
                " (user_id, plan_id, purchase_token, status, note, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (user_id, plan_id, token, status, note[:200], now()))
            self.c.commit()
        except Exception as e:
            print(f"  ⚠️  IAP 留痕失败: {e}")

    # ---- 举报 ----------------------------------------------------

    # ---- 官网线索（leads）-------------------------------------------
    def create_lead(self, name: str, company: str, contact: str, need: str,
                    source: str = "website", ip: str = "", ua: str = "") -> int:
        """记录一条官网咨询。返回 id。

        独立于用户体系 —— 填表的人不是 App 用户。
        先落库再发邮件：邮件失败也不能丢线索。
        """
        cur = self.c.execute(
            "INSERT INTO leads (name, company, contact, need, source, ip, ua,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name[:100], company[:120], contact[:160], need[:4000],
             source[:20], ip[:64], ua[:300], now()))
        self.c.commit()
        return cur.lastrowid

    def mark_lead_mail(self, lead_id: int, status: str) -> None:
        self.c.execute("UPDATE leads SET mail_status=? WHERE id=?", (status, lead_id))
        self.c.commit()

    def list_leads(self, limit: int = 50) -> list[dict]:
        rows = self.c.execute(
            "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_leads_since(self, ts: float, ip: str) -> int:
        """同一 IP 在时间窗内的提交数 —— 用于简单防刷。"""
        r = self.c.execute(
            "SELECT COUNT(*) c FROM leads WHERE ip=? AND created_at>?",
            (ip, ts)).fetchone()
        return r["c"] if r else 0

    def create_report(self, reporter_id: int, target_type: str, target_id: int,
                      reason: str, detail: str = "", snapshot: str = "") -> dict:
        """提交举报。

        csam 自动升级为 urgent —— 这类工单必须在人工队列里排到最前。
        同一人对同一目标重复举报只记一次（UNIQUE 约束），返回已有记录。
        """
        priority = "urgent" if reason == "csam" else "normal"
        try:
            cur = self.c.execute(
                "INSERT INTO reports (reporter_id, target_type, target_id, reason,"
                " detail, snapshot, priority, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (reporter_id, target_type, target_id, reason,
                 (detail or "")[:1000], (snapshot or "")[:500], priority, now()))
            self.c.commit()
            return {"id": cur.lastrowid, "duplicate": False, "priority": priority}
        except sqlite3.IntegrityError:
            row = self.c.execute(
                "SELECT id, priority FROM reports"
                " WHERE reporter_id=? AND target_type=? AND target_id=?",
                (reporter_id, target_type, target_id)).fetchone()
            return {"id": row["id"] if row else 0, "duplicate": True,
                    "priority": row["priority"] if row else priority}

    def report_queue(self, status: str = "pending", limit: int = 100) -> list[dict]:
        """待处理工单，urgent 优先。"""
        rows = self.c.execute(
            "SELECT * FROM reports WHERE status=?"
            " ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END, id"
            " LIMIT ?", (status, limit)).fetchall()
        return [dict(r) for r in rows]

    def report_counts(self) -> dict:
        """工单统计（各状态计数 + 待处理的紧急数）。"""
        out = {"pending": 0, "urgent": 0, "actioned": 0, "dismissed": 0}
        for r in self.c.execute(
                "SELECT status, COUNT(*) c FROM reports GROUP BY status").fetchall():
            out[r["status"]] = r["c"]
        row = self.c.execute(
            "SELECT COUNT(*) c FROM reports"
            " WHERE status='pending' AND priority='urgent'").fetchone()
        out["urgent"] = row["c"] if row else 0
        return out

    def resolve_report(self, report_id: int, status: str) -> bool:
        """处置工单。status: reviewed | actioned | dismissed"""
        self.c.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
        self.c.commit()
        return self.c.total_changes > 0

    def log_moderation(self, user_id: int, field: str, severity: str,                       reason: str, text: str) -> None:
        """记录一次被拦截的内容安全事件（合规留证）。

        excerpt 截断到 200 字符：留证需要能重现判定依据，
        但不应把违规内容完整落库 —— 那等于自己持有。
        """
        self.c.execute(
            "INSERT INTO moderation_events"
            " (user_id, field, severity, reason, excerpt, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, field, severity, reason, (text or "")[:200], now()))
        self.c.commit()

    def moderation_count(self, user_id: int, severity: str | None = None) -> int:
        """某用户的违规次数（可用于分级处置）。"""
        if severity:
            row = self.c.execute(
                "SELECT COUNT(*) AS n FROM moderation_events"
                " WHERE user_id=? AND severity=?", (user_id, severity)).fetchone()
        else:
            row = self.c.execute(
                "SELECT COUNT(*) AS n FROM moderation_events WHERE user_id=?",
                (user_id,)).fetchone()
        return int(row["n"]) if row else 0

    def add_comment(self, moment_id: int, user_id: int, content: str) -> dict:
        cur = self.c.execute(
            "INSERT INTO moment_comments (moment_id, user_id, content, created_at)"
            " VALUES (?,?,?,?)", (moment_id, user_id, content, now()))
        n = self.c.execute("SELECT COUNT(*) c FROM moment_comments WHERE moment_id=?",
                           (moment_id,)).fetchone()["c"]
        self.c.execute("UPDATE moments SET comment_count=? WHERE id=?", (n, moment_id))
        self.c.commit()
        return {"id": cur.lastrowid, "momentId": moment_id, "content": content}

    def list_comments(self, moment_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY id",
            (moment_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_moment(self, user_id: int, moment_id: int) -> bool:
        m = self.get_moment(moment_id)
        if not m or m["user_id"] != user_id:
            return False
        self.c.execute("DELETE FROM moments WHERE id=?", (moment_id,))
        self.c.execute("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,))
        self.c.execute("DELETE FROM moment_likes WHERE moment_id=?", (moment_id,))
        self.c.commit()
        return True

    # ---- affinity（好感度）------------------------------------------
    AFFINITY_MIN, AFFINITY_MAX = -2000, 2000

    def get_affinity(self, user_id: int, role_id: int) -> int:
        r = self.c.execute(
            "SELECT value FROM role_affinity WHERE user_id=? AND role_id=?",
            (user_id, role_id)).fetchone()
        return r["value"] if r else 0

    def add_affinity(self, user_id: int, role_id: int, delta: int,
                     reason: str = "") -> int:
        """范围夹紧到 [-2000, 2000]（对齐官方说明）。"""
        if delta == 0:
            return self.get_affinity(user_id, role_id)
        cur = self.get_affinity(user_id, role_id)
        new = max(self.AFFINITY_MIN, min(self.AFFINITY_MAX, cur + delta))
        applied = new - cur
        self.c.execute(
            "INSERT INTO role_affinity (user_id, role_id, value, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(user_id, role_id) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (user_id, role_id, new, now()))
        self.c.execute(
            "INSERT INTO affinity_log (user_id, role_id, delta, reason, created_at)"
            " VALUES (?,?,?,?,?)", (user_id, role_id, applied, reason, now()))
        self.c.commit()
        return new

    # ---- hidden chapters -------------------------------------------
    CHAPTER_LIMIT = 42      # 官方：每个角色最多 42 章

    def add_chapter(self, role_id: int, seq: int, ctype: str, threshold: int,
                    title: str, intro: str, content: str) -> int:
        n = self.c.execute("SELECT COUNT(*) c FROM hidden_chapters WHERE role_id=?",
                           (role_id,)).fetchone()["c"]
        if n >= self.CHAPTER_LIMIT:
            raise ValueError(f"chapter limit reached ({self.CHAPTER_LIMIT})")
        cur = self.c.execute(
            "INSERT INTO hidden_chapters (role_id, seq, type, affinity_threshold,"
            " title, intro, content, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (role_id, seq, ctype, threshold, title, intro, content, now()))
        self.c.commit()
        return cur.lastrowid

    def list_chapters(self, user_id: int, role_id: int) -> list[dict]:
        """
        返回章节列表，含解锁状态。
        未解锁时只给 intro（无剧透），对齐官方「Locked chapters still show
         a spoiler-free Intro」的设计。
        """
        aff = self.get_affinity(user_id, role_id)
        rows = self.c.execute(
            "SELECT * FROM hidden_chapters WHERE role_id=? ORDER BY seq, id",
            (role_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            th = d["affinity_threshold"]
            # 正向章节：affinity >= 阈值；负向章节：affinity <= 阈值
            if d["type"] == "negative":
                unlocked = aff <= th
            else:
                unlocked = aff >= th
            rec = self.c.execute(
                "SELECT * FROM chapter_unlocks WHERE user_id=? AND chapter_id=?",
                (user_id, d["id"])).fetchone()
            if unlocked and not rec:
                self.c.execute(
                    "INSERT OR IGNORE INTO chapter_unlocks (user_id, chapter_id,"
                    " unlocked_at) VALUES (?,?,?)", (user_id, d["id"], now()))
                self.c.commit()
                rec = self.c.execute(
                    "SELECT * FROM chapter_unlocks WHERE user_id=? AND chapter_id=?",
                    (user_id, d["id"])).fetchone()
            d["unlocked"] = bool(unlocked)
            d["enteredAt"] = rec["entered_at"] if rec else None
            d["currentAffinity"] = aff
            if not unlocked:
                d["content"] = ""          # 未解锁不泄露正文
            out.append(d)
        return out

    def enter_chapter(self, user_id: int, chapter_id: int) -> dict | None:
        """进入章节，并把发生的剧情同步进记忆（对齐官方说明）。"""
        row = self.c.execute("SELECT * FROM hidden_chapters WHERE id=?",
                             (chapter_id,)).fetchone()
        if not row:
            return None
        self.c.execute(
            "UPDATE chapter_unlocks SET entered_at=? WHERE user_id=? AND chapter_id=?",
            (now(), user_id, chapter_id))
        self.c.commit()
        return dict(row)

    def affinity_leaderboard(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT a.role_id, a.value, r.name AS role_name"
            " FROM role_affinity a JOIN roles r ON r.id=a.role_id"
            " WHERE a.user_id=? ORDER BY a.value DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- scenario（情景）--------------------------------------------
    SCENARIO_CATEGORIES = ["romance", "fantasy", "mystery", "historical",
                           "scifi", "slice-of-life", "horror"]

    def list_scenarios(self, user_id: int, q: str = "", category: str = "",
                       location: str = "", mine: int | None = None,
                       liked: bool = False) -> list[dict]:
        sql = ("SELECT s.*, c.display_name AS creator_name FROM scenarios s"
               " LEFT JOIN creators c ON c.user_id = s.creator_id WHERE s.is_public=1")
        args: list = []
        if mine is not None:
            sql = ("SELECT s.*, c.display_name AS creator_name FROM scenarios s"
                   " LEFT JOIN creators c ON c.user_id = s.creator_id WHERE s.creator_id=?")
            args.append(mine)
        if category:
            sql += " AND s.category=?"; args.append(category)
        if location:
            sql += " AND s.location=?"; args.append(location)
        if q:
            sql += " AND (s.title LIKE ? OR s.description LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY s.id DESC LIMIT 60"
        rows = self.c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            d["liked"] = bool(self.c.execute(
                "SELECT 1 FROM scenario_likes WHERE user_id=? AND scenario_id=?",
                (user_id, d["id"])).fetchone())
            out.append(d)
        if liked:
            out = [d for d in out if d["liked"]]
        return out

    def get_scenario(self, sid: int) -> dict | None:
        r = self.c.execute("SELECT * FROM scenarios WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    def create_scenario(self, creator_id: int, **kw) -> int:
        cur = self.c.execute(
            "INSERT INTO scenarios (creator_id, title, description, location,"
            " category, cover, tags, opener, is_public, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (creator_id, kw.get("title", ""), kw.get("description", ""),
             kw.get("location", ""), kw.get("category", ""), kw.get("cover", ""),
             json.dumps(kw.get("tags") or []), kw.get("opener", ""),
             1 if kw.get("isPublic", True) else 0, now()))
        self.c.commit()
        return cur.lastrowid

    def update_scenario(self, sid: int, creator_id: int, **kw) -> bool:
        s = self.get_scenario(sid)
        if not s or s["creator_id"] != creator_id:
            return False
        fields, args = [], []
        for col in ("title", "description", "location", "category", "cover", "opener"):
            if col in kw:
                fields.append(f"{col}=?")
                args.append(kw[col])
        if "tags" in kw:
            fields.append("tags=?"); args.append(json.dumps(kw["tags"] or []))
        if "isPublic" in kw:
            fields.append("is_public=?"); args.append(1 if kw["isPublic"] else 0)
        if not fields:
            return False
        args += [sid, creator_id]
        self.c.execute(f"UPDATE scenarios SET {','.join(fields)}"
                       " WHERE id=? AND creator_id=?", args)
        self.c.commit()
        return True

    def delete_scenario(self, sid: int, creator_id: int) -> bool:
        s = self.get_scenario(sid)
        if not s or s["creator_id"] != creator_id:
            return False
        self.c.execute("DELETE FROM scenarios WHERE id=?", (sid,))
        self.c.execute("DELETE FROM scenario_likes WHERE scenario_id=?", (sid,))
        self.c.commit()
        return True

    def toggle_scenario_like(self, user_id: int, sid: int, on: bool) -> int:
        if on:
            self.c.execute("INSERT OR IGNORE INTO scenario_likes"
                           " (user_id, scenario_id, created_at) VALUES (?,?,?)",
                           (user_id, sid, now()))
        else:
            self.c.execute("DELETE FROM scenario_likes WHERE user_id=? AND scenario_id=?",
                           (user_id, sid))
        n = self.c.execute("SELECT COUNT(*) c FROM scenario_likes WHERE scenario_id=?",
                           (sid,)).fetchone()["c"]
        self.c.execute("UPDATE scenarios SET like_count=? WHERE id=?", (n, sid))
        self.c.commit()
        return n

    def scenario_locations(self) -> list[str]:
        rows = self.c.execute(
            "SELECT DISTINCT location FROM scenarios WHERE location<>''"
            " ORDER BY location").fetchall()
        return [r["location"] for r in rows]

    def bump_scenario_play(self, sid: int) -> None:
        self.c.execute("UPDATE scenarios SET play_count=play_count+1 WHERE id=?", (sid,))
        self.c.commit()

    # ---- creator / follow -----------------------------------------
    def ensure_creator(self, user_id: int, display_name: str = "") -> dict:
        r = self.c.execute("SELECT * FROM creators WHERE user_id=?",
                           (user_id,)).fetchone()
        if not r:
            self.c.execute(
                "INSERT INTO creators (user_id, display_name, created_at)"
                " VALUES (?,?,?)", (user_id, display_name or f"Creator {user_id}", now()))
            self.c.commit()
            r = self.c.execute("SELECT * FROM creators WHERE user_id=?",
                               (user_id,)).fetchone()
        return dict(r)

    def update_creator(self, user_id: int, display_name: str = "", bio: str = "") -> dict:
        """更新创作者资料。display_name / bio 为空时保留原值。

        注意：调用方不应传用户昵称，否则会把创作者自己设的显示名覆盖掉。
        只传 user_id 时仅刷新统计。
        """
        self.ensure_creator(user_id, display_name)
        if display_name:
            self.c.execute("UPDATE creators SET display_name=? WHERE user_id=?",
                           (display_name, user_id))
        if bio:
            self.c.execute("UPDATE creators SET bio=? WHERE user_id=?", (bio, user_id))
        self.c.commit()
        # 同步统计角色数
        n = self.c.execute("SELECT COUNT(*) c FROM roles WHERE creator=?",
                           (str(user_id),)).fetchone()["c"]
        self.c.execute("UPDATE creators SET role_count=? WHERE user_id=?", (n, user_id))
        self.c.commit()
        return dict(self.c.execute("SELECT * FROM creators WHERE user_id=?",
                                   (user_id,)).fetchone())

    def follow(self, follower: int, target: int, auto_accept: bool = True) -> dict:
        if follower == target:
            return {"ok": False, "reason": "self"}
        if self.is_blocked(target, follower) or self.is_blocked(follower, target):
            return {"ok": False, "reason": "blocked"}
        status = "accepted" if auto_accept else "pending"
        self.c.execute(
            "INSERT OR REPLACE INTO follows (follower_id, following_id, status, created_at)"
            " VALUES (?,?,?,?)", (follower, target, status, now()))
        self.c.commit()
        return {"ok": True, "status": status}

    def unfollow(self, follower: int, target: int) -> None:
        self.c.execute("DELETE FROM follows WHERE follower_id=? AND following_id=?",
                       (follower, target))
        self.c.commit()

    def remove_follower(self, user: int, follower: int) -> None:
        self.c.execute("DELETE FROM follows WHERE follower_id=? AND following_id=?",
                       (follower, user))
        self.c.commit()

    def accept_follower(self, user: int, follower: int) -> bool:
        cur = self.c.execute(
            "UPDATE follows SET status='accepted'"
            " WHERE follower_id=? AND following_id=? AND status='pending'",
            (follower, user))
        self.c.commit()
        return cur.rowcount > 0

    def following_list(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT f.*, c.display_name, c.bio FROM follows f"
            " LEFT JOIN creators c ON c.user_id = f.following_id"
            " WHERE f.follower_id=? ORDER BY f.created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def follower_list(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT f.*, c.display_name, c.bio FROM follows f"
            " LEFT JOIN creators c ON c.user_id = f.follower_id"
            " WHERE f.following_id=? AND f.status='accepted'"
            " ORDER BY f.created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def pending_requests(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT f.*, c.display_name FROM follows f"
            " LEFT JOIN creators c ON c.user_id = f.follower_id"
            " WHERE f.following_id=? AND f.status='pending'"
            " ORDER BY f.created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def has_unread_requests(self, user_id: int) -> bool:
        last = self.c.execute("SELECT read_at FROM follow_reads WHERE user_id=?",
                              (user_id,)).fetchone()
        since = last["read_at"] if last else 0
        r = self.c.execute(
            "SELECT 1 FROM follows WHERE following_id=? AND status='pending'"
            " AND created_at > ? LIMIT 1", (user_id, since)).fetchone()
        return bool(r)

    def mark_requests_read(self, user_id: int) -> None:
        self.c.execute("INSERT OR REPLACE INTO follow_reads (user_id, read_at)"
                       " VALUES (?,?)", (user_id, now()))
        self.c.commit()

    def block_creator(self, user_id: int, target: int, on: bool) -> None:
        if on:
            self.c.execute("INSERT OR IGNORE INTO creator_blocks"
                           " (user_id, blocked_id, created_at) VALUES (?,?,?)",
                           (user_id, target, now()))
            self.c.execute("DELETE FROM follows WHERE"
                           " (follower_id=? AND following_id=?) OR"
                           " (follower_id=? AND following_id=?)",
                           (user_id, target, target, user_id))
        else:
            self.c.execute("DELETE FROM creator_blocks WHERE user_id=? AND blocked_id=?",
                           (user_id, target))
        self.c.commit()

    def is_blocked(self, user_id: int, target: int) -> bool:
        return bool(self.c.execute(
            "SELECT 1 FROM creator_blocks WHERE user_id=? AND blocked_id=?",
            (user_id, target)).fetchone())

    def block_list(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT b.*, c.display_name FROM creator_blocks b"
            " LEFT JOIN creators c ON c.user_id = b.blocked_id"
            " WHERE b.user_id=? ORDER BY b.created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def roles_overview(self, creator_id: int) -> dict:
        n = self.c.execute("SELECT COUNT(*) c FROM roles WHERE creator=?",
                           (str(creator_id),)).fetchone()["c"]
        f = self.c.execute("SELECT COUNT(*) c FROM follows"
                           " WHERE following_id=? AND status='accepted'",
                           (creator_id,)).fetchone()["c"]
        return {"roleCount": n, "followerCount": f,
                "totalLikes": 0}

    # ---- album / ugc -----------------------------------------------
    def add_album_item(self, user_id: int, file_path: str, *, role_id=None,
                       source: str = "ugc", prompt: str = "", style: str = "",
                       cost: int = 0, kind: str = "image") -> int:
        cur = self.c.execute(
            "INSERT INTO album_items (user_id, role_id, kind, source, file_path,"
            " prompt, style, heart_cost, is_unread, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,1,?)",
            (user_id, role_id, kind, source, file_path, prompt, style, cost, now()))
        self.c.commit()
        return cur.lastrowid

    def list_album(self, user_id: int, source: str | None = None,
                   role_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM album_items WHERE user_id=?"
        args: list = [user_id]
        if source:
            sql += " AND source=?"; args.append(source)
        if role_id is not None:
            sql += " AND role_id=?"; args.append(role_id)
        sql += " ORDER BY id DESC LIMIT 100"
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def album_has_unread(self, user_id: int) -> bool:
        return bool(self.c.execute(
            "SELECT 1 FROM album_items WHERE user_id=? AND is_unread=1 LIMIT 1",
            (user_id,)).fetchone())

    def album_mark_read(self, user_id: int) -> None:
        self.c.execute("UPDATE album_items SET is_unread=0 WHERE user_id=?", (user_id,))
        self.c.commit()

    # ---- quest / achievement ---------------------------------------
    @staticmethod
    def _quest_period(qtype: str) -> str:
        """user_quests.day 的取值。

        daily → 当天日期（每天重置）
        goal  → 固定字符串（**永不重置**）

        原先两者都用当天日期，导致「长期任务」每天凭空重置：
        `goal_create_role` 是零成本动作、奖励 30 hearts，
        于是用户可以每天白领 30 —— 等于一个永久免费 faucet，
        也让人误以为 hearts 死锁已被解决（其实只是靠这个漏洞撑着）。
        """
        if qtype == "goal":
            return "goal"          # 固定 key：一个账号一辈子只能领一次
        return time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))

    def list_quests(self, user_id: int, qtype: str = "daily") -> list[dict]:
        today = self._quest_period(qtype)
        rows = self.c.execute(
            "SELECT * FROM quests WHERE type=? ORDER BY id", (qtype,)).fetchall()
        out = []
        for q in rows:
            uq = self.c.execute(
                "SELECT * FROM user_quests WHERE user_id=? AND quest_id=? AND day=?",
                (user_id, q["id"], today)).fetchone()
            d = dict(q)
            d["progress"] = uq["progress"] if uq else 0
            d["claimed"] = bool(uq["claimed"]) if uq else False
            d["completed"] = d["progress"] >= q["target"]
            d["claimable"] = d["completed"] and not d["claimed"]
            out.append(d)
        return out

    def has_checked_in(self, user_id: int, day: str) -> bool:
        """今天是否已签到。用流水表判断，不额外建表。"""
        row = self.c.execute(
            "SELECT 1 FROM heart_ledger WHERE user_id=? AND reason=? LIMIT 1",
            (user_id, "checkin:" + day)).fetchone()
        return bool(row)

    def bump_quest(self, user_id: int, event_key: str) -> None:
        """行为发生后推进对应的日常/目标任务进度。"""
        rows = self.c.execute(
            "SELECT id, type FROM quests WHERE event_key=?", (event_key,)).fetchall()
        for r in rows:
            # 每个任务按自己的 type 决定周期 key（goal 永不重置）
            period = self._quest_period(r["type"])
            self.c.execute(
                "INSERT INTO user_quests (user_id, quest_id, progress, claimed, day,"
                " updated_at) VALUES (?,?,1,0,?,?)"
                " ON CONFLICT(user_id, quest_id, day) DO UPDATE SET"
                " progress=progress+1, updated_at=excluded.updated_at",
                (user_id, r["id"], period, now()))
        self.c.commit()

    def claim_quest(self, user_id: int, quest_id: int) -> dict:
        """领取任务奖励。

        并发安全靠 UPDATE ... AND claimed=0 的 rowcount，不是一个先把
        claimed 读出来再判断的 if 语句 —— 后者在两步之间有窗口，
        两个并发请求都能看到 claimed=0。（实测 12 并发只复现 1 次，
        窗口比签到小，但结构是同一个缺陷。）

        顺序也很关键：先改状态标记，拿到 rowcount==1 才认为本次领取有效，
        余额由调用方在拿到 ok=True 之后再加。绝不能反过来。
        """
        q = self.c.execute("SELECT * FROM quests WHERE id=?", (quest_id,)).fetchone()
        if not q:
            return {"ok": False, "reason": "not found"}
        today = self._quest_period(q["type"])
        uq = self.c.execute(
            "SELECT * FROM user_quests WHERE user_id=? AND quest_id=? AND day=?",
            (user_id, quest_id, today)).fetchone()
        if not uq or uq["progress"] < q["target"]:
            return {"ok": False, "reason": "not completed"}
        # 原子占位：只有仍为未领取的那一次能改成功
        cur = self.c.execute(
            "UPDATE user_quests SET claimed=1, updated_at=?"
            " WHERE user_id=? AND quest_id=? AND day=? AND claimed=0",
            (now(), user_id, quest_id, today))
        won = cur.rowcount == 1
        self.c.commit()
        if not won:
            return {"ok": False, "reason": "already claimed"}
        return {"ok": True, "hearts": q["reward_hearts"]}

    def list_achievements(self, user_id: int) -> list[dict]:
        rows = self.c.execute("SELECT * FROM achievements ORDER BY id").fetchall()
        out = []
        for a in rows:
            ua = self.c.execute(
                "SELECT unlocked_at FROM user_achievements"
                " WHERE user_id=? AND achievement_id=?", (user_id, a["id"])).fetchone()
            d = dict(a)
            d["unlocked"] = bool(ua)
            d["unlockedAt"] = ua["unlocked_at"] if ua else None
            out.append(d)
        return out

    def unlock_achievement(self, user_id: int, code: str) -> bool:
        a = self.c.execute("SELECT id FROM achievements WHERE code=?", (code,)).fetchone()
        if not a:
            return False
        cur = self.c.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, achievement_id, unlocked_at)"
            " VALUES (?,?,?)", (user_id, a["id"], now()))
        self.c.commit()
        return cur.rowcount > 0

    # ---- notification ----------------------------------------------
    def add_notification(self, user_id: int, kind: str, title: str, body: str = "") -> int:
        cur = self.c.execute(
            "INSERT INTO notifications (user_id, kind, title, body, created_at)"
            " VALUES (?,?,?,?,?)", (user_id, kind, title, body, now()))
        self.c.commit()
        return cur.lastrowid

    def list_notifications(self, user_id: int, limit: int = 50) -> list[dict]:
        rows = self.c.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def has_unread_notifications(self, user_id: int) -> bool:
        return bool(self.c.execute(
            "SELECT 1 FROM notifications WHERE user_id=? AND is_read=0 LIMIT 1",
            (user_id,)).fetchone())

    def mark_notifications_read(self, user_id: int, kind: str | None = None) -> int:
        if kind:
            cur = self.c.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND kind=?",
                (user_id, kind))
        else:
            cur = self.c.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=?", (user_id,))
        self.c.commit()
        return cur.rowcount

    # ---- sticker ---------------------------------------------------
    def sticker_categories(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT DISTINCT category FROM stickers ORDER BY category").fetchall()
        out = []
        for r in rows:
            cat = r["category"]
            items = self.c.execute(
                "SELECT * FROM stickers WHERE category=? ORDER BY id", (cat,)).fetchall()
            owned = self.c.execute(
                "SELECT COUNT(*) c FROM user_stickers us JOIN stickers s"
                " ON s.id=us.sticker_id WHERE us.user_id=? AND s.category=?",
                (user_id, cat)).fetchone()["c"]
            out.append({"category": cat, "total": len(items),
                        "owned": owned, "items": [dict(i) for i in items]})
        return out

    def grant_sticker(self, user_id: int, name: str) -> bool:
        s = self.c.execute("SELECT id FROM stickers WHERE name=?", (name,)).fetchone()
        if not s:
            return False
        cur = self.c.execute(
            "INSERT OR IGNORE INTO user_stickers (user_id, sticker_id, count, obtained_at)"
            " VALUES (?,?,1,?)", (user_id, s["id"], now()))
        self.c.commit()
        return cur.rowcount > 0

    def user_stickers(self, user_id: int) -> list[dict]:
        rows = self.c.execute(
            "SELECT s.*, us.count, us.obtained_at FROM user_stickers us"
            " JOIN stickers s ON s.id=us.sticker_id WHERE us.user_id=?"
            " ORDER BY us.obtained_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, user_id: int) -> dict:
        """用于成就判定与 dashboard"""
        g = lambda sql, *a: self.c.execute(sql, a).fetchone()[0]
        return {
            "messages": g("SELECT COUNT(*) FROM messages m JOIN chatrooms c"
                           " ON c.id=m.chatroom_id WHERE c.user_id=? AND m.speaker='user'", user_id),
            "chatrooms": g("SELECT COUNT(*) FROM chatrooms WHERE user_id=?", user_id),
            "moments": g("SELECT COUNT(*) FROM moments WHERE user_id=?", user_id),
            "scenarios": g("SELECT COUNT(*) FROM scenarios WHERE creator_id=?", user_id),
            "roles": g("SELECT COUNT(*) FROM roles WHERE creator=?", str(user_id)),
            "images": g("SELECT COUNT(*) FROM album_items WHERE user_id=?", user_id),
            "chapters": g("SELECT COUNT(*) FROM chapter_unlocks WHERE user_id=?", user_id),
        }
