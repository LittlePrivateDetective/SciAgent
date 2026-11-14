"""对照区缩放回归：字号真放大、栏宽不变、联动锚点不漂。

断言：
1. 点 + 后正文字号变大，且左右两栏正文同步变大；
2. 两栏宽度（栅格）保持不变 —— 只放大字，不改布局；
3. 缩放后仍在同一段落附近（不跳段）；
4. Ctrl+滚轮可缩放，页面无报错。
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
    port = 9981 + (int(time.time()) % 15)
    profile = OUT / f"_p_bizoom_{int(time.time())}"
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
          const d=vm.docs.find(x=>(x.page_count||0)>10)||vm.docs[0];
          await fetch(`/api/documents/${d.id}/bilingual/translate`, {method:'POST',
            headers:{'Content-Type':'application/json'}, body:JSON.stringify({page_from:1,page_to:4})});
          await vm.openDoc(d);
          vm.centerView='bilingual'; vm.onViewChange('bilingual');
          return 1;})()""")
        if not wait("document.querySelectorAll('.bi-seg').length > 3", 40):
            res["error"] = "对照段落未渲染"
            return res
        time.sleep(1.0)

        def snap():
            return js("""(()=>{const vm=window.__sciagent;
              const en=document.querySelector('.bi-pane:not(.zh)');
              const zh=document.querySelector('.bi-pane.zh');
              const enT=en.querySelector('.bi-text');
              const zhT=zh.querySelector('.bi-text.zh')||zh.querySelector('.bi-text');
              const r=e=>e?Math.round(e.getBoundingClientRect().width):0;
              const fs=e=>e?+parseFloat(getComputedStyle(e).fontSize).toFixed(2):0;
              const segs=[...en.querySelectorAll('.bi-seg')];
              const dr=en.getBoundingClientRect();
              let topSeg=null;
              for(const s of segs){ if(s.getBoundingClientRect().bottom>dr.top+4){topSeg=s.dataset.i;break;} }
              return {zoom:vm.bi.zoom, enPaneW:r(en), zhPaneW:r(zh),
                      enFont:fs(enT), zhFont:fs(zhT), topSeg};})()""")

        base = snap()
        res["steps"].append({"at_100": base})

        js("window.__sciagent.biSetZoom(140)")
        time.sleep(0.8)
        big = snap()
        res["steps"].append({"at_140": big})

        js("window.__sciagent.biSetZoom(100)")
        time.sleep(0.6)
        back = snap()
        res["steps"].append({"back_100": back})

        wheel = js("""(async()=>{const vm=window.__sciagent;
          const en=document.querySelector('.bi-pane:not(.zh)');
          const before=vm.bi.zoom;
          en.dispatchEvent(new WheelEvent('wheel',{deltaY:-120,ctrlKey:true,bubbles:true,cancelable:true}));
          await new Promise(r=>setTimeout(r,300));
          return {before, after:vm.bi.zoom};})()""")
        res["steps"].append({"ctrl_wheel": wheel})

        errs = js("window.__errs||[]")
        res["steps"].append({"page_errors": errs})
        font_ok = big["enFont"] > base["enFont"] * 1.3 and big["zhFont"] > base["zhFont"] * 1.3
        layout_ok = abs(big["enPaneW"] - base["enPaneW"]) <= 2 and abs(big["zhPaneW"] - base["zhPaneW"]) <= 2
        seg_ok = base["topSeg"] == big["topSeg"]
        back_ok = abs(back["enFont"] - base["enFont"]) < 0.2
        res["ok"] = bool(font_ok and layout_ok and seg_ok and back_ok
                         and wheel["after"] != wheel["before"] and not errs)
        if not res["ok"]:
            res["error"] = (f"font_ok={font_ok} layout_ok={layout_ok} "
                            f"seg_ok={seg_ok}({base['topSeg']}→{big['topSeg']}) back_ok={back_ok}")
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
