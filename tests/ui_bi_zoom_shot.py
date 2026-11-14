"""对照区缩放视觉证据：100% 与 140% 各一张。"""
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
    port = 9986 + (int(time.time()) % 10)
    profile = OUT / f"_p_bishot_{int(time.time())}"
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
          const d=vm.docs.find(x=>/2212\\.08073|2005\\.14165|2305/.test(x.filename)) || vm.docs.find(x=>(x.page_count||0)>10);
          await fetch(`/api/documents/${d.id}/bilingual/translate`, {method:'POST',
            headers:{'Content-Type':'application/json'}, body:JSON.stringify({page_from:1,page_to:4})});
          await vm.openDoc(d); vm.centerView='bilingual'; vm.onViewChange('bilingual');
          return d.filename;})()""")
        time.sleep(2.5)

        for z, name in ((100, "bi_zoom_100.png"), (150, "bi_zoom_150.png")):
            js(f"window.__sciagent.biSetZoom({z})")
            time.sleep(1.0)
            shot = call("Page.captureScreenshot", {"format": "png"})
            (OUT / name).write_bytes(base64.b64decode(shot["data"]))
            info = js("""(()=>{const vm=window.__sciagent;
              const en=document.querySelector('.bi-pane:not(.zh) .bi-text');
              return {zoom:vm.bi.zoom, font:getComputedStyle(en).fontSize};})()""")
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
