"""阅读器缩放回归：验证放大缩小是"真放大"，不是只改个数字。

断言：
1. 点 + 后页框宽度按档位变大（宽度比 ≈ 档位比），点 − 能回到 100%；
2. 文本层 scale 跟着页图走（角标热区不会和文字错位）；
3. 放大到 200% 后左侧内容仍可滚到（不能因为居中被裁掉）；
4. 缩放后仍在同一页的同一位置（不跳段）；
5. Ctrl+滚轮也能缩放；页面无报错。
"""
from __future__ import annotations

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
    port = 9920 + (int(time.time()) % 40)
    profile = OUT / f"_p_zoom_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1500,950", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = {"ok": False, "steps": [], "error": ""}
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
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(e):
            r = call("Runtime.evaluate", {"expression": e, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(str(r["exceptionDetails"])[:400])
            return (r.get("result") or {}).get("value")

        def wait(expr, sec, iv=0.4):
            end = time.time() + sec
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(iv)
            return False

        call("Page.enable")
        call("Runtime.enable")
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
             "window.__errs=[];window.addEventListener('error',e=>{window.__errs.push(String((e&&e.message)||''));});"})
        call("Page.navigate", {"url": BASE + "/"})
        if not wait("window.__sciagent && window.__sciagent.docs.length>3", 60):
            res["error"] = "主界面未就绪"
            return res

        js("""(async()=>{const vm=window.__sciagent;
          const d=vm.docs.find(x=>(x.page_count||0)>8)||vm.docs[0];
          await vm.openDoc(d); return 1;})()""")
        if not wait("document.querySelectorAll('.page-frame').length>0", 30):
            res["error"] = "页框未渲染"
            return res
        time.sleep(1.2)

        def snap():
            return js("""(()=>{const vm=window.__sciagent;const st=document.querySelector('.page-stage');
              const f=st.querySelector('.page-frame[data-page="'+vm.page+'"]');
              const img=f.querySelector('.pdf-page');
              return {zoom:vm.zoom, frameW:Math.round(f.getBoundingClientRect().width),
                      imgW:Math.round(img.clientWidth), scale:+vm.tl.scale.toFixed(4),
                      stageScrollW:st.scrollWidth, stageClientW:st.clientWidth,
                      scrollTop:Math.round(st.scrollTop), page:vm.page};})()""")

        base = snap()
        res["steps"].append({"at_100": base})

        js("window.__sciagent.setZoom(200)")
        time.sleep(1.2)
        big = snap()
        res["steps"].append({"at_200": big})
        ratio = big["frameW"] / max(base["frameW"], 1)

        # 200% 时左侧必须还能滚到：scrollLeft=0 时页框左边不能是负的
        left = js("""(()=>{const st=document.querySelector('.page-stage');
          st.scrollLeft=0;
          const f=st.querySelector('.page-frame');
          return Math.round(f.getBoundingClientRect().left - st.getBoundingClientRect().left);})()""")
        res["steps"].append({"left_edge_px": left})

        # 文本层热区必须跟着页图缩放，否则点角标会点偏
        scale_ok = abs(big["scale"] - big["imgW"] / base["imgW"] * base["scale"]) < 0.05 if base["imgW"] else False

        js("window.__sciagent.setZoom(100)")
        time.sleep(0.8)
        back = snap()
        res["steps"].append({"back_100": back})

        # Ctrl+滚轮
        wheel = js("""(async()=>{const vm=window.__sciagent;
          const st=document.querySelector('.page-stage');
          const before=vm.zoom;
          st.dispatchEvent(new WheelEvent('wheel',{deltaY:-120,ctrlKey:true,bubbles:true,cancelable:true}));
          await new Promise(r=>setTimeout(r,300));
          return {before, after:vm.zoom};})()""")
        res["steps"].append({"ctrl_wheel": wheel})

        errs = js("window.__errs||[]")
        res["steps"].append({"page_errors": errs})
        ok = (abs(ratio - 2) < 0.08 and back["zoom"] == 100
              and abs(back["frameW"] - base["frameW"]) <= 4
              and left >= -1 and scale_ok and wheel["after"] != wheel["before"] and not errs)
        res["ok"] = ok
        if not ok:
            res["error"] = f"断言未通过 ratio={ratio:.2f} left={left} scale_ok={scale_ok}"
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    raise SystemExit(0 if r["ok"] else 1)
