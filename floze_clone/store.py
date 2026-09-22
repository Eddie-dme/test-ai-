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

import json
import sqlite3
import time
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
    created_at      REAL NOT NULL
);

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

CREATE TABLE IF NOT EXISTS heart_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    delta           INTEGER NOT NULL,
    reason          TEXT NOT NULL,        -- chat/ugc/moment/ad_reward/purchase
    created_at      REAL NOT NULL
);

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
"""


def now() -> float:
    return time.time()


def connect(init_schema: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if init_schema:
        conn.executescript(SCHEMA)          # 幂等
    return conn


# ------------------------------------------------------------------ 响应封装

def ok(data: Any = None) -> dict:
    """对齐 Vesperine 响应约定：{flag, msg, data}，flag=0 为成功。"""
    return {"flag": 0, "msg": "", "data": data}


def err(msg: str, flag: int = 1) -> dict:
    return {"flag": flag, "msg": msg, "data": None}


# ------------------------------------------------------------------ 业务方法

class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # ---- user -------------------------------------------------------
    def ensure_user(self) -> dict:
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

    def add_hearts(self, user_id: int, delta: int, reason: str) -> int:
        self.c.execute("UPDATE users SET hearts = MAX(0, hearts + ?) WHERE id=?",
                       (delta, user_id))
        self.c.execute(
            "INSERT INTO heart_ledger (user_id, delta, reason, created_at) VALUES (?,?,?,?)",
            (user_id, delta, reason, now()))
        self.c.commit()
        r = self.c.execute("SELECT hearts FROM users WHERE id=?", (user_id,)).fetchone()
        return r["hearts"]

    def heart_info(self, user_id: int) -> dict:
        r = self.c.execute(
            "SELECT hearts, unlimited_until FROM users WHERE id=?", (user_id,)).fetchone()
        return {"amount": r["hearts"], "unlimitedHeartsExpiredAt": r["unlimited_until"]}

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

    def log_moderation(self, user_id: int, field: str, severity: str,
                       reason: str, text: str) -> None:
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
    def list_quests(self, user_id: int, qtype: str = "daily") -> list[dict]:
        today = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))
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

    def bump_quest(self, user_id: int, event_key: str) -> None:
        """行为发生后推进对应的日常/目标任务进度。"""
        today = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))
        rows = self.c.execute(
            "SELECT id FROM quests WHERE event_key=?", (event_key,)).fetchall()
        for r in rows:
            self.c.execute(
                "INSERT INTO user_quests (user_id, quest_id, progress, claimed, day,"
                " updated_at) VALUES (?,?,1,0,?,?)"
                " ON CONFLICT(user_id, quest_id, day) DO UPDATE SET"
                " progress=progress+1, updated_at=excluded.updated_at",
                (user_id, r["id"], today, now()))
        self.c.commit()

    def claim_quest(self, user_id: int, quest_id: int) -> dict:
        today = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))
        q = self.c.execute("SELECT * FROM quests WHERE id=?", (quest_id,)).fetchone()
        if not q:
            return {"ok": False, "reason": "not found"}
        uq = self.c.execute(
            "SELECT * FROM user_quests WHERE user_id=? AND quest_id=? AND day=?",
            (user_id, quest_id, today)).fetchone()
        if not uq or uq["progress"] < q["target"]:
            return {"ok": False, "reason": "not completed"}
        if uq["claimed"]:
            return {"ok": False, "reason": "already claimed"}
        self.c.execute(
            "UPDATE user_quests SET claimed=1, updated_at=?"
            " WHERE user_id=? AND quest_id=? AND day=?",
            (now(), user_id, quest_id, today))
        self.c.commit()
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
