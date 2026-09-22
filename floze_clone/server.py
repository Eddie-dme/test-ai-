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
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import llm
import affinity
import moderation
import iap_verify
import imagegen
import tts
import voices as voice_map
from seed_data import (ROLES, CHAPTERS, QUESTS, ACHIEVEMENTS, STICKERS,
                       ACHIEVEMENT_STICKERS,
                       SCENARIOS, MOMENTS, ALBUMS)
from store import Store, connect, ok, err, now

STATIC = Path(__file__).resolve().parent / "static"
GENERATED_DIR = STATIC / "generated"      # 生成的图片落盘目录（静态可访问）
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
    for q in QUESTS:
        store.c.execute(
            "INSERT OR IGNORE INTO quests (type, code, title, description, target,"
            " reward_hearts, event_key) VALUES (?,?,?,?,?,?,?)",
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

    def __init__(self, store: Store):
        self.s = store

    # ---- user / persona
    def user_settings(self) -> dict:
        u = self.s.ensure_user()
        return ok({"user": {"id": u["id"], "nickname": u["nickname"],
                            "avatar": u["avatar"]},
                   "persona": {"name": u["persona_name"], "desc": u["persona_desc"]},
                   "heartInfo": self.s.heart_info(u["id"])})

    # ---- role
    def role_list(self) -> dict:
        return ok(self.s.list_roles())

    def role_detail(self, role_id: int) -> dict:
        r = self.s.get_role(role_id)
        return ok(r) if r else err("role not found")

    # ---- chatMode（对齐 /chatMode，返回档位列表）
    def chat_modes(self) -> dict:
        items = []
        for mid, cfg in llm.CHAT_MODES.items():
            items.append({"id": mid, "model": cfg["model"],
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
        u = self.s.ensure_user()
        role_id = int(body.get("roleId", 0))
        if not self.s.get_role(role_id):
            return err("role not found")
        mode = body.get("chatMode", "classic")
        return ok(self.s.create_chatroom(u["id"], role_id, mode,
                                         body.get("model", "standard")))

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
        """广告奖励：CHAT → 该会话下一条消息免费；DAILY_HEART → 直接给 hearts。"""
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
    REPORT_TARGETS = {"role", "moment", "comment", "user", "album", "scenario"}

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
        except Exception:
            pass
        return ""

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
    IAP_PLANS = {f"top.lurvy.vesperine.heart.plan{i}": i * 5 + 5 for i in range(1, 13)}

    def iap_plans(self) -> dict:
        out = []
        for i, (pid, h) in enumerate(self.IAP_PLANS.items()):
            out.append({"id": pid, "hearts": h, "price": f"${2.99 + i * 8.0:.2f}"})
        return ok(out)

    def iap_purchase(self, body: dict) -> dict:
        u = self.s.ensure_user()
        plan_id = body.get("planId", "")
        if plan_id not in self.IAP_PLANS:
            return err("unknown plan")
        hearts = self.IAP_PLANS[plan_id]

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

    def _charge(self, u: dict, cost: int, reason: str):
        """免费额度 → hearts 的两级策略（对齐 Vesperine 的 free → heart 链）。
        返回 (ok, error_response)。
        """
        fq_type = "roleUgc"
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
            # 生成失败退还 hearts（不赚用户失败的钱）
            if cost:
                self.s.add_hearts(u["id"], cost, "ugc_refund")
            return err(f"生成失败: {ex}")

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
            if cost:
                self.s.add_hearts(u["id"], cost, "album_refund")
            return err(f"生成失败: {ex}")
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):     # 降噪
        pass

    def _api(self) -> API:
        """每个请求用独立 SQLite 连接。

        ThreadingHTTPServer 为每个请求开新线程，而 sqlite3 默认禁止跨线程
        复用连接（check_same_thread）。因此每请求新建连接。
        """
        return API(Store(connect(init_schema=False)))

    # ---------- 工具
    def _json(self, payload: dict, code: int = 200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # CORS：APK 内嵌前端时走 file:// 协议，跨域请求需要放行
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ---------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)
        if p.startswith("/api/"):
            api = self._api()
        else:
            api = None
        try:
            if p == "/api/user/settings":       return self._json(api.user_settings())
            if p == "/api/role/mine":           return self._json(api.role_mine())
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
        except Exception as e:
            return self._json(err(f"server error: {e}", 500), 500)
        if p.startswith("/generated/") or p.startswith("/speech/"):
            return self._static(p)
        self._static(p)

    # ---------- POST
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        api = self._api()
        try:
            if p == "/api/chatroom":
                return self._json(api.chatroom_create(self._body()))
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
            if p == "/api/moment/comment/create":
                return self._json(api.moment_comment_create(self._body()))
            if p == "/api/message/send":
                return self._stream_send(api)
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
        try:
            for piece in llm.chat_stream(role, persona, memory, history, chatroom["chat_mode"]):
                acc.append(piece)
                emit({"event": "token", "text": piece})
        except Exception as ex:
            emit({"event": "error", "msg": str(ex)[:200]})

        full = "".join(acc)
        if full:
            store.add_message(cid, "assistant", full)
        emit({"event": "done", "length": len(full)})


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
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
