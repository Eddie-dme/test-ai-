"""
Vesperine 复刻 —— 主服务（零依赖，仅用 Python 标准库）

端点命名对齐 floze_reverse/api_contract.txt 中逆向出的真实契约：
    /user/settings  /role/list  /chatMode  /chatroom/*  /message/*
    /heart/*  /ad/*

响应统一为 {flag, msg, data}，flag=0 为成功（Vesperine 全站约定）。

运行：
    python3 server.py            # 默认 8080，mock 模式（无需 API key）
    MINIMAX_API_KEY=... python3 server.py    # 接入真实模型
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import llm
import affinity
import moderation
import iap_verify
import mailer
import imagegen
import tts
import voices as voice_map
from seed_data import (ROLES, CHAPTERS, QUESTS, ACHIEVEMENTS, STICKERS,
                       ACHIEVEMENT_STICKERS,
                       SCENARIOS, MOMENTS, ALBUMS)
from store import Store, connect, ok, err, now

STATIC = Path(__file__).resolve().parent / "static"
GENERATED_DIR = STATIC / "generated"      # 生成的图片落盘目录（静态可访问）

# 访问密钥 —— 所有 /api/* 请求必须带 X-Access-Key 匹配此值。
# 未配置时跳过校验（方便本地开发）；生产环境必须设置。
# 静态资源（前端页面、立绘）不受保护，否则前端自身都加载不了。
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()

# 限流 —— 每个来源 IP 每分钟允许的 /api/* 请求数。
# 目的不是替代认证，而是防止未认证请求被无限刷（认证挡住的是数据，
# 限流挡住的是资源）。设为 0 可关闭。
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))

# 请求体上限 —— 单个 POST body 的字节数上限，超过直接 413 拒绝。
#
# 为什么需要：服务挂在一层会缓冲请求体的反向代理后面，超限时网关
# 在第一跳就回 413，客户端看到的是「Failed to buffer the request body」，
# 既分不清是自己发太大还是服务出错，也白白占了连接和带宽。
# 在应用层先挡一道，客户端能拿到明确的 error msg，服务也不会
# 为一个注定失败的请求把 body 读进内存。
#
# 1 MiB 的依据：最大的合法请求是长消息（message/send 的 text），
# 正常几条 KB；图片上传不走 POST，而是客户端本地保存。
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1024 * 1024)))

_rate_hits: dict = {}
_rate_lock = threading.Lock()


def rate_allow(ip: str) -> bool:
    """朴素滑动窗口。单进程内存实现，够用且无外部依赖。

    注意：进程重启计数清零；多实例部署时各自独立计数。
    """
    if RATE_LIMIT_PER_MIN <= 0:
        return True
    now = time.time()
    cutoff = now - 60.0
    with _rate_lock:
        hits = _rate_hits.setdefault(ip, [])
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= RATE_LIMIT_PER_MIN:
            return False
        hits.append(now)
        # 顺手清理长期不活跃的条目，避免字典无界增长
        if len(_rate_hits) > 4096:
            for k in [k for k, v in _rate_hits.items() if not v or v[-1] < cutoff]:
                _rate_hits.pop(k, None)
        return True
SPEECH_DIR = STATIC / "speech"            # 语音落盘目录（静态可访问）

import os as _os
# 测试环境：设 HOST=0.0.0.0 即可让局域网/容器外部访问
HOST = _os.environ.get("HOST", "127.0.0.1")
PORT = int(_os.environ.get("PORT", "8080"))

# ---------------------------------------------------------------- 广告/支付配置
# 未拿到 AdMob 账号时，用 Google 官方公开的测试单元 ID，可直接跑通集成。
# 这些 ID 不需要任何账号，上线前换成自己的即可。
AD_CONFIG = {
    "mode": _os.environ.get("AD_MODE", "test"),        # test | live
    "test": {
        "publisher": "ca-app-pub-3940256099942544",
        "rewarded": "ca-app-pub-3940256099942544/5224354917",
        "interstitial": "ca-app-pub-3940256099942544/1033173712",
        "banner": "ca-app-pub-3940256099942544/6300978111",
    },
    "live": {   # 拿到 AdMob 账号后填这里，或用环境变量注入
        "publisher": _os.environ.get("ADMOB_PUBLISHER", ""),
        "rewarded": _os.environ.get("ADMOB_REWARDED", ""),
        "interstitial": _os.environ.get("ADMOB_INTERSTITIAL", ""),
        "banner": _os.environ.get("ADMOB_BANNER", ""),
    },
}

# 支付模式：stub（本地模拟）| live（Google Play Billing）
PAYMENT_MODE = _os.environ.get("PAYMENT_MODE", "stub")

# 广告冷却（秒）—— 对齐实测到的默认冷却 90s
AD_COOLDOWN = 90
# 广告奖励开关。v1.0 关闭：客户端从未接入广告 SDK，前端若保留「看广告换
# hearts」入口就是虚假功能（Play 政策明令禁止，审核会拒）。
# v1.1 接入 AdMob 后，设 ADS_ENABLED=1 并完成原生 SDK 对接再打开。
ADS_ENABLED = os.environ.get("ADS_ENABLED", "0").strip() == "1"

# 免费额度（对齐 Vesperine 的 freeQuotaType）
FREE_QUOTA = {"suggestReply": 5, "roleUgc": 3, "momentPost": 3, "momentComment": 5}

# 动态流消耗配置（对齐 Vesperine 的 heart store 实测值）
MOMENT_COST = {"post": 3, "comment": 2}


def bootstrap(store: Store) -> None:
    """首次启动时灌入种子角色、章节、任务、成就、贴纸与配额。"""
    if not store.list_roles():
        for r in ROLES:
            store.c.execute(
                "INSERT INTO roles (name, description, personality, background,"
                " first_message, status_bar, tags, creator, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (r["name"], r["description"], r["personality"], r["background"],
                 r["first_message"], r["status_bar"], json.dumps(r["tags"]),
                 "system", now()))
        store.c.commit()

    # 灌入隐藏章节（按角色名关联）
    for role_name, chapters in CHAPTERS.items():
        row = store.c.execute("SELECT id FROM roles WHERE name=?",
                              (role_name,)).fetchone()
        if not row:
            continue
        rid = row["id"]
        exists = store.c.execute("SELECT COUNT(*) c FROM hidden_chapters WHERE role_id=?",
                                 (rid,)).fetchone()["c"]
        if exists:
            continue
        for ch in chapters:
            store.add_chapter(rid, ch["seq"], ch["type"], ch["affinityThreshold"],
                              ch["title"], ch["intro"], ch["content"])

    # 任务 / 成就 / 贴纸（幂等：靠 code / name 唯一约束）
    # 任务用 upsert 而不是 INSERT OR IGNORE：奖励数值是产品参数，
    # 改了 seed 就必须让**存量库**也跟着生效，否则老用户跑的还是旧经济。
    for q in QUESTS:
        store.c.execute(
            "INSERT INTO quests (type, code, title, description, target,"
            " reward_hearts, event_key) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET"
            " title=excluded.title, description=excluded.description,"
            " target=excluded.target, reward_hearts=excluded.reward_hearts,"
            " event_key=excluded.event_key",
            (q["type"], q["code"], q["title"], q["description"],
             q["target"], q["reward_hearts"], q["event_key"]))
    for a in ACHIEVEMENTS:
        store.c.execute(
            "INSERT OR IGNORE INTO achievements (code, title, description, icon,"
            " threshold, metric) VALUES (?,?,?,?,?,?)",
            (a["code"], a["title"], a["description"], a["icon"],
             a["threshold"], a["metric"]))
    for s in STICKERS:
        exists = store.c.execute("SELECT 1 FROM stickers WHERE name=?",
                                 (s["name"],)).fetchone()
        if not exists:
            store.c.execute(
                "INSERT INTO stickers (category, name, emoji) VALUES (?,?,?)",
                (s["category"], s["name"], s["emoji"]))
    store.c.commit()

    u = store.ensure_user()
    store.ensure_ad_quota(u["id"])
    for t, lim in FREE_QUOTA.items():
        store.c.execute(
            "INSERT OR IGNORE INTO free_quota (user_id, type, count, limit_)"
            " VALUES (?,?,0,?)", (u["id"], t, lim))
    store.c.commit()

    # ── 官方内容：让新用户打开时不是三个空白页 ──
    # 动态与相册挂在当前用户下（数据只有一份），
    # 但动态带 role_id，前端会显示为「角色发的」；
    # 情景需要 creators 记录才能显示创作者名。
    store.c.execute(
        "INSERT OR IGNORE INTO creators (user_id, display_name, bio, created_at)"
        " VALUES (?,?,?,?)",
        (u["id"], "Vesperine",
         "Official scenarios and characters from the Vesperine team.", now()))
    store.c.commit()

    _name_to_rid = {r["name"]: r["id"] for r in store.list_roles()}

    # 情景（幂等：按标题去重）
    if store.c.execute("SELECT COUNT(*) c FROM scenarios").fetchone()["c"] == 0:
        for s in SCENARIOS:
            store.c.execute(
                "INSERT INTO scenarios (creator_id, title, description, location,"
                " category, cover, tags, opener, is_public, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,?)",
                (u["id"], s["title"], s["description"], s["location"],
                 s["category"], s["cover"], json.dumps(s["tags"]),
                 s["opener"], now()))
        store.c.commit()

    # 官方动态（幂等：按内容去重）
    if store.c.execute("SELECT COUNT(*) c FROM moments").fetchone()["c"] == 0:
        for m in MOMENTS:
            rid = _name_to_rid.get(m["role_name"])
            store.c.execute(
                "INSERT INTO moments (user_id, content, role_id, tags,"
                " like_count, comment_count, created_at)"
                " VALUES (?,?,?,?,?,0,?)",
                (u["id"], m["content"], rid, json.dumps(m["tags"]),
                 m["like_count"], now()))
        store.c.commit()

    # 角色相册（幂等：按 file_path 去重）
    if store.c.execute("SELECT COUNT(*) c FROM album_items").fetchone()["c"] == 0:
        for a in ALBUMS:
            store.c.execute(
                "INSERT INTO album_items (user_id, role_id, kind, source,"
                " file_path, prompt, style, created_at)"
                " VALUES (?,?,'image','album',?,?,?,?)",
                (u["id"], _name_to_rid.get(a["role_name"]),
                 a["file_path"], a["prompt"], a["style"], now()))
        store.c.commit()


class API:
    """业务层：把 HTTP 请求映射到 Store + LLM。"""

    def __init__(self, store: Store, client_ip: str = "", user_agent: str = ""):
        self.s = store
        # 请求上下文放在这里而不是 API 里直接读 headers ——
        # API 是业务层，拿不到 HTTP handler 的 self.headers。
        # 之前把 _client_ip 写在 API 里读 self.headers，结果 IP 一直是空。
        self.client_ip = client_ip
        self.user_agent = user_agent

    # ---- user / persona
    def user_settings(self) -> dict:
        u = self.s.ensure_user()
        return ok({"user": {"id": u["id"], "nickname": u["nickname"],
                            "avatar": u["avatar"]},
                   "persona": {"name": u["persona_name"], "desc": u["persona_desc"]},
                   "heartInfo": self.s.heart_info(u["id"])})

    def user_settings_update(self, body: dict) -> dict:
        """更新昵称与 persona（用户自称 + 人设描述）。"""
        u = self.s.ensure_user()
        nickname = (body.get("nickname") or "").strip()[:40]
        if not nickname:
            return err("NICKNAME_REQUIRED")
        pname = (body.get("personaName") or "").strip()[:40]
        pdesc = (body.get("personaDesc") or "").strip()[:500]
        upd = self.s.update_user_profile(
            u["id"], nickname=nickname,
            persona_name=pname, persona_desc=pdesc)
        return ok({
            "user": {"id": upd["id"], "nickname": upd["nickname"],
                     "avatar": upd.get("avatar", "")},
            "persona": {"name": upd.get("persona_name", ""),
                        "desc": upd.get("persona_desc", "")},
        })

    # ---- role
    def bootstrap(self) -> dict:
        """首屏所需的全部数据，一次返回。

        跨境 RTT 实测 380ms，而首屏原本要打 6 个请求（auth/me、
        role/list、chatMode、chatroom/list、heart/list、ad/times）。
        即便前端并行发出，每个请求仍要各自走一遍网络；合并成一个
        把首屏等待从约 2 秒压到 400ms 量级。

        服务端这边是串行取数 —— 它到数据库是本地的，多查几次的
        代价远低于让客户端多跑一次跨国往返。
        """
        u = self.s.ensure_user()
        return ok({
            "user": {
                "id": u["id"], "nickname": u.get("nickname", ""),
                "avatar": u.get("avatar", ""),
                "personaName": u.get("persona_name", ""),
                "personaDesc": u.get("persona_desc", ""),
                "hearts": u.get("hearts", 0),
            },
            "roles": self.role_list().get("data") or [],
            "modes": self.chat_modes().get("data") or [],
            "rooms": self.chatroom_list().get("data") or [],
            # heart_list 返回的是 {heartInfo, history}，这里只取前者，
            # 免得前端要写成 d.heartInfo.heartInfo.amount
            "heartInfo": (self.heart_list().get("data") or {}).get("heartInfo"),
            "adTimes": self.ad_times().get("data"),
        })

    def role_list(self) -> dict:
        return ok(self.s.list_roles())

    def role_detail(self, role_id: int) -> dict:
        r = self.s.get_role(role_id)
        return ok(r) if r else err("role not found")

    # ---- chatMode（对齐 /chatMode，返回档位列表）
    def chat_modes(self) -> dict:
        items = []
        for mid, cfg in llm.CHAT_MODES.items():
            # 不返回 model —— 它是实现细节，用户不需要知道我们后端接的哪家模型。
            # 返回 label/description 让档位差异可感知：只给 "classic · 2💖" 时
            # 用户无法判断多花 4 个 hearts 买到什么。
            items.append({"id": mid,
                          "label": cfg.get("label", mid),
                          "description": cfg.get("description", ""),
                          "temperature": cfg["temperature"],
                          "maxTokens": cfg["max_tokens"],
                          "heartCost": cfg["heart_cost"],
                          "style": cfg["style"]})
        return ok(items)

    # ---- chatroom
    def chatroom_list(self) -> dict:
        u = self.s.ensure_user()
        return ok(self.s.list_chatrooms(u["id"]))

    def chatroom_create(self, body: dict) -> dict:
        """开启/继续与某个角色的对话。

        **同一角色只有一个会话**。用户点「开始对话」的意思是「继续聊」，
        不是「再开一个聊天窗口」—— 旧版本每次点击都 INSERT 一条，
        结果同一角色会出现多条会话和重复的开场白。

        如果复用上了已有会话但档位不同，把档位写回数据库：
        prepare_send 读的是 chatrooms.chat_mode，只改前端显示会让
        「显示 5💖 实扣 2💖」。
        """
        u = self.s.ensure_user()
        role_id = int(body.get("roleId", 0))
        if not self.s.get_role(role_id):
            return err("role not found")
        mode = body.get("chatMode", "classic")
        if mode not in llm.CHAT_MODES:
            mode = "classic"

        existing = self.s.find_chatroom(u["id"], role_id)
        if existing:
            if existing["chat_mode"] != mode:
                self.s.set_chatroom_mode(existing["id"], mode)
            return ok({"id": existing["id"], "roleId": role_id,
                       "chatMode": mode, "model": existing.get("model", "standard"),
                       "resumed": True})
        return ok(self.s.create_chatroom(u["id"], role_id, mode,
                                        body.get("model", "standard")))

    def chatroom_mode(self, body: dict) -> dict:
        """切换已有会话的档位。

        必须在服务端落库：计费读的是 chatrooms.chat_mode，
        只改前端显示就是「UI 显示 5💖、实际扣 2💖」。

        校验会话归属 —— 没有这一条，知道 id 的人就能改别人的会话档位。
        """
        u = self.s.ensure_user()
        try:
            cid = int(body.get("chatroomId", 0))
        except (TypeError, ValueError):
            return err("chatroom not found")
        mode = (body.get("chatMode") or "").strip()
        if mode not in llm.CHAT_MODES:
            return err("invalid chatMode")
        row = self.s.c.execute(
            "SELECT user_id FROM chatrooms WHERE id=?", (cid,)).fetchone()
        if not row or row["user_id"] != u["id"]:
            return err("chatroom not found")
        self.s.set_chatroom_mode(cid, mode)
        return ok({"chatroomId": cid, "chatMode": mode,
                   "heartCost": llm.heart_cost(mode)})

    def message_list(self, chatroom_id: int) -> dict:
        return ok(self.s.list_messages(chatroom_id))

    # ---- heart
    def heart_list(self) -> dict:
        u = self.s.ensure_user()
        rows = self.s.c.execute(
            "SELECT * FROM free_quota WHERE user_id=?", (u["id"],)).fetchall()
        free = {r["type"]: {"count": r["count"], "limit": r["limit_"]} for r in rows}
        return ok({"heartInfo": self.s.heart_info(u["id"]), "freeQuota": free})

    def heart_history(self) -> dict:
        u = self.s.ensure_user()
        rows = self.s.c.execute(
            "SELECT * FROM heart_ledger WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (u["id"],)).fetchall()
        return ok([dict(r) for r in rows])

    # ---- ad（对齐 /ad/times 与 /ad/reward）
    def ad_times(self) -> dict:
        u = self.s.ensure_user()
        self.s.ensure_ad_quota(u["id"])
        quota = {}
        for row in self.s.ad_state(u["id"]):
            quota[row["type"]] = {
                "count": row["count"], "limit": row["limit_"],
                "unredeemedChatroomIds": json.loads(row["unredeemed"] or "[]"),
                "nextAvailableTime": row["next_available"]}
        return ok(quota)

    def ad_reward(self, body: dict) -> dict:
        """广告奖励：CHAT → 该会话下一条消息免费；DAILY_HEART → 直接给 hearts。

        v1.0 默认关闭（ADS_ENABLED=0）：没有接入广告 SDK 就发奖励，等于让
        任何人白拿 hearts。前端入口已同步移除，这里再兜一层，防止直接调 API。
        """
        if not ADS_ENABLED:
            return err("ADS_DISABLED")
        u = self.s.ensure_user()
        ad_type = body.get("type", "DAILY_HEART")
        chatroom_id = body.get("chatroomId")

        next_avail = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + AD_COOLDOWN))
        r = self.s.bump_ad(u["id"], ad_type, next_avail)
        if r["flag"] != 0:
            return r

        if ad_type == "DAILY_HEART":
            amount = 5
            balance = self.s.add_hearts(u["id"], amount, "ad_reward")
            return ok({"type": ad_type, "hearts": amount, "balance": balance,
                       "nextAvailableTime": next_avail})
        if ad_type == "CHAT" and chatroom_id:
            row = self.s.c.execute(
                "SELECT unredeemed FROM ad_quota WHERE user_id=? AND type='CHAT'",
                (u["id"],)).fetchone()
            ids = json.loads(row["unredeemed"] or "[]")
            if int(chatroom_id) not in ids:
                ids.append(int(chatroom_id))
            self.s.c.execute(
                "UPDATE ad_quota SET unredeemed=? WHERE user_id=? AND type='CHAT'",
                (json.dumps(ids), u["id"]))
            self.s.c.commit()
            return ok({"type": ad_type, "unredeemedChatroomIds": ids,
                       "nextAvailableTime": next_avail})
        return ok({"type": ad_type, "nextAvailableTime": next_avail})

    # ---- memory
    def memory_list(self, chatroom_id: int) -> dict:
        rows = self.s.c.execute(
            "SELECT * FROM memories WHERE chatroom_id=? ORDER BY id",
            (chatroom_id,)).fetchall()
        return ok([dict(r) for r in rows])

    def summarize(self, chatroom_id: int) -> dict:
        msgs = self.s.list_messages(chatroom_id)
        if len(msgs) < 2:
            return err("not enough history")
        text = "\n".join(f"{m['speaker']}: {m['content']}" for m in msgs)
        role = self.s.get_role(
            self.s.c.execute("SELECT role_id FROM chatrooms WHERE id=?",
                             (chatroom_id,)).fetchone()["role_id"])
        s = llm.summarize_for_memory(text, role["name"])
        if not s:
            return err("summarize failed")
        self.s.add_memory(chatroom_id, "summary", s)
        return ok({"summary": s})

    # ---- moment（动态流）----------------------------------------------
    # ---------- 内容安全 ----------
    def _screen(self, *texts, field: str = "") -> dict | None:
        """内容安全护栏。返回 None 表示通过；否则返回错误响应。

        多个字段（如角色创建的 background + firstMessage）合并后再判定，
        避免跨字段的违规组合被逐个检查时漏掉。
        留证失败不影响拦截本身 —— 宁可少一条记录，不能不拦。
        """
        blob = "\n".join(t for t in texts if t)
        r = moderation.screen(blob, field=field)
        if not r.get("blocked"):
            return None
        try:
            u = self.s.ensure_user()
            self.s.log_moderation(u["id"], field, r["severity"], r["reason"], blob)
            n = self.s.moderation_count(u["id"], "csam")
            print(f"  ⚠️  内容拦截 user={u['id']} field={field} "
                  f"severity={r['severity']} csam累计={n}")
        except Exception as e:
            print(f"  ⚠️  留证写入失败（拦截仍生效）: {e}")
        return err(moderation.refusal_message(r["severity"]))

    def moment_list(self) -> dict:
        u = self.s.ensure_user()
        return ok(self.s.list_moments(u["id"]))

    def moment_create(self, body: dict) -> dict:
        """发帖：free 额度 → hearts（对齐 Vesperine 的 free → heart 策略链）"""
        u = self.s.ensure_user()
        content = (body.get("content") or "").strip()
        if not content:
            return err("empty content")
        blocked = self._screen(content, field="moment")
        if blocked:
            return blocked
        cost = MOMENT_COST["post"]

        # 1) 免费额度
        fq = self.s.c.execute(
            "SELECT * FROM free_quota WHERE user_id=? AND type='momentPost'",
            (u["id"],)).fetchone()
        if fq and fq["count"] < fq["limit_"]:
            self.s.c.execute(
                "UPDATE free_quota SET count=count+1 WHERE user_id=? AND type='momentPost'",
                (u["id"],))
            self.s.c.commit()
            cost = 0
        else:
            # 2) 扣 hearts
            info = self.s.heart_info(u["id"])
            if info["amount"] < cost:
                return err("NOT_ENOUGH_HEARTS")
            self.s.add_hearts(u["id"], -cost, "moment_post")

        role_id = body.get("roleId")
        mid = self.s.create_moment(u["id"], content,
                                   int(role_id) if role_id else None,
                                   body.get("tags") or [])
        self.track("moment")
        return ok({"id": mid, "cost": cost,
                   "heartInfo": self.s.heart_info(u["id"])})

    def moment_like(self, body: dict, on: bool) -> dict:
        u = self.s.ensure_user()
        mid = int(body.get("momentId", 0))
        if not self.s.get_moment(mid):
            return err("moment not found")
        n = self.s.toggle_like(u["id"], mid, on)
        return ok({"momentId": mid, "liked": on, "likeCount": n})

    def moment_save(self, body: dict, on: bool) -> dict:
        u = self.s.ensure_user()
        mid = int(body.get("momentId", 0))
        if not self.s.get_moment(mid):
            return err("moment not found")
        self.s.toggle_save(u["id"], mid, on)
        return ok({"momentId": mid, "saved": on})

    def moment_delete(self, body: dict) -> dict:
        u = self.s.ensure_user()
        mid = int(body.get("momentId", 0))
        return ok({"deleted": self.s.delete_moment(u["id"], mid)})

    # ---- 删除：会话 / 记忆 / 角色 ---------------------------------
    #
    # 三者都需要校验归属，且要用「删了几行」而非布尔返回 ——
    # 前端要区分「不存在」（别人删了/刷新）和「不是你的」（越权），
    # 文字提示完全不同。越权统一回 not found，不泄露资源是否存在。

    def chatroom_delete(self, body: dict) -> dict:
        """删会话。连带清空它的消息与记忆 —— messages/memories
        没有 user_id，不级联就会留下孤儿行（隐私残留 + 统计对不上）。"""
        u = self.s.ensure_user()
        try:
            cid = int(body.get("chatroomId", 0))
        except (TypeError, ValueError):
            return err("chatroom not found")
        n = self.s.delete_chatroom(u["id"], cid)
        if not n:
            return err("chatroom not found")
        return ok({"deleted": n})

    def memory_delete(self, body: dict) -> dict:
        u = self.s.ensure_user()
        try:
            mid = int(body.get("memoryId", 0))
        except (TypeError, ValueError):
            return err("memory not found")
        n = self.s.delete_memory(u["id"], mid)
        if not n:
            return err("memory not found")
        return ok({"deleted": n})

    def role_delete(self, body: dict) -> dict:
        """删自建角色。内置的 6 个 creator='system'，任何人都删不掉。"""
        u = self.s.ensure_user()
        try:
            rid = int(body.get("roleId", 0))
        except (TypeError, ValueError):
            return err("role not found")
        n = self.s.delete_role(u["id"], rid)
        if not n:
            return err("role not found or not yours")
        return ok({"deleted": n})

    def moment_comments(self, moment_id: int) -> dict:
        return ok(self.s.list_comments(moment_id))

    def moment_comment_create(self, body: dict) -> dict:
        u = self.s.ensure_user()
        mid = int(body.get("momentId", 0))
        content = (body.get("content") or "").strip()
        if not self.s.get_moment(mid):
            return err("moment not found")
        if not content:
            return err("empty content")
        blocked = self._screen(content, field="moment_comment")
        if blocked:
            return blocked
        cost = MOMENT_COST["comment"]
        info = self.s.heart_info(u["id"])
        if info["amount"] < cost:
            return err("NOT_ENOUGH_HEARTS")
        self.s.add_hearts(u["id"], -cost, "moment_comment")
        c = self.s.add_comment(mid, u["id"], content)
        c["cost"] = cost
        self.track("comment")
        return ok(c)

    def moment_saved_list(self) -> dict:
        u = self.s.ensure_user()
        ids = [r["moment_id"] for r in self.s.c.execute(
            "SELECT moment_id FROM moment_saves WHERE user_id=?", (u["id"],)).fetchall()]
        return ok([self.s.get_moment(i) for i in ids if self.s.get_moment(i)])

    def moment_tag_candidates(self) -> dict:
        """对齐 /moment/tag-role-candidates：可供倒应的角色列表"""
        return ok([{"id": r["id"], "name": r["name"]} for r in self.s.list_roles()])

    # ---- 配置（前端读取广告/支付模式）--------------------------------
    def config(self) -> dict:
        m = AD_CONFIG["mode"]
        return ok({
            "adMode": m,
            "ad": AD_CONFIG.get(m, AD_CONFIG["test"]),
            "paymentMode": PAYMENT_MODE,
            "featureFlags": {
                "realIap": PAYMENT_MODE == "live",
                "realAdSdk": m == "live",
                "heartPurchase": True,
            },
        })

    # ---- 举报 ----------------------------------------------------------
    # Apple Review Guidelines 与 Google Play 政策都要求 UGC 应用
    # 提供举报与屏蔽，这是上架硬门槛。

    REPORT_REASONS = [
        {"id": "csam",       "label": "涉及未成年人", "urgent": True},
        {"id": "sexual",     "label": "不当性内容"},
        {"id": "violence",   "label": "暴力或血腥"},
        {"id": "hate",       "label": "仇恨或歧视"},
        {"id": "harassment", "label": "骚扰或霸凌"},
        {"id": "spam",       "label": "垃圾信息"},
        {"id": "ip",         "label": "侵权"},
        {"id": "other",      "label": "其他"},
    ]
    REPORT_TARGETS = {"role", "moment", "comment", "user", "album",
                      "scenario", "message"}

    def report_reasons(self) -> dict:
        """举报原因选项（前端弹窗用）。"""
        return ok(self.REPORT_REASONS)

    def _report_snapshot(self, ttype: str, tid: int) -> str:
        """举报当时的内容快照。

        内容事后可能被删除或修改，没快照就无法复核工单。
        取不到就算了 —— 不能因为快照失败而拒绝用户的举报。
        """
        try:
            if ttype == "moment":
                r = self.s.get_moment(tid)
                return (r or {}).get("content", "") or ""
            if ttype == "role":
                r = self.s.get_role(tid)
                if r:
                    return f"{r.get('name','')}: {r.get('description','')}"
            if ttype == "comment":
                row = self.s.c.execute(
                    "SELECT content FROM moment_comments WHERE id=?", (tid,)).fetchone()
                return row["content"] if row else ""
            if ttype == "scenario":
                row = self.s.c.execute(
                    "SELECT title, description FROM scenarios WHERE id=?", (tid,)).fetchone()
                return f"{row['title']}: {row['description']}" if row else ""
            if ttype == "message":
                # AI 回复的举报：Play 生成式 AI 政策要求应用内可举报不当输出。
                # 快照必须留原文 —— 这条消息可能随后被处置或删除。
                row = self.s.c.execute(
                    "SELECT speaker, content FROM messages WHERE id=?", (tid,)).fetchone()
                return f"[{row['speaker']}] {row['content']}" if row else ""
        except Exception:
            pass
        return ""

    # ---- 官网线索 ---------------------------------------------------
    # 简单的提交频率限制：同一 IP 10 分钟内最多 3 条。
    # 不引入额外依赖，用内存字典即可 —— 重启丢失反而正合适（本来就只是防误触/脚本）。
    # 10 分钟 10 条。原先设 3 条，但测试时立刻踩到问题：
    # 一个办公室里 10 个人同时咨询，出口 IP 相同，第 4 个人就会被拒。
    # 这是 B2B 官网，同一家公司多人同时填表完全正常。
    # 真正的滥用防护靠下面两点：字段长度限制 + 落库后人工筛。
    _LEAD_WINDOW = 600
    _LEAD_MAX_PER_WINDOW = 10
    _lead_hits: dict = {}

    def lead_create(self, body: dict) -> dict:
        """接收官网联系表单。

        不需要登录 —— 填表的人不是 App 用户。
        先落库再发邮件：邮件失败也不能丢线索。
        """
        name = (body.get("name") or "").strip()
        company = (body.get("company") or "").strip()
        contact = (body.get("contact") or "").strip()
        need = (body.get("need") or "").strip()

        # 字段校验
        if not name:
            return err("MISSING_NAME")
        if not contact:
            return err("MISSING_CONTACT")
        if len(name) > 100 or len(company) > 120 or len(contact) > 160 or len(need) > 4000:
            return err("TOO_LONG")

        # 极简防刷：同一 IP 时间窗内提交次数
        ip = self.client_ip
        now_ts = time.time()
        hits = [t for t in API._lead_hits.get(ip, []) if now_ts - t < API._LEAD_WINDOW]
        if len(hits) >= API._LEAD_MAX_PER_WINDOW:
            return err("RATE_LIMITED")
        hits.append(now_ts)
        API._lead_hits[ip] = hits
        # 顺手清理过期条目，避免字典无限增长
        if len(API._lead_hits) > 500:
            API._lead_hits = {k: v for k, v in API._lead_hits.items()
                              if any(now_ts - t < API._LEAD_WINDOW for t in v)}

        lead_id = self.s.create_lead(
            name, company, contact, need, source="website",
            ip=ip, ua=(self.user_agent or "")[:300])

        # 发邮件通知（未配置 SMTP 时返回 skipped，不算失败）
        status = mailer.send_lead({
            "id": lead_id, "name": name, "company": company,
            "contact": contact, "need": need, "ip": ip,
        })
        try:
            self.s.mark_lead_mail(lead_id, status)
        except Exception:
            pass

        print(f"  📩 新线索 #{lead_id} {name} / {company or '-'} / {contact} (mail={status})")
        return ok({"id": lead_id, "mail": status})

    def lead_list(self) -> dict:
        """线索列表。生产环境必须加管理员鉴权 —— 与 report_queue 同级别风险。"""
        return ok({"items": self.s.list_leads(50)})

    def report_create(self, body: dict) -> dict:
        u = self.s.ensure_user()
        ttype = (body.get("targetType") or "").strip()
        try:
            tid = int(body.get("targetId", 0))
        except (TypeError, ValueError):
            return err("invalid targetId")
        reason = (body.get("reason") or "").strip()
        detail = (body.get("detail") or "").strip()

        if ttype not in self.REPORT_TARGETS:
            return err("invalid targetType")
        if not tid:
            return err("missing targetId")
        if reason not in {r["id"] for r in self.REPORT_REASONS}:
            return err("invalid reason")

        snap = self._report_snapshot(ttype, tid)
        r = self.s.create_report(u["id"], ttype, tid, reason, detail, snap)
        if r["priority"] == "urgent":
            print(f"  🚨 紧急举报 id={r['id']} type={ttype} target={tid} "
                  f"user={u['id']}（涉未成年人内容，优先处理）")
        return ok({"id": r["id"], "duplicate": r["duplicate"],
                   "priority": r["priority"]})

    def report_queue(self) -> dict:
        """待处理工单。注：production 必须加管理员鉴权。"""
        return ok({"counts": self.s.report_counts(),
                   "items": self.s.report_queue()})

    def report_resolve(self, body: dict) -> dict:
        """处置工单。status: reviewed | actioned | dismissed"""
        try:
            rid = int(body.get("reportId", 0))
        except (TypeError, ValueError):
            return err("invalid reportId")
        status = (body.get("status") or "").strip()
        if status not in ("reviewed", "actioned", "dismissed"):
            return err("invalid status")
        self.s.resolve_report(rid, status)
        return ok({"reportId": rid, "status": status})

    # ---- IAP -----------------------------------------------------------
    # 商品 ID 带包名前缀：Play Console 里的商品 ID 在应用内全局唯一，
    # 加前缀可避免以后多应用混淆。改这里必须同步 Play Console 配置。
    #
    # 为什么从 12 档改成 4 档：
    # 原定义是 {plan{i}: i*5+5 for i in 1..12}，价格 $2.99 + (i-1)*$8.00，
    # 结果是 plan1=10 hearts/$2.99、plan2=15 hearts/$10.99 ——
    # 贵 4 倍只多 5 个 hearts，性价比完全不成立，用户无法做选择。
    #
    # 现在的档位按「单价递减」设计（大额更划算），这是虚拟货币的通行做法：
    #   30   hearts  $1.99   $0.066/个
    #   100  hearts  $4.99   $0.050/个
    #   300  hearts  $12.99  $0.043/个
    #   800  hearts  $29.99  $0.037/个
    #
    # 参照消耗：一条 classic 消息 2 hearts、一张相册图 21 hearts。
    # 30 hearts ≈ 15 条对话，$1.99 对首次付费是合适的门槛。
    #
    # 注意：这里的 price 只是显示用的参考值，真实价格以 Play Console
    # 里配置的为准（各区域自动换算）。客户端应优先用 Google 返回的
    # formattedPrice，拿不到时才回退到这个值。
    IAP_PLANS = {
        "top.lurvy.vesperine.hearts30":  {"hearts": 30,  "price": "$1.99",  "label": "入门"},
        "top.lurvy.vesperine.hearts100": {"hearts": 100, "price": "$4.99",  "label": "常用"},
        "top.lurvy.vesperine.hearts300": {"hearts": 300, "price": "$12.99", "label": "超值"},
        "top.lurvy.vesperine.hearts800": {"hearts": 800, "price": "$29.99", "label": "大额"},
    }

    def iap_plans(self) -> dict:
        """商品列表。字段与旧版兼容（id/hearts/price），新增 label。"""
        out = []
        for pid, cfg in self.IAP_PLANS.items():
            out.append({"id": pid, "hearts": cfg["hearts"],
                        "price": cfg["price"], "label": cfg["label"]})
        return ok(out)

    def iap_purchase(self, body: dict) -> dict:
        u = self.s.ensure_user()
        plan_id = body.get("planId", "")
        if plan_id not in self.IAP_PLANS:
            return err("unknown plan")
        hearts = self.IAP_PLANS[plan_id]["hearts"]

        # ── 测试模式：直接发放，不需要真实支付 ──
        if PAYMENT_MODE != "live":
            balance = self.s.add_hearts(u["id"], hearts, f"iap_stub:{plan_id}")
            return ok({"planId": plan_id, "hearts": hearts, "balance": balance,
                       "mode": "stub",
                       "notice": "未接入真实支付，仅用于测试环境验证业务逻辑"})

        # ── 生产模式：必须向 Google 核对 ──
        token = (body.get("purchaseToken") or "").strip()
        if not token:
            return err("missing purchaseToken")

        # 幂等：同一 token 只能兑换一次，否则客户端重放就能刷 hearts
        if self.s.purchase_seen(token):
            return err("purchaseToken 已被使用", flag=2)

        sa = iap_verify.service_account()
        if not sa:
            return err("服务端未配置 Google Play 凭据", flag=2)

        r = iap_verify.verify_purchase(plan_id, token, sa=sa)
        if not r.get("ok"):
            # 失败也留痕，否则同一个假 token 会被无限重试刷日志
            self.s.record_purchase(u["id"], plan_id, token, "failed",
                                   str(r.get("error", "")))
            return err(f"订单校验未通过：{r.get('error') or '状态异常'}", flag=2)

        # 先落库再发放：万一后续步骤崩溃，这条记录可用于对账补偿
        self.s.record_purchase(u["id"], plan_id, token, "verified", "")
        balance = self.s.add_hearts(u["id"], hearts, f"iap:{plan_id}")

        # consume：消耗型商品必须标记已消费，
        # 否则用户买过一次就再也买不了同一档。
        consumed = iap_verify.consume_purchase(plan_id, token, sa=sa)
        return ok({"planId": plan_id, "hearts": hearts, "balance": balance,
                   "mode": "live", "consumed": consumed})

    # ---- affinity / hidden chapters --------------------------------
    def affinity_get(self, role_id: int) -> dict:
        u = self.s.ensure_user()
        v = self.s.get_affinity(u["id"], role_id)
        return ok({"roleId": role_id, "value": v,
                   "label": affinity.describe(v),
                   "min": self.s.AFFINITY_MIN, "max": self.s.AFFINITY_MAX})

    def affinity_overview(self) -> dict:
        u = self.s.ensure_user()
        rows = self.s.affinity_leaderboard(u["id"])
        for r in rows:
            r["label"] = affinity.describe(r["value"])
        return ok(rows)

    def chapter_list(self, role_id: int) -> dict:
        """对齐 /hidden-chapter：未解锁只返回无剧透 intro。"""
        u = self.s.ensure_user()
        rows = self.s.list_chapters(u["id"], role_id)
        pub = [{k: c[k] for k in ("id", "seq", "type", "affinity_threshold",
                                  "title", "intro", "unlocked", "enteredAt",
                                  "content", "currentAffinity")} for c in rows]
        unlocked = sum(1 for c in pub if c["unlocked"])
        return ok({"chapters": pub, "affinity": pub[0]["currentAffinity"] if pub else 0,
                   "unlockedCount": unlocked, "total": len(pub),
                   "limit": self.s.CHAPTER_LIMIT})

    def chapter_batch(self, body: dict) -> dict:
        """创作者侧：批量写入章节（对齐 /hidden-chapter/batch）。"""
        role_id = int(body.get("roleId", 0))
        if not self.s.get_role(role_id):
            return err("role not found")
        items = body.get("chapters") or []
        added = 0
        for i, c in enumerate(items):
            try:
                self.s.add_chapter(role_id, int(c.get("seq", i)),
                                   c.get("type", "positive"),
                                   int(c.get("affinityThreshold", 0)),
                                   c.get("title", ""), c.get("intro", ""),
                                   c.get("content", ""))
                added += 1
            except ValueError as e:
                return err(str(e))
        return ok({"added": added})

    def chapter_enter(self, body: dict) -> dict:
        """进入章节，并把剧情写入该角色的记忆。"""
        u = self.s.ensure_user()
        cid = int(body.get("chapterId", 0))
        role_id = int(body.get("roleId", 0))
        affv = self.s.get_affinity(u["id"], role_id)
        ch = self.s.enter_chapter(u["id"], cid)
        if not ch:
            return err("chapter not found")
        th = ch["affinity_threshold"]
        unlocked = affv <= th if ch["type"] == "negative" else affv >= th
        if not unlocked:
            return err("chapter locked")
        # 找到该角色下的会话，写入记忆（对齐官方：章节内容会同步到记忆）
        cr = self.s.c.execute(
            "SELECT id FROM chatrooms WHERE user_id=? AND role_id=?"
            " ORDER BY updated_at DESC LIMIT 1", (u["id"], role_id)).fetchone()
        if cr:
            self.s.add_memory(cr["id"], "summary",
                              f"[Hidden Chapter: {ch['title']}] {ch['content'][:400]}")
        self.track("chapter_unlock")
        return ok({"chapterId": cid, "title": ch["title"],
                   "content": ch["content"], "syncedToMemory": bool(cr)})

    # ---- scenario（情景）----------------------------------------------
    def scenario_list(self, q: str = "", category: str = "", location: str = "",
                      scope: str = "all") -> dict:
        u = self.s.ensure_user()
        mine = u["id"] if scope == "mine" else None
        rows = self.s.list_scenarios(u["id"], q=q, category=category,
                                     location=location, mine=mine,
                                     liked=(scope == "liked"))
        return ok(rows)

    def scenario_detail(self, sid: int) -> dict:
        s = self.s.get_scenario(sid)
        return ok(s) if s else err("scenario not found")

    def scenario_meta(self) -> dict:
        return ok({"categories": self.s.SCENARIO_CATEGORIES,
                   "locations": self.s.scenario_locations()})

    def scenario_create(self, body: dict) -> dict:
        u = self.s.ensure_user()
        title = (body.get("title") or "").strip()
        if not title:
            return err("title required")
        sid = self.s.create_scenario(
            u["id"], title=title, description=body.get("description", ""),
            location=body.get("location", ""), category=body.get("category", ""),
            cover=body.get("cover", ""), tags=body.get("tags"),
            opener=body.get("opener", ""), isPublic=body.get("isPublic", True))
        self.s.ensure_creator(u["id"], u["nickname"])
        return ok({"id": sid})

    def scenario_update(self, body: dict) -> dict:
        u = self.s.ensure_user()
        sid = int(body.get("scenarioId", 0))
        kw = {k: body[k] for k in ("title", "description", "location", "category",
                                   "cover", "tags", "opener", "isPublic")
              if k in body}
        return ok({"updated": self.s.update_scenario(sid, u["id"], **kw)})

    def scenario_delete(self, body: dict) -> dict:
        u = self.s.ensure_user()
        sid = int(body.get("scenarioId", 0))
        return ok({"deleted": self.s.delete_scenario(sid, u["id"])})

    def scenario_like(self, body: dict, on: bool) -> dict:
        u = self.s.ensure_user()
        sid = int(body.get("scenarioId", 0))
        if not self.s.get_scenario(sid):
            return err("scenario not found")
        n = self.s.toggle_scenario_like(u["id"], sid, on)
        return ok({"scenarioId": sid, "liked": on, "likeCount": n})

    def scenario_batch_liked(self, body: dict) -> dict:
        u = self.s.ensure_user()
        ids = body.get("ids") or []
        out = {}
        for i in ids:
            out[str(i)] = bool(self.s.c.execute(
                "SELECT 1 FROM scenario_likes WHERE user_id=? AND scenario_id=?",
                (u["id"], int(i))).fetchone())
        return ok(out)

    def scenario_start(self, body: dict) -> dict:
        """用情景开聊（对齐 /chatroom/listScenario）。

        开场顺序：先情景设定（opener），再角色开场白 —— 这样用户先读到的
        是「这是什么场景」，然后是角色的第一句话，符合叙事直觉。
        """
        u = self.s.ensure_user()
        sid = int(body.get("scenarioId", 0))
        s = self.s.get_scenario(sid)
        if not s:
            return err("scenario not found")
        role_id = body.get("roleId")
        if not role_id:
            return err("roleId required")
        mode = body.get("chatMode", "story")
        # with_intro=False：先不插角色开场，手动控制顺序
        cr = self.s.create_chatroom(u["id"], int(role_id), mode, "standard",
                                    with_intro=False)
        if s.get("opener"):
            self.s.add_message(cr["id"], "assistant",
                               f"〔{s['title']}〕\n{s['opener']}")
        role = self.s.get_role(int(role_id))
        if role and role.get("first_message"):
            self.s.add_message(cr["id"], "assistant", role["first_message"])
        self.s.bump_scenario_play(sid)
        return ok({"chatroomId": cr["id"], "scenarioId": sid, "title": s["title"]})

    # ---- creator / follow --------------------------------------------
    def creator_get(self, user_id: int) -> dict:
        u = self.s.ensure_user()
        c = self.s.ensure_creator(user_id, u["nickname"] if user_id == u["id"] else "")
        c["overview"] = self.s.roles_overview(user_id)
        c["roles"] = [r for r in self.s.list_roles()
                      if str(r.get("creator")) == str(user_id)]
        return ok(c)

    def creator_update(self, body: dict) -> dict:
        u = self.s.ensure_user()
        c = self.s.update_creator(u["id"], body.get("displayName", ""),
                                  body.get("bio", ""))
        return ok(c)

    def follow_action(self, body: dict, action: str) -> dict:
        u = self.s.ensure_user()
        target = int(body.get("creatorId", 0))
        if not target:
            return err("creatorId required")
        if action == "request":
            r = self.s.follow(u["id"], target, auto_accept=body.get("autoAccept", True))
            return ok(r) if r["ok"] else err(r["reason"])
        if action == "delete":
            self.s.unfollow(u["id"], target)
            return ok({"unfollowed": target})
        return err("unknown action")

    def follower_action(self, body: dict, action: str) -> dict:
        u = self.s.ensure_user()
        other = int(body.get("followerId", 0))
        if action == "delete":
            self.s.remove_follower(u["id"], other)
            return ok({"removed": other})
        if action == "accept":
            return ok({"accepted": self.s.accept_follower(u["id"], other)})
        return err("unknown action")

    def social_lists(self) -> dict:
        u = self.s.ensure_user()
        return ok({"following": self.s.following_list(u["id"]),
                   "followers": self.s.follower_list(u["id"]),
                   "requests": self.s.pending_requests(u["id"]),
                   "hasUnread": self.s.has_unread_requests(u["id"])})

    def mark_follow_read(self) -> dict:
        u = self.s.ensure_user()
        self.s.mark_requests_read(u["id"])
        return ok({"ok": True})

    def block_action(self, body: dict, on: bool) -> dict:
        u = self.s.ensure_user()
        target = int(body.get("creatorId", 0))
        self.s.block_creator(u["id"], target, on)
        return ok({"creatorId": target, "blocked": on})

    def block_list(self) -> dict:
        u = self.s.ensure_user()
        return ok(self.s.block_list(u["id"]))

    def roles_overview(self) -> dict:
        u = self.s.ensure_user()
        return ok(self.s.roles_overview(u["id"]))

    # ---- UGC 头像生成 --------------------------------------------------
    def ugc_styles(self) -> dict:
        return ok(imagegen.list_styles())

    # 哪个 reason 用哪份额度。
    # 旧版 _charge 里写死 fq_type = "roleUgc"，导致头像（6 hearts）
    # 和相册（21 hearts）共用同一份 3 次免费名额 ——
    # 用户可以把本该 21 hearts 的相册图用头像额度白嫆 3 张。
    # 没列在这里的 reason 不给免费额度，直接扣 hearts。
    FREE_QUOTA_FOR = {
        "ugc_text": "roleUgc",
        "ugc_photo": "roleUgc",
        # album_image 刻意不给免费额度：单价高（21），
        # 且它是一次性消费型功能，不应该成为拉新手段。
    }

    def _charge(self, u: dict, cost: int, reason: str):
        """免费额度 → hearts 的两级策略。返回 (ok, error_response)。

        返回实际的 cost（0 表示走了免费额度），以及消耗的额度类型，
        调用方在失败时可用它们回滚。
        """
        fq_type = self.FREE_QUOTA_FOR.get(reason)
        if fq_type:
            fq = self.s.c.execute(
                "SELECT * FROM free_quota WHERE user_id=? AND type=?",
                (u["id"], fq_type)).fetchone()
            if fq and fq["count"] < fq["limit_"]:
                self.s.c.execute(
                    "UPDATE free_quota SET count=count+1 WHERE user_id=? AND type=?",
                    (u["id"], fq_type))
                self.s.c.commit()
                return 0, None
        info = self.s.heart_info(u["id"])
        if info["amount"] < cost:
            return cost, err("NOT_ENOUGH_HEARTS")
        self.s.add_hearts(u["id"], -cost, reason)
        return cost, None

    def _refund_charge(self, u: dict, cost: int, reason: str,
                       used_free_quota: bool) -> None:
        """生成失败时回滚扣费。

        同时回滚两样东西，缺一样用户就亏：
          - hearts（cost > 0 时）
          - 免费额度计数（cost == 0 说明这次是免费名额，也得退回去）
        旧版只退 hearts，免费额度被吃掉 —— 实测连发 3 次失败的相册生成，
        3 次额度全没，一张图也没拿到。
        """
        if cost:
            self.s.add_hearts(u["id"], cost, reason + "_refund")
            return
        fq_type = self.FREE_QUOTA_FOR.get(reason)
        if not fq_type or not used_free_quota:
            return
        self.s.c.execute(
            "UPDATE free_quota SET count=MAX(0, count-1)"
            " WHERE user_id=? AND type=?", (u["id"], fq_type))
        self.s.c.commit()

    def ugc_generate(self, body: dict) -> dict:
        """头像生成：Text-to-Avatar / Photo-to-Avatar。"""
        u = self.s.ensure_user()
        prompt = (body.get("prompt") or "").strip()
        style = body.get("style", "")
        photo_b64 = body.get("photo")            # Data URL 或裸 base64
        role_id = body.get("roleId")
        is_photo = bool(photo_b64)
        if not prompt and not is_photo:
            return err("prompt or photo required")

        cost, e = self._charge(u, 6, "ugc_photo" if is_photo else "ugc_text")
        if e:
            return e

        subj = None
        if photo_b64:
            raw = photo_b64.split(",", 1)[-1]      # 容错 Data URL 前缀
            try:
                subj = base64.b64decode(raw)
            except Exception:
                return err("invalid photo encoding")

        try:
            res = imagegen.generate(prompt or "portrait of a character",
                                    style=style, subject_image=subj,
                                    aspect_ratio=body.get("aspectRatio", "1:1"),
                                    n=int(body.get("n", 1)))
        except Exception as ex:
            # 生成失败把 hearts 和免费额度都退回去 —— 不赚用户失败的钱，
            # 也不能吃掉他一次免费名额（旧版只退 hearts，额度照扣）。
            self._refund_charge(u, cost, "ugc_text" if not is_photo else "ugc_photo",
                                used_free_quota=(cost == 0))
            return err("图片生成失败，请稍后重试（heart 已退还）")

        paths = imagegen.save_images(res["images"], str(GENERATED_DIR),
                                     f"ugc{u['id']}-{int(time.time())}")
        ids = [self.s.add_album_item(u["id"], p, role_id=role_id, source="ugc",
                                     prompt=res["prompt"], style=style, cost=cost)
               for p in paths]
        self.track("image")
        return ok({"ids": ids, "count": len(paths),
                   "urls": [f"/generated/{os.path.basename(p)}" for p in paths],
                   "cost": cost, "mode": "photo" if is_photo else "text",
                   "heartInfo": self.s.heart_info(u["id"])})

    # ---- 相册 ------------------------------------------------------
    def album_list(self, source: str = "") -> dict:
        u = self.s.ensure_user()
        items = self.s.list_album(u["id"], source=source or None)
        for it in items:
            it["url"] = f"/generated/{os.path.basename(it['file_path'])}"
        return ok({"items": items, "hasUnread": self.s.album_has_unread(u["id"])})

    def album_generate(self, body: dict) -> dict:
        """相册图生成（消耗 21 hearts，对齐实测消耗表）。"""
        u = self.s.ensure_user()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return err("prompt required")
        cost, e = self._charge(u, 21, "album_image")
        if e:
            return e
        try:
            res = imagegen.generate(prompt, style=body.get("style", ""),
                                    aspect_ratio=body.get("aspectRatio", "1:1"),
                                    n=int(body.get("n", 1)))
        except Exception as ex:
            self._refund_charge(u, cost, "album_image",
                                used_free_quota=(cost == 0))
            return err("图片生成失败，请稍后重试（heart 已退还）")
        paths = imagegen.save_images(res["images"], str(GENERATED_DIR),
                                     f"album{u['id']}-{int(time.time())}")
        ids = [self.s.add_album_item(u["id"], p, role_id=body.get("roleId"),
                                     source="album", prompt=res["prompt"],
                                     style=body.get("style", ""), cost=cost)
               for p in paths]
        self.track("image")
        return ok({"ids": ids, "count": len(paths),
                   "urls": [f"/generated/{os.path.basename(p)}" for p in paths],
                   "cost": cost, "heartInfo": self.s.heart_info(u["id"])})

    def album_mark_read(self) -> dict:
        u = self.s.ensure_user()
        self.s.album_mark_read(u["id"])
        return ok({"ok": True})

    # ---- 角色创建 / 管理（Creator Studio 侧）--------------------------
    # 字段限制对齐官方角色创建指南：
    #   名称 30 字 / identity 5 个 / personality 10 个 /
    #   背景 3000 字 / 开场白 1000 字 / tag 20 个
    ROLE_LIMITS = {"name": 30, "background": 3000, "first_message": 1000, "tags": 20}

    def role_create(self, body: dict) -> dict:
        u = self.s.ensure_user()
        name = (body.get("name") or "").strip()
        if not name:
            return err("name required")
        if len(name) > self.ROLE_LIMITS["name"]:
            return err(f"name too long (max {self.ROLE_LIMITS['name']})")
        bg = body.get("background") or ""
        if len(bg) > self.ROLE_LIMITS["background"]:
            return err(f"background too long (max {self.ROLE_LIMITS['background']})")
        fm = body.get("firstMessage") or ""
        if len(fm) > self.ROLE_LIMITS["first_message"]:
            return err(f"first message too long (max {self.ROLE_LIMITS['first_message']})")
        tags = body.get("tags") or []
        if len(tags) > self.ROLE_LIMITS["tags"]:
            return err(f"too many tags (max {self.ROLE_LIMITS['tags']})")

        # 角色设定是公开内容，且会被反复用于生成对话，
        # 因此 background / firstMessage / description 合并判定
        blocked = self._screen(
            bg, fm,
            body.get("description", ""),
            body.get("personality", ""),
            field="role_create")
        if blocked:
            return blocked

        cur = self.s.c.execute(
            "INSERT INTO roles (name, description, personality, background,"
            " first_message, status_bar, tags, creator, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (name, body.get("description", ""), body.get("personality", ""), bg,
             fm, body.get("statusBar", ""), json.dumps(tags), str(u["id"]), now()))
        self.s.c.commit()
        rid = cur.lastrowid
        # 同步创作者统计（不传 display_name，避免用昵称覆盖已设置的创作者名）
        self.s.update_creator(u["id"])
        self.track("role_create")
        return ok({"id": rid, "name": name})

    def role_update(self, body: dict) -> dict:
        u = self.s.ensure_user()
        rid = int(body.get("roleId", 0))
        r = self.s.get_role(rid)
        if not r:
            return err("role not found")
        if str(r.get("creator")) != str(u["id"]):
            return err("not your role", flag=3)
        fields, args = [], []
        mapping = {"name": "name", "description": "description",
                   "personality": "personality", "background": "background",
                   "firstMessage": "first_message", "statusBar": "status_bar"}
        for k, col in mapping.items():
            if k in body:
                fields.append(f"{col}=?"); args.append(body[k])
        if "tags" in body:
            fields.append("tags=?"); args.append(json.dumps(body["tags"] or []))
        if not fields:
            return err("nothing to update")
        args.append(rid)
        self.s.c.execute(f"UPDATE roles SET {','.join(fields)} WHERE id=?", args)
        self.s.c.commit()
        return ok({"updated": rid})

    def role_mine(self) -> dict:
        u = self.s.ensure_user()
        rows = [r for r in self.s.list_roles() if str(r.get("creator")) == str(u["id"])]
        return ok(rows)

    # ---- quest / achievement / notification / sticker ----------------
    def quest_dashboard(self) -> dict:
        """对齐 /quest/dashboard"""
        u = self.s.ensure_user()
        daily = self.s.list_quests(u["id"], "daily")
        goals = self.s.list_quests(u["id"], "goal")
        ach = self.s.list_achievements(u["id"])
        return ok({
            "daily": daily, "goals": goals,
            "dailyDone": sum(1 for q in daily if q["completed"]),
            "dailyTotal": len(daily),
            "claimable": sum(1 for q in daily + goals if q["claimable"]),
            "achievements": ach,
            "achievementsUnlocked": sum(1 for a in ach if a["unlocked"]),
            "stats": self.s.stats(u["id"]),
        })

    def quest_list(self, qtype: str) -> dict:
        u = self.s.ensure_user()
        return ok(self.s.list_quests(u["id"], qtype or "daily"))

    # 每日签到的奖励额度。设计约束：必须**独立于任何消耗行为**，
    # 因为 hearts 归零的用户做不了任何任务（发消息/动态/评论全要付费），
    # 一旦没有零成本入口就是永久死锁 —— 签到是唯一的出口。
    CHECKIN_HEARTS = 10

    def daily_checkin(self) -> dict:
        """每日签到。零成本，是零余额用户唯一的自救路径。

        不再先 has_checked_in() 再 add_hearts()：那两步之间没有互斥，
        实测 12 个并发请求会拿到 2 次发放。改成 add_hearts_once()，
        由唯一索引保证一天只有一次能写进流水。
        """
        u = self.s.ensure_user()
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))
        r = self.s.add_hearts_once(u["id"], self.CHECKIN_HEARTS, "checkin:" + day)
        if not r["granted"]:
            return err("ALREADY_CHECKED_IN")
        return ok({"hearts": self.CHECKIN_HEARTS, "balance": r["balance"], "day": day})

    def checkin_status(self) -> dict:
        u = self.s.ensure_user()
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 3600))
        return ok({"checkedIn": self.s.has_checked_in(u["id"], day),
                   "reward": self.CHECKIN_HEARTS, "day": day})

    def quest_claim(self, body: dict) -> dict:
        u = self.s.ensure_user()
        qid = int(body.get("questId", 0))
        r = self.s.claim_quest(u["id"], qid)
        if not r["ok"]:
            return err(r["reason"])
        balance = self.s.add_hearts(u["id"], r["hearts"], "quest_claim")
        self.s.add_notification(u["id"], "system", "Quest complete",
                                f"+{r['hearts']} hearts")
        return ok({"hearts": r["hearts"], "balance": balance})

    def achievements(self) -> dict:
        u = self.s.ensure_user()
        # 每次拉取时重算进度，未解锁但已达阈值的自动解锁
        st = self.s.stats(u["id"])
        newly = []
        for a in self.s.list_achievements(u["id"]):
            if not a["unlocked"] and st.get(a["metric"], 0) >= a["threshold"]:
                if self.s.unlock_achievement(u["id"], a["code"]):
                    newly.append(a["title"])
                    sticker = ACHIEVEMENT_STICKERS.get(a["code"])
                    if sticker:
                        self.s.grant_sticker(u["id"], sticker)
                    self.s.add_notification(u["id"], "system",
                                            f"Achievement: {a['title']}", a["description"])
        return ok({"achievements": self.s.list_achievements(u["id"]),
                   "newlyUnlocked": newly})

    def notifications(self) -> dict:
        u = self.s.ensure_user()
        return ok({"items": self.s.list_notifications(u["id"]),
                   "hasUnread": self.s.has_unread_notifications(u["id"])})

    def notify_mark(self, body: dict) -> dict:
        u = self.s.ensure_user()
        n = self.s.mark_notifications_read(u["id"], body.get("kind"))
        return ok({"marked": n})

    def sticker_list(self) -> dict:
        u = self.s.ensure_user()
        return ok({"categories": self.s.sticker_categories(u["id"]),
                   "owned": self.s.user_stickers(u["id"])})

    # ---- 行为埋点（推进任务进度）-----------------------------------
    def track(self, event_key: str) -> None:
        """失败不影响主流程。"""
        try:
            u = self.s.ensure_user()
            self.s.bump_quest(u["id"], event_key)
        except Exception:
            pass

    # ---- 语音（TTS）----------------------------------------------
    def speech_generate(self, body: dict) -> dict:
        """对齐 /message/speech/generate：把一条角色消息转成语音。"""
        u = self.s.ensure_user()
        message_id = body.get("messageId")
        cid = int(body.get("chatroomId", 0))
        role_name = body.get("roleName", "")

        # 优先按 messageId 取原文；否则允许直接传 text（便于试听）
        text = body.get("text") or ""
        if message_id and cid:
            row = self.s.c.execute(
                "SELECT content FROM messages WHERE id=? AND chatroom_id=?",
                (int(message_id), cid)).fetchone()
            if row:
                text = row["content"]
        if not text:
            return err("message not found or empty")

        # 朗读也走护栏：模型可能生成了边界内容，
        # 手动传入 text 试听时同样要拦。
        blocked = self._screen(text, field="speech")
        if blocked:
            return blocked

        if not role_name and cid:
            cr = self.s.c.execute(
                "SELECT r.name FROM chatrooms c JOIN roles r ON r.id=c.role_id"
                " WHERE c.id=?", (cid,)).fetchone()
            role_name = cr["name"] if cr else ""

        r = tts.synthesize(text, role_name)
        if not r.get("ok"):
            return err(f"TTS 失败: {r.get('error','unknown')}")
        return ok({"url": r["url"], "cached": r.get("cached", False),
                   "chars": r.get("chars", 0),
                   "durationMs": r.get("duration_ms", 0),
                   "voiceId": voice_map.voice_for(role_name),
                   "roleName": role_name})

    def tts_vocals(self) -> dict:
        """对齐 /tts/vocal/list"""
        v = tts.list_voices()
        return ok({"custom": v["custom"],
                   "systemCount": len(v["system"]),
                   "systemSample": v["system"][:40]})

    # ---- 发送消息（核心：hearts 消耗 + 广告抵扣 + 流式）
    def prepare_send(self, body: dict):
        """
        校验并决定这次发送是否计费。返回 (chatroom, cost, is_ad_funded, error)。
        对齐 Vesperine 的 preCheck：free → heart → ad 三级策略。
        """
        u = self.s.ensure_user()
        cid = int(body.get("chatroomId", 0))
        row = self.s.c.execute("SELECT * FROM chatrooms WHERE id=?", (cid,)).fetchone()
        if not row:
            return None, 0, 0, err("chatroom not found")
        chatroom = dict(row)
        cost = llm.heart_cost(chatroom["chat_mode"])

        # 1) 未兑换的广告额度 → 免费
        q = self.s.c.execute(
            "SELECT unredeemed FROM ad_quota WHERE user_id=? AND type='CHAT'",
            (u["id"],)).fetchone()
        ids = json.loads(q["unredeemed"] or "[]") if q else []
        if cid in ids:
            ids.remove(cid)
            self.s.c.execute(
                "UPDATE ad_quota SET unredeemed=? WHERE user_id=? AND type='CHAT'",
                (json.dumps(ids), u["id"]))
            self.s.c.commit()
            return chatroom, 0, 1, None

        # 2) hearts 是否足够
        info = self.s.heart_info(u["id"])
        if info["amount"] < cost:
            return chatroom, cost, 0, err("NOT_ENOUGH_HEARTS")

        self.s.add_hearts(u["id"], -cost, "chat")
        return chatroom, cost, 0, None


class _PayloadTooLarge(Exception):
    """请求体超过 MAX_BODY_BYTES。

    用异常而不是返回空 dict，是因为空 dict 会被业务层当成「没传参数」
    继续往下走，最终变成一个语义模糊的 400；而这里需要的是明确的 413。
    """

    def __init__(self, size: int):
        self.size = size
        super().__init__(f"payload {size} bytes exceeds limit")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        """访问日志 —— 记录真实来源 IP 与请求行。

        默认实现只写 stderr 且不含来源 IP，出问题无法追查。
        经反向代理时以 X-Forwarded-For 的首段为准。
        """
        try:
            ip = self.client_address[0] if self.client_address else "?"
            fwd = self.headers.get("X-Forwarded-For", "") if self.headers else ""
            if fwd:
                ip = "%s via %s" % (fwd.split(",")[0].strip(), ip)
            sys.stderr.write("[%s] %s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), ip, fmt % args))
            sys.stderr.flush()
        except Exception:
            pass

    def _authorized(self) -> bool:
        """校验请求头里的访问密钥。未配置 ACCESS_KEY 时放行。

        用 compare_digest 而非 == ，避免比较过程泄露密钥长度/前缀。
        """
        if not ACCESS_KEY:
            return True
        provided = self.headers.get("X-Access-Key", "") if self.headers else ""
        try:
            return hmac.compare_digest(provided.encode(), ACCESS_KEY.encode())
        except Exception:
            return False

    def _api(self) -> API:
        """每个请求用独立 SQLite 连接。

        ThreadingHTTPServer 为每个请求开新线程，而 sqlite3 默认禁止跨线程
        复用连接（check_same_thread）。因此每请求新建连接。

        顺带解析 Bearer 令牌得到 user_id —— 复用同一个连接，不开第二次。
        """
        store = Store(connect(init_schema=False))
        store.user_id = store.user_id_by_token(self._bearer_token())
        return API(store, client_ip=self._req_client_ip(),
                   user_agent=self._req_user_agent())

    def _req_client_ip(self) -> str:
        """真实来源 IP。经 Cloudflare / nginx 时依次看这几个头。

        顺序有讲究：CF-Connecting-IP 是 Cloudflare 写入的、不可被客户端伪造；
        X-Forwarded-For 可能是客户端自己带的，取首段但排在其后。
        """
        try:
            h = self.headers
            if not h:
                return ""
            cf = (h.get("CF-Connecting-IP") or "").strip()
            if cf:
                return cf[:64]
            fwd = (h.get("X-Forwarded-For") or "").strip()
            if fwd:
                return fwd.split(",")[0].strip()[:64]
            return ((self.client_address[0] if self.client_address else "") or "")[:64]
        except Exception:
            return ""

    def _req_user_agent(self) -> str:
        try:
            return ((self.headers.get("User-Agent", "") if self.headers else "") or "")[:300]
        except Exception:
            return ""

    # ---------- 认证

    def _bearer_token(self) -> str:
        """从 Authorization 头取令牌。"""
        header = self.headers.get("Authorization", "") if self.headers else ""
        if header.startswith("Bearer "):
            return header[7:].strip()
        return ""

    @staticmethod
    def _public_user(u: dict) -> dict:
        """对外暴露的用户字段 —— 刻意不含 email 与 password_hash。"""
        return {
            "id": u.get("id"),
            "nickname": u.get("nickname", ""),
            "avatar": u.get("avatar", ""),
            "personaName": u.get("persona_name", ""),
            "personaDesc": u.get("persona_desc", ""),
            "hearts": u.get("hearts", 0),
        }

    def _register(self, body: dict) -> dict:
        email = auth.normalize_email(body.get("email", ""))
        password = body.get("password") or ""
        if not auth.looks_like_email(email):
            return err("INVALID_EMAIL")
        if len(password) < 8:
            return err("WEAK_PASSWORD")
        if len(password) > 200:
            return err("WEAK_PASSWORD")
        # 合规：注册前必须明确同意条款与隐私政策。
        # 客户端会拦一道，服务端再校验一次 —— 客户端校验挡不住直接调接口。
        if not body.get("acceptedTerms"):
            return err("TERMS_NOT_ACCEPTED")
        store = Store(connect(init_schema=False))
        if store.user_by_email(email):
            return err("EMAIL_TAKEN")
        nickname = (body.get("nickname") or "").strip()[:40] or email.split("@")[0][:40]
        user = store.create_user(
            email, auth.hash_password(password), nickname=nickname,
            persona_name=(body.get("personaName") or "").strip()[:40],
            persona_desc=(body.get("personaDesc") or "").strip()[:500],
            free_quota=FREE_QUOTA)
        token = store.create_token(user["id"], auth.TOKEN_TTL_SECONDS)
        return ok({"token": token, "user": self._public_user(user)})

    def _login(self, body: dict) -> dict:
        email = auth.normalize_email(body.get("email", ""))
        password = body.get("password") or ""
        store = Store(connect(init_schema=False))
        user = store.user_by_email(email)
        # 不区分「用户不存在」与「密码错误」，避免邮箱枚举
        if not user or not auth.verify_password(password, user.get("password_hash") or ""):
            return err("INVALID_CREDENTIALS")
        token = store.create_token(user["id"], auth.TOKEN_TTL_SECONDS)
        return ok({"token": token, "user": self._public_user(user)})

    def _change_password(self, body: dict) -> dict:
        """修改密码。需要提供当前密码，改完吊销所有令牌。

        吊销全部（而非只留当前）是刻意的：若账号曾被他人使用，
        改密码应当把对方踢下线。
        """
        store = Store(connect(init_schema=False))
        uid = store.user_id_by_token(self._bearer_token())
        if not uid:
            return err("UNAUTHORIZED")

        current = body.get("currentPassword") or ""
        newpw = body.get("newPassword") or ""
        if len(newpw) < 8 or len(newpw) > 200:
            return err("WEAK_PASSWORD")

        store.user_id = uid
        user = store.ensure_user()
        if not auth.verify_password(current, user.get("password_hash") or ""):
            return err("INVALID_CREDENTIALS")

        store.set_password(uid, auth.hash_password(newpw))
        store.revoke_all_tokens(uid)
        return ok(None)

    def _delete_account(self) -> dict:
        """删除当前账号及其全部数据。

        商店审核要求应用内可自助删除，且必须真的清空关联数据 ——
        只删 users 行会留下孤儿记录，既不合规也是隐私残留。
        """
        store = Store(connect(init_schema=False))
        uid = store.user_id_by_token(self._bearer_token())
        if not uid:
            return err("UNAUTHORIZED")
        counts = store.delete_user(uid)
        return ok({"deleted": counts})

    # ---------- 找回密码
    def _reset_base_url(self) -> str:
        """重置链接的前缀。

        优先用显式配置的 PUBLIC_BASE_URL —— 服务在反向代理后面，
        从 Host 头推断会被代理改写成内部域名，导致邮件里的链接打不开。
        未配置时兜底用 api.lurvy.top（当前唯一部署环境）。
        """
        return (os.environ.get("PUBLIC_BASE_URL", "").strip()
                or "https://api.lurvy.top").rstrip("/")

    def _forgot_password(self, body: dict) -> dict:
        """发送重置邮件。

        **无论邮箱是否存在都返回同样的成功响应。**
        否则这个端点就成了一个「该邮箱有没有注册过」的枚举器 ——
        对情感陪伴类产品，知道某人在用这个 App 本身就是隐私。
        """
        email = auth.normalize_email(body.get("email", ""))
        if not auth.looks_like_email(email):
            return err("INVALID_EMAIL")

        store = Store(connect(init_schema=False))
        user = store.user_by_email(email)
        if user:
            token = store.create_password_reset(user["id"], ttl_seconds=3600)
            link = f"{self._reset_base_url()}/?reset={token}"
            status = mailer.send_password_reset(email, link, minutes=60)
            if status != "sent":
                # 发不出去不能装作发了 —— 用户会一直等一封永远不会来的邮件。
                # 但这属于运维问题，日志里记清楚，客户端仍返回中性文案。
                print(f"  ⚠️  重置邮件未送达 ({status}): user={user['id']}")
        return ok({"sent": True})

    def _reset_password(self, body: dict) -> dict:
        """用重置令牌设置新密码。

        成功后吊销全部令牌：重置密码的常见场景就是「号可能被别人用了」。
        """
        token = (body.get("token") or "").strip()
        newpw = body.get("newPassword") or ""
        if len(newpw) < 8 or len(newpw) > 200:
            return err("WEAK_PASSWORD")
        store = Store(connect(init_schema=False))
        uid = store.consume_password_reset(token)
        if not uid:
            # 过期 / 已用 / 伪造 统一同一个错误，不区分
            return err("INVALID_RESET_TOKEN")
        store.set_password(uid, auth.hash_password(newpw))
        store.revoke_all_tokens(uid)
        return ok(None)

    def _logout(self) -> dict:
        token = self._bearer_token()
        if token:
            Store(connect(init_schema=False)).revoke_token(token)
        return ok(None)

    def _me(self) -> dict:
        store = Store(connect(init_schema=False))
        uid = store.user_id_by_token(self._bearer_token())
        if not uid:
            return err("UNAUTHORIZED")
        store.user_id = uid
        return ok(self._public_user(store.ensure_user()))

    # ---------- 工具
    def _json(self, payload: dict, code: int = 200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # CORS：APK 内嵌前端时走 file:// 协议，跨域请求需要放行
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Access-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if MAX_BODY_BYTES > 0 and n > MAX_BODY_BYTES:
            # 兜底：do_POST 入口已经挡过一次，这里是防其它调用路径绕过。
            # 不读 body —— 读进来正是要避免的事。
            self.close_connection = True
            raise _PayloadTooLarge(n)
        try:
            return json.loads(self.rfile.read(n).decode())
        except _PayloadTooLarge:
            raise
        except Exception:
            return {}

    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC)) or not f.is_file():
            self._json(err("not found", 404), 404)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".apk": "application/vnd.android.package-archive",
                 ".mp3": "audio/mpeg",
                 ".jpeg": "image/jpeg",
                 ".jpg": "image/jpeg",
                 ".png": "image/png"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        # 开发阶段对 html/js/css 禁用缓存：
        # 否则手机浏览器会缓存旧页面，导致“改了没生效”的假象。
        # 图片 / APK 不设，避免每次重下 2MB。
        if f.suffix in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        """部分浏览器 / 下载器会先发 HEAD 探测（看大小与类型）。
        直接复用 GET 的逻辑，由 _json/_static 判断 command 决定是否写 body。"""
        self.do_GET()

    def do_OPTIONS(self):
        """CORS 预检请求（APK 内嵌前端的跨域调用会先发 OPTIONS）"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Access-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ---------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)
        if p.startswith("/api/"):
            if not rate_allow(self.client_address[0]):
                return self._json({"flag": 1, "msg": "RATE_LIMITED"}, 429)
            if not self._authorized():
                return self._json({"flag": 1, "msg": "UNAUTHORIZED"}, 401)
            api = self._api()
            # 除认证端点外都要求登录 —— 否则未登录设备会全部落到
            # 同一个匿名用户上，多用户隔离就形同虚设。
            if not p.startswith("/api/auth/") and not api.s.user_id:
                return self._json(err("UNAUTHORIZED"), 401)
        else:
            api = None
        try:
            if p == "/api/auth/me":             return self._json(self._me())
            if p == "/api/user/settings":       return self._json(api.user_settings())
            if p == "/api/user/roles/overview": return self._json(api.roles_overview())
            if p == "/api/role/mine":           return self._json(api.role_mine())
            if p == "/api/bootstrap":          return self._json(api.bootstrap())
            if p == "/api/role/list":           return self._json(api.role_list())
            # 通配路由必须限定为数字 ID。
            # 若只写 startswith("/api/role/")，/api/role/create 与
            # /api/role/update 会被它吃掉，int("create") 直接抛 500 ——
            # 结果就是「创建角色」功能整条不可用。
            if p.startswith("/api/role/"):
                _tail = p.rsplit("/", 1)[1]
                if _tail.isdigit():
                    return self._json(api.role_detail(int(_tail)))
            if p == "/api/chatMode":            return self._json(api.chat_modes())
            if p == "/api/config":              return self._json(api.config())
            if p == "/api/heart/plans":         return self._json(api.iap_plans())
            if p == "/api/hidden-chapter/list":
                return self._json(api.chapter_list(int(qs.get("roleId", [0])[0])))
            if p == "/api/affinity":
                return self._json(api.affinity_get(int(qs.get("roleId", [0])[0])))
            if p == "/api/affinity/overview":
                return self._json(api.affinity_overview())
            if p == "/api/scenario/list":
                return self._json(api.scenario_list(
                    q=qs.get("q", [""])[0], category=qs.get("category", [""])[0],
                    location=qs.get("location", [""])[0],
                    scope=qs.get("scope", ["all"])[0]))
            if p == "/api/scenario/meta":       return self._json(api.scenario_meta())
            if p == "/api/scenario/detail":
                return self._json(api.scenario_detail(int(qs.get("scenarioId", [0])[0])))
            if p == "/api/creator/get":
                return self._json(api.creator_get(int(qs.get("userId", [1])[0])))
            if p == "/api/following/list":      return self._json(api.social_lists())
            if p == "/api/creator/block/list":  return self._json(api.block_list())
            if p == "/api/user/roles/overview": return self._json(api.roles_overview())
            if p == "/api/ugc/style":            return self._json(api.ugc_styles())
            if p == "/api/album/list":
                return self._json(api.album_list(qs.get("source", [""])[0]))
            if p == "/api/quest/dashboard":    return self._json(api.quest_dashboard())
            if p == "/api/quest/list":
                return self._json(api.quest_list(qs.get("type", ["daily"])[0]))
            if p == "/api/quest/checkin-status":
                return self._json(api.checkin_status())
            if p == "/api/achievement":        return self._json(api.achievements())
            if p == "/api/notification/list":  return self._json(api.notifications())
            if p == "/api/notification/has-unread":
                return self._json(ok({"hasUnread": api.notifications()["data"]["hasUnread"]}))
            if p == "/api/sticker/category-list": return self._json(api.sticker_list())
            if p == "/api/tts/vocal/list":     return self._json(api.tts_vocals())
            if p == "/api/chatroom/list":       return self._json(api.chatroom_list())
            if p == "/api/message/list":        return self._json(api.message_list(int(qs.get("chatroomId", [0])[0])))
            if p == "/api/heart/list":          return self._json(api.heart_list())
            if p == "/api/heart/history":       return self._json(api.heart_history())
            if p == "/api/ad/times":            return self._json(api.ad_times())
            if p == "/api/memory/list":         return self._json(api.memory_list(int(qs.get("chatroomId", [0])[0])))
            if p == "/api/moment/list":         return self._json(api.moment_list())
            if p == "/api/moment/saved-list":   return self._json(api.moment_saved_list())
            if p == "/api/moment/tag-role-candidates": return self._json(api.moment_tag_candidates())
            if p == "/api/moment/comment/list":
                return self._json(api.moment_comments(int(qs.get("momentId", [0])[0])))
            # 举报：查询类走 GET，提交与处置走 POST
            if p == "/api/report/reasons":     return self._json(api.report_reasons())
            if p == "/api/report/queue":       return self._json(api.report_queue())
            if p == "/api/contact/list":       return self._json(api.lead_list())
        except Exception as e:
            return self._json(err(f"server error: {e}", 500), 500)
        if p.startswith("/generated/") or p.startswith("/speech/"):
            return self._static(p)
        self._static(p)

    # ---------- POST
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        # 体积闸门 —— 放在限流和鉴权之前：读一个 header 就完成判断，
        # 比拒绝一个已经读完 5MB 再解析的请求便宜得多。
        #
        # 声明长度超限时：直接回 413 且不读 body，然后关连接。
        # 关连接是必须的 —— 否则残留的 body 会被当成下一个请求解析。
        _cl = int(self.headers.get("Content-Length") or 0)
        if MAX_BODY_BYTES > 0 and _cl > MAX_BODY_BYTES:
            self.close_connection = True
            return self._json(
                err(f"PAYLOAD_TOO_LARGE: {_cl} bytes exceeds {MAX_BODY_BYTES}", 413), 413)
        if p.startswith("/api/"):
            if not rate_allow(self.client_address[0]):
                return self._json({"flag": 1, "msg": "RATE_LIMITED"}, 429)
            # /api/contact 免 ACCESS_KEY：官网是公开页面，把密钥写进前端 JS
            # 等于公开密钥。这个端点靠 IP 限流 + 字段校验防滥用，
            # 且只能写入 leads 表，读不到任何用户数据。
            if p != "/api/contact" and not self._authorized():
                return self._json({"flag": 1, "msg": "UNAUTHORIZED"}, 401)
        api = self._api()
        # 官网联系表单必须免登录 —— 填表的人不是 App 用户，没有账号。
        # 仅有 /api/contact 这一个例外，其余 /api/ 一律要求登录。
        _PUBLIC_POST = {"/api/contact"}
        if (p.startswith("/api/") and not p.startswith("/api/auth/")
                and p not in _PUBLIC_POST and not api.s.user_id):
            return self._json(err("UNAUTHORIZED"), 401)
        try:
            if p == "/api/auth/register":
                return self._json(self._register(self._body()))
            if p == "/api/auth/login":
                return self._json(self._login(self._body()))
            if p == "/api/auth/logout":
                return self._json(self._logout())
            if p == "/api/auth/delete":
                return self._json(self._delete_account())
            if p == "/api/auth/password":
                return self._json(self._change_password(self._body()))
            # 找回密码：两个端点都必须在未登录时可用
            if p == "/api/auth/forgot":
                return self._json(self._forgot_password(self._body()))
            if p == "/api/auth/reset":
                return self._json(self._reset_password(self._body()))
            if p == "/api/user/settings":
                return self._json(api.user_settings_update(self._body()))
            if p == "/api/chatroom":
                return self._json(api.chatroom_create(self._body()))
            if p == "/api/chatroom/mode":
                return self._json(api.chatroom_mode(self._body()))
            if p == "/api/ad/reward":
                return self._json(api.ad_reward(self._body()))
            if p == "/api/memory/summarize":
                return self._json(api.summarize(int(self._body().get("chatroomId", 0))))
            if p == "/api/heart/purchase":
                return self._json(api.iap_purchase(self._body()))
            if p == "/api/hidden-chapter/batch":
                return self._json(api.chapter_batch(self._body()))
            if p == "/api/hidden-chapter/enter":
                return self._json(api.chapter_enter(self._body()))
            if p == "/api/scenario/create":
                return self._json(api.scenario_create(self._body()))
            if p == "/api/scenario/update":
                return self._json(api.scenario_update(self._body()))
            if p == "/api/scenario/delete":
                return self._json(api.scenario_delete(self._body()))
            if p == "/api/scenario/like":
                return self._json(api.scenario_like(self._body(), True))
            if p == "/api/scenario/unlike":
                return self._json(api.scenario_like(self._body(), False))
            if p == "/api/scenario/batch-liked":
                return self._json(api.scenario_batch_liked(self._body()))
            if p == "/api/chatroom/listScenario":
                return self._json(api.scenario_start(self._body()))
            if p == "/api/creator/update":
                return self._json(api.creator_update(self._body()))
            if p == "/api/following/request":
                return self._json(api.follow_action(self._body(), "request"))
            if p == "/api/following/delete":
                return self._json(api.follow_action(self._body(), "delete"))
            if p == "/api/follower/delete":
                return self._json(api.follower_action(self._body(), "delete"))
            if p == "/api/follower/accept":
                return self._json(api.follower_action(self._body(), "accept"))
            if p == "/api/follower/markAsRead":
                return self._json(api.mark_follow_read())
            if p == "/api/creator/block/update":
                b = self._body()          # 注意：只能读一次，rfile 读完就空
                return self._json(api.block_action(b, b.get("blocked", True)))
            if p == "/api/contact":
                return self._json(api.lead_create(self._body()))
            if p == "/api/report/create":
                return self._json(api.report_create(self._body()))
            if p == "/api/report/resolve":
                return self._json(api.report_resolve(self._body()))
            if p == "/api/role/create":
                return self._json(api.role_create(self._body()))
            if p == "/api/role/update":
                return self._json(api.role_update(self._body()))
            if p == "/api/ugc/generate":
                return self._json(api.ugc_generate(self._body()))
            if p == "/api/album/generate":
                return self._json(api.album_generate(self._body()))
            if p == "/api/album/mark-read":
                return self._json(api.album_mark_read())
            if p == "/api/quest/claim":
                return self._json(api.quest_claim(self._body()))
            if p == "/api/quest/checkin":
                return self._json(api.daily_checkin())
            if p == "/api/notification/mark-read":
                return self._json(api.notify_mark(self._body()))
            if p == "/api/notification/mark-roles-as-read":
                return self._json(api.notify_mark({"kind": "role"}))
            if p == "/api/notification/mark-posts-as-read":
                return self._json(api.notify_mark({"kind": "post"}))
            if p == "/api/notification/mark-moment-posts-as-read":
                return self._json(api.notify_mark({"kind": "moment"}))
            if p == "/api/notification/mark-moment-comments-as-read":
                return self._json(api.notify_mark({"kind": "comment"}))
            if p == "/api/message/speech/generate":
                return self._json(api.speech_generate(self._body()))
            if p == "/api/moment/create":
                return self._json(api.moment_create(self._body()))
            if p == "/api/moment/like":
                return self._json(api.moment_like(self._body(), True))
            if p == "/api/moment/unlike":
                return self._json(api.moment_like(self._body(), False))
            if p == "/api/moment/save":
                return self._json(api.moment_save(self._body(), True))
            if p == "/api/moment/unsave":
                return self._json(api.moment_save(self._body(), False))
            if p == "/api/moment/delete":
                return self._json(api.moment_delete(self._body()))
            if p == "/api/chatroom/delete":
                return self._json(api.chatroom_delete(self._body()))
            if p == "/api/memory/delete":
                return self._json(api.memory_delete(self._body()))
            if p == "/api/role/delete":
                return self._json(api.role_delete(self._body()))
            if p == "/api/moment/comment/create":
                return self._json(api.moment_comment_create(self._body()))
            if p == "/api/message/send":
                return self._stream_send(api)
        except _PayloadTooLarge as e:
            self.close_connection = True
            return self._json(
                err(f"PAYLOAD_TOO_LARGE: {e.size} bytes exceeds {MAX_BODY_BYTES}", 413), 413)
        except Exception as e:
            return self._json(err(f"server error: {e}", 500), 500)
        self._json(err("not found", 404), 404)

    # ---------- 流式发送（SSE）
    def _stream_send(self, api: API):
        store = api.s
        body = self._body()

        # 内容安全 —— 必须在扣费之前。
        # 若放在 prepare_send 之后，被拦截的消息已经把 hearts 扣了，
        # 用户会因为一次拒绝而被白扣费。
        _text = (body.get("text") or "").strip()
        _blocked = api._screen(_text, field="message")
        if _blocked:
            return self._json(_blocked)

        chatroom, cost, ad_funded, e = api.prepare_send(body)
        if e:
            return self._json(e)

        text = _text
        cid = chatroom["id"]
        store.add_message(cid, "user", text, cost, ad_funded)
        store.touch_chatroom(cid)
        api.track("chat")          # 推进日常任务

        role = store.get_role(chatroom["role_id"])
        u = store.ensure_user()
        persona = {"persona_name": u["persona_name"], "persona_desc": u["persona_desc"]}
        memory = store.memory_block(cid)
        history = store.history_for_llm(cid)

        # 评估 Affinity 变化（驱动隐藏章节解锁）
        aff_delta, aff_reason = affinity.evaluate(text)
        aff_new = (store.add_affinity(u["id"], chatroom["role_id"],
                                      aff_delta, aff_reason)
                   if aff_delta else store.get_affinity(u["id"], chatroom["role_id"]))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        emit({"event": "meta", "cost": cost, "isAdFunded": bool(ad_funded),
              "chatMode": chatroom["chat_mode"],
              "heartInfo": store.heart_info(u["id"]),
              "affinity": {"value": aff_new, "delta": aff_delta,
                           "reason": aff_reason,
                           "label": affinity.describe(aff_new)}})

        acc = []
        blocked = False
        # 用量先攒在一个可变量里，流结束后统一落库。
        # on_usage 在生成器的 finally 里调用，即使下面 break 也会拿到值。
        _usage: dict = {}

        def _on_usage(rec: dict) -> None:
            _usage.update(rec)

        try:
            for piece in llm.chat_stream(role, persona, memory, history,
                                         chatroom["chat_mode"], on_usage=_on_usage):
                acc.append(piece)
                # 出站审核 —— 必须在推送前检查。
                # 只拦用户输入不够：Play 生成式 AI 政策要求对**模型输出**也做过滤，
                # 否则模型被诱导产出的违规内容会直接推到用户屏幕上。
                # 零滞后：每个 token 都先审后推，命中则中断且不落库。
                if api._screen("".join(acc), field="assistant"):
                    blocked = True
                    break
                emit({"event": "token", "text": piece})
        except Exception as ex:
            # 不把上游的原始异常文本发给客户端 —— 那些是英文技术信息
            # （HTTP Error 500 / Connection reset），直接展示给用户毫无帮助，
            # 反而像应用坏了。记在服务端日志，用户拿到友好的中文提示
            # + 退款（见下面的 NO_OUTPUT 分支）。
            print(f"⚠️  模型调用失败 cid={cid}: {type(ex).__name__}: {ex}", file=sys.stderr)

        if blocked:
            # 被拦下的那次调用上游已经花完了 token —— 不计入就是把
            # 最该看见的成本漏掉。blocked=1 单独标出，便于观察拦截率。
            #
            # 注意：拦截发生在流中段，usage 帧通常在流的最后一帧，
            # 所以这里几乎必然走估算分支（estimated=1），且 output_tokens
            # 只计了已收到的部分 —— **这类记录是成本下界，不是准确值**。
            # 不为记账把违规内容读完：内容已经违规，继续解析只会扩大暴露。
            if _usage:
                store.record_usage(u["id"], cid, chatroom["chat_mode"],
                                   _usage, blocked=True)
            # 退钱：用户没有拿到任何可用回复，不该为一次被拦截的生成付费。
            # 上游成本是我们的，不是用户的。
            if cost and not ad_funded:
                store.add_hearts(u["id"], cost, "chat_refund_blocked")
            # 不落库：违规内容一旦进 messages 表，就从「过滤」变成了「持有」。
            emit({"event": "error", "code": "CONTENT_BLOCKED",
                  "msg": "该回复未通过内容安全检查，已中止。"})
            emit({"event": "done", "length": 0, "blocked": True,
                  "refunded": cost if (cost and not ad_funded) else 0,
                  "heartInfo": store.heart_info(u["id"])})
            return

        full = "".join(acc)
        mid = None
        if full:
            mid = store.add_message(cid, "assistant", full)
        if _usage:
            store.record_usage(u["id"], cid, chatroom["chat_mode"], _usage)

        # 一条内容都没产出（上游 500 / 连接中断 / 提前 closed）→ 退 hearts。
        # 旧版本只发一句 error 事件就结束，用户付了钱、界面上留一条空回复，
        # 刷新后连那条空回复也没了 —— 完全不知道发生了什么。
        refunded = 0
        if not full and cost and not ad_funded:
            store.add_hearts(u["id"], cost, "chat_refund_empty")
            refunded = cost
            emit({"event": "error", "code": "NO_OUTPUT",
                  "msg": "没有收到回复，hearts 已退还，请重试。"})

        # messageId 回传前端：AI 回复必须可在应用内举报（同一政策要求）
        emit({"event": "done", "length": len(full), "messageId": mid,
              "refunded": refunded, "heartInfo": store.heart_info(u["id"])})


def local_addresses(port: int) -> list[str]:
    """列出本机所有可用访问地址（便于手机/同事访问或打包时填入）。"""
    import socket
    addrs = [f"http://127.0.0.1:{port}"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            a = f"http://{ip}:{port}"
            if a not in addrs:
                addrs.append(a)
    except Exception:
        pass
    # 退路：UDP 探测默认出口 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        a = f"http://{ip}:{port}"
        if a not in addrs:
            addrs.append(a)
    except Exception:
        pass
    return addrs


def _background_maintenance(interval_sec: int = 3600) -> None:
    """后台维护：WAL checkpoint + 清理过期令牌。

    为什么要 checkpoint：SQLite 的 WAL 模式下，写入先进 -wal 文件，
    只有 checkpoint 才合并回主库。长期运行的进程若不主动触发，
    -wal 会持续增长 —— 实测生产库主文件 356 KB，而 -wal 涨到 4.1 MB。
    这不会丢数据（读取会自动合并），但会拖慢查询，也让「直接拷 .db」
    这种备份方式拿到过期快照（今天就踩到了）。

    为什么要清令牌：过期的 auth_tokens 只在被访问到时才删除，
    长期不登录的账号会留下垃圾行。

    维护失败一律吞掉 —— 它不该影响正常服务。
    """
    while True:
        time.sleep(interval_sec)
        try:
            conn = connect(init_schema=False)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            Store(connect(init_schema=False)).prune_expired_tokens()
        except Exception:
            pass


def main():
    conn = connect()
    store = Store(conn)
    bootstrap(store)

    mode = "真实模型" if llm.api_key() else "mock（未设置 MINIMAX_API_KEY）"
    print("=" * 62)
    print("  Vesperine 复刻 —— 测试环境已启动")
    print("=" * 62)
    print(f"  模型: {mode}")
    print(f"  广告: {AD_CONFIG['mode']}   支付: {PAYMENT_MODE}")
    print(f"  角色数: {len(store.list_roles())}")
    print("-" * 62)
    print("  可用访问地址：")
    for a in local_addresses(PORT):
        tag = "← 手机/同事访问用这个" if "127.0.0.1" not in a else "← 本机"
        print(f"    {a:34s} {tag}")
    print("-" * 62)
    print("  打包/嵌入提示：若前端不在同源运行，用")
    print("    <页面地址>?api=http://<IP>:8080   或注入 window.__VESPERINE_API__")
    if HOST == "0.0.0.0":
        print("  ⚠️  已监听 0.0.0.0 —— 请勿直接把此端口暴露到公网")
    print("=" * 62)
    # 后台维护线程（daemon，不阻塞退出）
    threading.Thread(target=_background_maintenance, daemon=True).start()

    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
