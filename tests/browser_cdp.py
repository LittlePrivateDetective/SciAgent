"""用 Chrome DevTools Protocol 在**真实时间**下驱动无头 Edge。

为什么不能直接 `--virtual-time-budget=120000 --dump-dom`：
虚拟时钟会把 setTimeout 快进，但 SSE 的 `reader.read()` 是真实网络 I/O。
实测流式用例会永远卡在 await，连 catch 分支都走不到（QA 第三轮 C07B
只输出了「开始真实 SSE…」一行）。而 bilingual / anchor / graph 这类纯 DOM
用例没有长连接，虚拟时钟反而更快，所以只有流式用例必须走 CDP。

CDP 下页面用的是真实计时器、真实 fetch、真实 SSE，取证也最硬。
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import websocket  # websocket-client

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def run_harness(url: str, profile: Path, *, timeout: int = 200,
                screenshot: Path | None = None) -> dict:
    """打开 url，等到页面 #out 出现 RESULT:，返回文本与截图路径。

    返回 {"text": str, "dom_length": int, "shot": str, "error": str}
    """
    out = {"text": "", "dom_length": 0, "shot": "", "error": ""}
    if not EDGE.exists():
        out["error"] = "本机无 Edge"
        return out

    port = 9333 + (int(time.time()) % 300)  # 避开被占用的调试端口
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*",  # 否则 CDP WebSocket 握手 403 Forbidden
         f"--user-data-dir={profile}", "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ws = None
    try:
        # 等 CDP 端口就绪
        targets = None
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    targets = json.load(r)
                if targets:
                    break
            except Exception:
                time.sleep(0.4)
        if not targets:
            out["error"] = "CDP 端口未就绪"
            return out
        page = next((t for t in targets if t.get("type") == "page"), targets[0])
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)
        seq = [0]

        def call(method: str, params: dict | None = None, wait: bool = True):
            seq[0] += 1
            mid = seq[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            if not wait:
                return None
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})

        expr = "document.getElementById('out') ? document.getElementById('out').textContent : ''"
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            time.sleep(0.6)
            try:
                res = call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True}, wait=True)
                text = (res.get("result") or {}).get("value") or ""
            except Exception:
                continue
            if "RESULT:" in text:
                break
        out["text"] = text.strip()
        try:
            dom = call("Runtime.evaluate",
                       {"expression": "document.documentElement.outerHTML.length",
                        "returnByValue": True})
            out["dom_length"] = (dom.get("result") or {}).get("value") or 0
        except Exception:
            pass
        if screenshot:
            try:
                shot = call("Page.captureScreenshot", {"format": "png"})
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                screenshot.write_bytes(base64.b64decode(shot["data"]))
                out["shot"] = str(screenshot)
            except Exception as exc:  # 截图失败不影响结论
                out["error"] = f"截图失败 {exc}"
    except Exception as exc:
        out["error"] = f"CDP 异常 {exc}"
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return out
