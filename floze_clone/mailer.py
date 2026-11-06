"""
Vesperine —— 官网线索邮件通知

职责：把 lurvy.top 的联系表单内容发到指定的收件箱。

设计原则
────────
1. **零第三方依赖** —— 只用 smtplib / email 标准库，与整个项目保持一致。
2. **失败不阻塞** —— 邮件发不出去时，线索已经落库了，不能因为 SMTP 挂了
   就把用户的咨询丢掉。调用方按 mail_status 记录结果。
3. **凭证只从环境变量读** —— 不写进代码、不写进仓库。
4. **未配置即静默跳过** —— 本地开发 / 未配 SMTP 时返回 skipped 而不是报错。

配置（/etc/vesperine.env）
──────────────────────────
    SMTP_HOST=smtp.feishu.cn        # 飞书企业邮箱
    SMTP_PORT=465                   # 465=SSL, 587=STARTTLS
    SMTP_USER=eddie@lurvy.top
    SMTP_PASS=<授权码，不是登录密码>
    LEAD_TO=eddie@lurvy.top         # 收件人，可多个，逗号分隔

    # 未配置 SMTP_HOST 或 SMTP_USER 时视为未启用，发邮件步骤跳过。
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
LEAD_TO = [x.strip() for x in os.environ.get("LEAD_TO", "").split(",") if x.strip()]
LEAD_FROM_NAME = os.environ.get("LEAD_FROM_NAME", "lurvy.top 官网")

TIMEOUT = 12   # 秒。SMTP 卡住时不能把 HTTP 请求一起拖死


def enabled() -> bool:
    """SMTP 是否配置完整。"""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS and LEAD_TO)


def _build_lead_message(lead: dict) -> EmailMessage:
    """把一条线索拼成邮件。纯文本 + 明确的字段标签，方便手机上看。"""
    msg = EmailMessage()
    name = lead.get("name") or "(未填)"
    company = lead.get("company") or "(未填)"
    msg["Subject"] = f"[官网咨询] {company} — {name}"
    msg["From"] = formataddr((LEAD_FROM_NAME, SMTP_USER))
    msg["To"] = ", ".join(LEAD_TO)
    msg["Date"] = formatdate(localtime=True)

    # 收件人直接点「回复」就能回给填表人（如果是邮箱的话）
    contact = (lead.get("contact") or "").strip()
    if "@" in contact:
        msg["Reply-To"] = contact

    lines = [
        "官网收到一条新咨询",
        "=" * 40,
        "",
        f"姓名/称呼：{name}",
        f"公司：     {company}",
        f"联系方式： {contact}",
        "",
        "需求描述",
        "-" * 40,
        (lead.get("need") or "(未填)"),
        "",
        "-" * 40,
        f"提交时间：{formatdate(localtime=True)}",
        f"来源 IP： {lead.get('ip') or '-'}",
        f"线索 ID： #{lead.get('id', '-')}",
    ]
    msg.set_content("\n".join(lines))
    return msg


def send_lead(lead: dict) -> str:
    """发送一条线索通知。

    返回 mail_status 字符串：
      sent    —— 发送成功
      skipped —— 未配置 SMTP（不算失败）
      failed  —— 配置了但发送失败（线索仍已落库）
    """
    if not enabled():
        return "skipped"

    msg = _build_lead_message(lead)
    try:
        if SMTP_PORT == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                  timeout=TIMEOUT, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return "sent"
    except Exception as e:
        # 不抛出去 —— 调用方需要继续返回成功响应给用户，
        # 线索已经落库，运维可以从库里捞。
        print(f"  ⚠️  线索邮件发送失败 lead={lead.get('id')}: {e}")
        return "failed"


if __name__ == "__main__":
    # 自检：不真发，只看配置是否完整
    print("SMTP 已配置:", enabled())
    print("  收件人:", LEAD_TO or "(未设置)")
    print("  服务器:", SMTP_HOST or "(未设置)", f":{SMTP_PORT}")
