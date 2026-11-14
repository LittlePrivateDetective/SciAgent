"""连续滚动的视觉证据：截图 + 记录同屏可见页数与页缝间距。"""
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
    port = 9900 + (int(time.time()) % 60)
    profile = OUT / f"_p_shot_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1500,950", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    res = {"ok": False}
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
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        seq = [0]

        def call(method, params=None):
            seq[0] += 1
            i = seq[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == i:
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(expr):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(str(r["exceptionDetails"])[:300])
            return (r.get("result") or {}).get("value")

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE + "/"})
        for _ in range(120):
            try:
                if js("!!(window.__sciagent && window.__sciagent.docs.length)"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        js("""(async()=>{const vm=window.__sciagent;
          const d=[...vm.docs].filter(x=>(x.page_count||0)>=8).sort((a,b)=>b.page_count-a.page_count)[0];
          await vm.openDoc(d); return d.filename;})()""")
        time.sleep(1.6)

        # 滚到第 1、2 页交界，这样一屏里正好上下两页
        js("""(()=>{const st=document.querySelector('.page-stage');
          const f=st.querySelector('.page-frame[data-page="1"]');
          st.scrollTop=f.offsetTop+f.offsetHeight-st.clientHeight*0.55; return 1;})()""")
        time.sleep(1.8)

        geo = js("""(()=>{const st=document.querySelector('.page-stage');
          const sr=st.getBoundingClientRect();
          const fs=[...st.querySelectorAll('.page-frame')].map(f=>{
            const b=f.getBoundingClientRect();
            return {p:+f.dataset.page, top:Math.round(b.top-sr.top), h:Math.round(b.height),
                    vis: b.bottom>sr.top+4 && b.top<sr.bottom-4};});
          const vis=fs.filter(x=>x.vis);
          let gap=null;
          if(vis.length>=2){const a=fs.find(x=>x.p===vis[0].p), b=fs.find(x=>x.p===vis[1].p);
            gap=b.top-(a.top+a.h);}
          return {stageH:Math.round(sr.height), pageH:fs[0].h, visible:vis.map(x=>x.p),
                  seamGap:gap, page:window.__sciagent.page};})()""")
        res["geo"] = geo

        shot = call("Page.captureScreenshot", {"format": "png"})
        (OUT / "reader_cont_scroll.png").write_bytes(base64.b64decode(shot["data"]))
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
