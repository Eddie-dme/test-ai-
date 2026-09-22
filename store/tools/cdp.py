#!/usr/bin/env python3
"""用 CDP 精确控制 headless Chrome 截图。

用法: cdp.py <tab> <out.png> <像素宽> <像素高> [css宽=500] [额外等待ms=4000]

--window-size 会被 Chrome 的 500px 最小窗口宽度钳制，无法得到 360 CSS 视口；
CDP 的 Emulation.setDeviceMetricsOverride 没有这个限制。
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# 设 SHOT_URL 可渲染本地模板（如 file:///tmp/store/market.html）
BASE = os.environ.get("SHOT_URL", "http://127.0.0.1:8080")
PORT = 9333


class WS:
    """最小 WebSocket 客户端（客户端帧需 mask，RFC 6455）。"""

    def __init__(self, url):
        assert url.startswith("ws://"), url
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("WebSocket 握手失败")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise EOFError("WebSocket 握手被拒: " + buf.split(b"\r\n")[0].decode())
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.mid = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError("连接关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        b0, b1 = self._read(2)
        op = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if b1 & 0x80 else None
        payload = self._read(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if op == 0x8:
            raise EOFError("服务端关闭连接")
        if op in (0x9, 0xA):
            return None
        return payload.decode("utf-8", "replace")

    def call(self, method, params=None, timeout=30):
        self.mid += 1
        mid = self.mid
        self.send({"id": mid, "method": method, "params": params or {}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = self.recv()
            if msg is None:
                continue
            obj = json.loads(msg)
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError(f"{method} 失败: {obj['error']}")
                return obj.get("result", {})
        raise TimeoutError(f"{method} 超时")


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 1
    tab = sys.argv[1]
    out = sys.argv[2]
    px_w, px_h = int(sys.argv[3]), int(sys.argv[4])
    css_w = int(sys.argv[5]) if len(sys.argv) > 5 else 500
    settle_ms = int(sys.argv[6]) if len(sys.argv) > 6 else 4000

    dpr = px_w / css_w
    css_h = round(px_h / dpr)

    profile = f"/tmp/crprof_cdp_{os.getpid()}"
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check", "--disable-extensions",
         "--hide-scrollbars", f"--user-data-dir={profile}",
         f"--remote-debugging-port={PORT}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_url = None
        for _ in range(50):
            try:
                for target in http_json(f"http://127.0.0.1:{PORT}/json/list"):
                    if target.get("type") == "page":
                        ws_url = target["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not ws_url:
            raise RuntimeError("无法连接 Chrome 的 CDP 端口")

        ws = WS(ws_url)
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": css_w, "height": css_h,
            "deviceScaleFactor": dpr, "mobile": True,
        })
        ws.call("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-reduced-motion", "value": "reduce"}],
        })
        ws.call("Page.navigate", {"url": BASE})

        if tab == "-":
            # 静态模板页：不跑应用 JS，只等资源加载
            time.sleep(max(settle_ms, 2500) / 1000)
        else:
            deadline = time.time() + 25
            while time.time() < deadline:
                time.sleep(0.4)
                try:
                    res = ws.call("Runtime.evaluate", {
                        "expression": "document.readyState+'|'+(typeof switchTab)",
                        "returnByValue": True})
                    val = res["result"].get("value", "")
                    if val.startswith("complete") and "function" in val:
                        break
                except Exception:
                    pass
            else:
                raise RuntimeError("页面未在 25 秒内就绪")

            if tab.startswith("@"):
                # 参数为 @<js文件> 时执行脚本并等待 Promise（用于生成真实对话等内容）
                js = open(tab[1:], encoding="utf-8").read()
                res = ws.call("Runtime.evaluate", {
                    "expression": js, "returnByValue": True,
                    "awaitPromise": True}, timeout=300)
                if "exceptionDetails" in res:
                    print("  JS 异常:", json.dumps(
                        res["exceptionDetails"], ensure_ascii=False)[:500])
                elif res["result"].get("value") is not None:
                    print("  JS 返回:", str(res["result"]["value"])[:400])
            else:
                ws.call("Runtime.evaluate",
                        {"expression": f"switchTab({tab!r})", "returnByValue": True})
            time.sleep(settle_ms / 1000)

        shot = ws.call("Page.captureScreenshot", {"format": "png"}, timeout=45)
        data = base64.b64decode(shot["data"])
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"  {out}  {len(data)} 字节  (css {css_w}x{css_h} @{dpr:.3f}x)")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
