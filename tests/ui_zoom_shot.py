"""缩放视觉证据：同一页在 100% / 200% 下各截一张，便于人工核对清晰度与可读性。"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = "http://127.0.0.1:8013"


def run() -> dict:
    port = 9948 + (int(time.time()) % 20)
    profile = OUT / f"_p_zshot_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1500,950", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = {"ok": False, "shots": []}
    ws = None
    try:
        targets = None
        for _ in range(60):
            try:
                targets = json.load(urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                if targets:
                    break
            except Exception:
                time.sleep(0.4)
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        seq = [0]

        def call(m, pr=None):
            seq[0] += 1
            i = seq[0]
            ws.send(json.dumps({"id": i, "method": m, "params": pr or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == i:
                    return d.get("result", {})

        def js(e):
            r = call("Runtime.evaluate", {"expression": e, "returnByValue": True, "awaitPromise": True})
            return (r.get("result") or {}).get("value")

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE + "/"})
        for _ in range(140):
            try:
                if js("window.__sciagent && window.__sciagent.docs.length>3"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        js("""(async()=>{const vm=window.__sciagent;
          // 选一篇正文密集的论文，翻到有正文的第 2 页，缩放效果才看得出来
          const d=vm.docs.find(x=>/2005\\.14165|2305|2212/.test(x.filename)) || vm.docs.find(x=>(x.page_count||0)>10);
          await vm.openDoc(d); vm.goPage(2); return d.filename;})()""")
        time.sleep(2.2)

        for z, name in ((100, "reader_zoom_100.png"), (200, "reader_zoom_200.png")):
            js(f"window.__sciagent.setZoom({z})")
            time.sleep(1.6)
            shot = call("Page.captureScreenshot", {"format": "png"})
            (OUT / name).write_bytes(base64.b64decode(shot["data"]))
            info = js("""(()=>{const vm=window.__sciagent;
              const st=document.querySelector('.page-stage');
              const f=st.querySelector('.page-frame[data-page="'+vm.page+'"]');
              const img=f.querySelector('.pdf-page');
              return {zoom:vm.zoom, frameW:Math.round(f.getBoundingClientRect().width),
                      imgNatural:img.naturalWidth, scale:+vm.tl.scale.toFixed(3)};})()""")
            res["shots"].append({"file": name, **info})
        res["ok"] = True
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
