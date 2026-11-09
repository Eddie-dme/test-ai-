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


def smtp_ready() -> bool:
    """SMTP 凭据是否齐全（能发任意邮件）。

    与 enabled() 分开：LEAD_TO 只是官网线索的收件人，
    密码重置邮件不需要它 —— 否则线上忘配 LEAD_TO 就会导致
    「找回密码」在明明配好了 SMTP 的情况下静默失效。
    """
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def enabled() -> bool:
    """官网线索通知是否可用（需要 SMTP 凭据 + 收件人）。"""
    return bool(smtp_ready() and LEAD_TO)


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


def _deliver(msg: EmailMessage) -> str:
    """实际投递一封邮件，返回 sent / skipped / failed。

    lead 通知与密码重置共用这一段 —— 两处各写一遍 SMTP 逻辑，
    迟早会有一处漏掉 STARTTLS 或超时设置。
    """
    if not smtp_ready():
        return "skipped"
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
        # 不抛出去 —— 调用方需要继续返回成功响应给用户。
        print(f"  ⚠️  邮件发送失败: {e}")
        return "failed"


def send_lead(lead: dict) -> str:
    """发送一条线索通知。返回 sent / skipped / failed。"""
    if not enabled():
        return "skipped"
    return _deliver(_build_lead_message(lead))


RESET_FROM_NAME = os.environ.get("RESET_FROM_NAME", "Vesperine")


def _build_reset_message(to_email: str, link: str, minutes: int) -> EmailMessage:
    """密码重置邮件。

    刻意不透露「这个邮箱有没有注册过」以外的信息，也不带用户昵称 ——
    重置邮件有可能进错邮箱（用户打错字），里面不该出现任何可用信息。
    """
    msg = EmailMessage()
    msg["Subject"] = "Reset your Vesperine password"
    msg["From"] = formataddr((RESET_FROM_NAME, SMTP_USER))
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(
        "Someone asked to reset the password for this Vesperine account.\n\n"
        f"Open this link to choose a new password (valid for {minutes} minutes):\n\n"
        f"{link}\n\n"
        "The link can only be used once.\n\n"
        "If this was not you, you can ignore this email -- your password\n"
        "will not change.\n"
    )
    return msg


def send_password_reset(to_email: str, link: str, minutes: int = 60) -> str:
    """发送重置邮件。返回 sent / skipped / failed。

    注意：调用方**不应**把返回值直接告诉客户端 —— 那会变成
    「这个邮箱存在吗」的探测接口。调用方统一回「如果该邮箱已注册，我们发了邮件」。
    """
    if not smtp_ready():
        return "skipped"
    return _deliver(_build_reset_message(to_email, link, minutes))


if __name__ == "__main__":
    # 自检：不真发，只看配置是否完整
    print("SMTP 已配置:", enabled())
    print("  收件人:", LEAD_TO or "(未设置)")
    print("  服务器:", SMTP_HOST or "(未设置)", f":{SMTP_PORT}")
