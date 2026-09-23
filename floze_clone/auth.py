"""密码哈希与访问令牌 —— 只用标准库，不引入依赖。

**为什么用 pbkdf2_hmac 而不是 scrypt**

scrypt 更抗 GPU 爆破，但它的可用性取决于 Python 构建时链接的 OpenSSL：
实测本机（Python 3.9.6 / LibreSSL 2.8.3）**没有** `hashlib.scrypt`，
而服务器（Python 3.9.25 / OpenSSL 3.5.8）有。两边不一致会导致
「服务器注册的账号在本地无法验证」，开发环境直接废掉。

因此统一用 `pbkdf2_hmac`：标准库普遍可用，行为两端一致。
迭代次数取 600k（OWASP 2023 对 PBKDF2-HMAC-SHA256 的推荐量级），
单次校验约几百毫秒 —— 登录可接受，爆破代价足够高。

存储格式：``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``
带算法与迭代数前缀，将来换算法或调参数也能识别旧格式、平滑迁移。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"
_DKLEN = 32

TOKEN_BYTES = 32                          # 令牌熵：256 位
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90     # 90 天


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    """把明文密码转成可入库的字符串。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, PBKDF2_ITERATIONS, dklen=_DKLEN)
    return "{}${}${}${}".format(_ALGO, PBKDF2_ITERATIONS, _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    """校验密码。格式损坏、算法未知、参数异常一律判失败，不抛异常。"""
    if not stored or not password:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        if iterations <= 0 or iterations > 10_000_000:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        if not salt or not expected:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, iterations, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def new_token() -> str:
    """生成访问令牌。

    不用 JWT：服务端有数据库，随机串更简单、可即时吊销，
    也不需要在令牌里塞可被读取的声明。
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def normalize_email(email: str) -> str:
    """归一化：去空白 + 转小写。避免 Foo@x.com 与 foo@x.com 注册成两个账号。"""
    return (email or "").strip().lower()


def looks_like_email(email: str) -> bool:
    """极简校验，只挡住明显不是邮箱的输入。

    完整 RFC 校验既复杂又拦不住真正无效的地址 —— 真正的验证手段是
    发确认邮件（当前未实现）。这里只做形态检查，避免垃圾数据入库。
    """
    if not email or len(email) > 254 or " " in email:
        return False
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain:
        return False
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    if "@" in domain:
        return False
    return True
