"""局部截图：按 CSS 选择器截取某个元素的精确区域，用于逐像素检查对齐。

整页截图缩到 1080 宽后，3px 的错位根本看不出来；这里用 CDP 的
Page.captureScreenshot + clip，按元素的实际 rect 截原图。

用法：
  ./.venv/Scripts/python.exe tests/ui_shot.py ".panel.right .panel-head" out.png
  ./.venv/Scripts/python.exe tests/ui_shot.py ".chat-tools" right_tools.png 1680,1000
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
BASE = "http://127.0.0.1:8013"


def shot(selector: str, out: Path, win: str = "1680,1000",
         pad: int = 6, scale: int = 2, ask: str = "") -> dict:
    port = 9500 + (int(time.time()) % 200)
    profile = ROOT / "docs" / "qa" / "evidence" / f"_p_shot_{int(time.time())}"
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         f"--window-size={win}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ws = None
    try:
        targets = None
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    targets = json.load(r)
                if targets:
                    break
            except Exception:
                time.sleep(0.4)
        page = next((t for t in targets if t.get("type") == "page"), targets[0])
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20)
        seq = [0]

        def call(method, params=None):
            seq[0] += 1
            mid = seq[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE + "/"})
        for _ in range(40):
            time.sleep(0.5)
            r = call("Runtime.evaluate",
                     {"expression": "document.querySelector('.chat-tools .el-switch')?1:0",
                      "returnByValue": True})
            if (r.get("result") or {}).get("value") == 1:
                break
        time.sleep(0.8)
        if ask:
            # 真跑一次问答，才能截到气泡/证据/锚点的真实排版
            call("Runtime.evaluate", {"expression":
                 f"window.__sciagent && window.__sciagent.ask({ask!r})"})
            for _ in range(120):
                time.sleep(1)
                r = call("Runtime.evaluate", {"expression":
                         "(()=>{const m=document.querySelector('.msg.assistant .md');"
                         "return m&&m.textContent.length>60?1:0})()",
                         "returnByValue": True})
                if (r.get("result") or {}).get("value") == 1:
                    break
            time.sleep(1.2)
        expr = f"""(() => {{
            const el = document.querySelector({selector!r});
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {{x: r.x, y: r.y, w: r.width, h: r.height}};
        }})()"""
        rect = (call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                .get("result") or {}).get("value")
        if not rect:
            return {"error": f"未找到元素 {selector}"}
        clip = {"x": max(0, rect["x"] - pad), "y": max(0, rect["y"] - pad),
                "width": rect["w"] + pad * 2, "height": rect["h"] + pad * 2,
                "scale": scale}
        res = call("Page.captureScreenshot",
                   {"format": "png", "clip": clip, "captureBeyondViewport": True})
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(res["data"]))
        return {"file": str(out), "rect": rect, "clip": clip, "bytes": out.stat().st_size}
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sel = sys.argv[1] if len(sys.argv) > 1 else ".panel.right .panel-head"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs/qa/evidence/shot.png"
    win = sys.argv[3] if len(sys.argv) > 3 else "1680,1000"
    q = sys.argv[4] if len(sys.argv) > 4 else ""
    print(json.dumps(shot(sel, dst, win, ask=q), ensure_ascii=False))
