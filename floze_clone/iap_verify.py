"""
Vesperine —— Google Play 订单校验

为什么必须做：客户端上报「我买成功了」完全不可信 —— 改一个请求就能白拿 hearts。
唯一可靠的做法是服务端拿 purchaseToken 去 Google 核对。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
零依赖实现
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Google 的 API 要求 OAuth2 Bearer Token，而换取 token 需要 RS256 签名
（用服务账号私钥）。签名通常靠 `cryptography` 或 `PyJWT`，但本项目
坚持零 Python 依赖，所以：

  - JWT 签名 → 调用系统 openssl（`openssl dgst -sha256 -sign`）
  - HTTP     → 标准库 urllib
  - 缓存     → 内存字典（access_token 有效期 1 小时）

代价是每次签发 token 要 fork 一个 openssl 进程（毫秒级，且 token 会缓存
一小时，实际开销可忽略）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
接入步骤（拿到 Play 账号后）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Google Cloud Console → 创建服务账号 → 下载 JSON 密钥
2. Play Console → 用户和权限 → 邀请该服务账号邮箱
   → 授予「查看财务数据」+「管理订单和订阅」
   （只给「查看」不够，消耗型商品需要 consume 权限）
3. 把 JSON 放到服务器，设置环境变量：
       export GOOGLE_PLAY_SA_JSON=/path/to/service-account.json
4. 把 PAYMENT_MODE 设为 live
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"

# access_token 缓存：{service_account_email: (token, expires_at)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# 提前 5 分钟续期，避免边界过期
_SKEW = 300


# ────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def service_account() -> dict | None:
    """读取服务账号 JSON。未配置返回 None（调用方据此判定 live 是否可用）。"""
    p = os.environ.get("GOOGLE_PLAY_SA_JSON", "").strip()
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def package_name() -> str:
    return os.environ.get("GOOGLE_PLAY_PACKAGE", "top.lurvy.vesperine")


# ────────────────────────────────────────────────────────────
# JWT（RS256，借 openssl 签名）
# ────────────────────────────────────────────────────────────

def _sign_rs256(message: bytes, private_key_pem: str) -> bytes:
    """
    用 openssl 对 message 做 RS256 签名。

    私钥通过临时文件传递 —— openssl 的 `-sign` 需要文件路径，
    而 `-inkey /dev/stdin` 在部分平台上不可靠。
    临时文件用 0600 权限创建，用完立即删除。
    """
    fd, path = tempfile.mkstemp(prefix="vsp_key_", suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=message, capture_output=True, timeout=20)
        if proc.returncode != 0:
            raise RuntimeError(
                f"openssl 签名失败: {proc.stderr.decode(errors='replace')[:200]}")
        return proc.stdout
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _make_jwt(sa: dict) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/androidpublisher",
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode()
    sig = _sign_rs256(signing_input, sa["private_key"])
    return signing_input.decode() + "." + _b64url(sig)


# ────────────────────────────────────────────────────────────
# OAuth2
# ────────────────────────────────────────────────────────────

def access_token(sa: dict | None = None) -> str:
    """换取（并缓存）access_token。"""
    sa = sa or service_account()
    if not sa:
        raise RuntimeError("未配置 GOOGLE_PLAY_SA_JSON")

    email = sa.get("client_email", "")
    hit = _TOKEN_CACHE.get(email)
    if hit and hit[1] - _SKEW > time.time():
        return hit[0]

    assertion = _make_jwt(sa)
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()

    req = urllib.request.Request(
        _TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"换取 access_token 失败 HTTP {e.code}: {detail}")

    tok = data.get("access_token")
    if not tok:
        raise RuntimeError(f"响应中没有 access_token: {data}")
    _TOKEN_CACHE[email] = (tok, time.time() + int(data.get("expires_in", 3600)))
    return tok


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────

def verify_purchase(product_id: str, purchase_token: str,
                    *, sa: dict | None = None) -> dict:
    """
    向 Google 核对一次购买。

    返回 {"ok": bool, "state": int|None, "consumed": bool|None, "raw": dict}
      purchaseState: 0=已购买 1=已取消 2=待处理
      consumptionState: 0=未消费 1=已消费
    """
    pkg = package_name()
    tok = access_token(sa)
    url = (f"{_API}/{urllib.parse.quote(pkg)}/purchases/products/"
           f"{urllib.parse.quote(product_id)}/tokens/"
           f"{urllib.parse.quote(purchase_token)}")

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return {"ok": False, "state": None, "consumed": None,
                "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"ok": False, "state": None, "consumed": None,
                "error": f"{type(e).__name__}: {e}"}

    state = raw.get("purchaseState")
    consumed = raw.get("consumptionState")
    return {
        "ok": state == 0 and consumed == 0,
        "state": state,
        "consumed": consumed,
        "raw": raw,
        "error": "" if state == 0 else f"purchaseState={state}",
    }


def consume_purchase(product_id: str, purchase_token: str,
                     *, sa: dict | None = None) -> bool:
    """
    标记消耗型商品已消费。

    不 consume 的话，用户买过一次就再也买不了同一档 ——
    这是消耗型 IAP 最常见的坑。
    """
    pkg = package_name()
    tok = access_token(sa)
    url = (f"{_API}/{urllib.parse.quote(pkg)}/purchases/products/"
           f"{urllib.parse.quote(product_id)}/tokens/"
           f"{urllib.parse.quote(purchase_token)}:consume")
    req = urllib.request.Request(url, data=b"", method="POST",
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"  ⚠️  consume 失败: {type(e).__name__}: {e}")
        return False


# ────────────────────────────────────────────────────────────
# 自检
# ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("  openssl 可用:", bool(subprocess.run(
        ["which", "openssl"], capture_output=True).stdout.strip()))
    print("  服务账号配置:", "已配置" if service_account() else "未配置（正常，等凭证）")
    print("  包名:", package_name())

    # 生成一次性 RSA 私钥，验证签名链路本身能跑通
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
        key_path = kf.name
    subprocess.run(["openssl", "genrsa", "-out", key_path, "2048"],
                   capture_output=True, timeout=30)
    pem = open(key_path).read()
    os.unlink(key_path)

    jwt = _make_jwt({"client_email": "test@example.iam.gserviceaccount.com",
                     "private_key": pem})
    parts = jwt.split(".")
    print(f"  JWT 段数: {len(parts)}（应为 3）")
    print(f"  签名长度: {len(base64.urlsafe_b64decode(parts[2] + '=='))} 字节（RSA2048 应为 256）")
    print(f"  ✅ 签名链路可用" if len(parts) == 3 else "  ❌ 签名链路异常")
