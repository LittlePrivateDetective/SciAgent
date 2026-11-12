"""悬停翻译：译文弹出来的同时，页面上要把"正在译的那一句"框出来。

为什么单独一条用例：光有译文气泡，用户对不上原文是哪一句（句子中间夹公式、
数字时尤其明显）。这条验四件事：
1. 鼠标停在一个词上，立即出现句级高亮（不等 380ms 的翻译延迟）；
2. 高亮矩形贴住该句的词（不糊成整页、不偏行）；
3. 译文出来，气泡里点明"高亮那句"；
4. 鼠标移开，高亮和气泡一起消失（留着黄框会让人以为选中了区域）。

用法：./.venv/Scripts/python.exe tests/ui_hover_sent.py
证据：docs/qa/evidence/hover_sent_{on,off}.png
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = __import__("os").environ.get("SCIAGENT_BASE", "http://127.0.0.1:8013")


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9861 + (int(time.time()) % 30)
    profile = Path(tempfile.gettempdir()) / f"_p_hover_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    res: dict = {"ok": False, "steps": [], "error": ""}
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
        if not targets:
            return {"ok": False, "error": "CDP 端口未就绪"}
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        seq = [0]

        def call(m, p=None):
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": m, "params": p or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == seq[0]:
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(expr):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(str(r["exceptionDetails"])[:300])
            return (r.get("result") or {}).get("value")

        def shot(name):
            d = call("Page.captureScreenshot", {"format": "png"}).get("data")
            if d:
                (OUT / name).write_bytes(base64.b64decode(d))

        call("Page.enable")
        call("Runtime.enable")
        call("Emulation.setDeviceMetricsOverride",
             {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        call("Page.navigate", {"url": BASE + ("&" if "?" in BASE else "?") + "r=1"})
        for _ in range(120):
            if js("!!(window.__sciagent && window.__sciagent.docs.length)"):
                break
            time.sleep(0.4)

        # 打开有词级文本层的版本（_img 那份是扫描件，没有 .tw）
        js("""(async()=>{const vm=window.__sciagent;
              const d=vm.docs.find(x=>x.filename==='1706.03762v1.pdf')
                   || vm.docs.find(x=>!x.is_scanned) || vm.docs[0];
              vm.centerView='reader'; await vm.openDoc(d);
              vm.hover.on = true;              // 悬停翻译开关（默认关，演示时打开）
              await vm.loadTextLayer();
              return d.filename})()""")
        for _ in range(40):
            if js("window.__sciagent.tl.words.length > 0"):
                break
            time.sleep(0.4)
        if not js("window.__sciagent.tl.words.length"):
            res["error"] = "该页没有词级文本层，换一篇再测"
            return res

        # 挑一句 30~120 字符的，取句中第一个词的中心作为悬停点
        target = js("""(()=>{
          const vm = window.__sciagent, ws = vm.tl.words;
          const sis = [...new Set(ws.map(w=>w.si).filter(s=>s!=null))];
          let pick = null;
          for (const s of sis) {
            const g = ws.filter(w=>w.si===s);
            const len = g.map(w=>w.t).join(' ').length;
            if (len > 30 && len < 120) { pick = s; break; }
          }
          if (pick == null) return JSON.stringify({err:'没有合适长度的句子'});
          const i = ws.findIndex(w=>w.si===pick);
          const el = [...document.querySelectorAll('.tw')][i];
          el.scrollIntoView({block:'center'});
          const r = el.getBoundingClientRect();
          return JSON.stringify({si: pick, index: i,
            x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
            words: ws.filter(w=>w.si===pick).length});
        })()""")
        t = json.loads(target)
        res["steps"].append({"target": t})
        if "err" in t:
            res["error"] = t["err"]
            return res

        time.sleep(0.4)
        call("Input.dispatchMouseEvent",
             {"type": "mouseMoved", "x": t["x"], "y": t["y"], "button": "none"})
        time.sleep(0.25)      # 只等到高亮，不给翻译留时间

        early = js("""(()=>{const vm=window.__sciagent;
          const hs=[...document.querySelectorAll('.hsent')];
          const rects=hs.map(e=>{const r=e.getBoundingClientRect();
            return [Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)];});
          return JSON.stringify({si: vm.hover.si, n: hs.length, rects: rects,
            visible: vm.hover.visible, zh: vm.hover.zh});})()""")
        early = json.loads(early)
        res["steps"].append({"immediate": early})

        # 高亮矩形要真的覆盖这一句的词（不是随便一个位置）
        fit = js(f"""(()=>{{
          const vm=window.__sciagent;
          const ws=vm.tl.words.filter(w=>w.si==={t['si']});
          const host=document.querySelector('.text-layer');
          const hb=host.getBoundingClientRect();
          const sc=hb.width / (vm.tl.W || 1);
          let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
          ws.forEach(w=>{{x0=Math.min(x0,w.x);y0=Math.min(y0,w.y);
            x1=Math.max(x1,w.x+w.w);y1=Math.max(y1,w.y+w.h);}});
          const want={{l:hb.left+x0*sc, t:hb.top+y0*sc, r:hb.left+x1*sc, b:hb.top+y1*sc}};
          const hs=[...document.querySelectorAll('.hsent')].map(e=>e.getBoundingClientRect());
          if(!hs.length) return JSON.stringify({{err:'no highlight'}});
          const got={{l:Math.min(...hs.map(r=>r.left)), t:Math.min(...hs.map(r=>r.top)),
                     r:Math.max(...hs.map(r=>r.right)), b:Math.max(...hs.map(r=>r.bottom))}};
          return JSON.stringify({{dl:Math.round(got.l-want.l), dt:Math.round(got.t-want.t),
            dr:Math.round(got.r-want.r), db:Math.round(got.b-want.b)}});
        }})()""")
        fit = json.loads(fit)
        res["steps"].append({"fit": fit})

        # 等译文
        for _ in range(60):
            if js("window.__sciagent.hover.visible && window.__sciagent.hover.zh"):
                break
            time.sleep(0.5)
        late = js("""(()=>{const vm=window.__sciagent;
          const tip=document.querySelector('.hover-tip');
          return JSON.stringify({visible: vm.hover.visible, zh: vm.hover.zh,
            hint: (tip? tip.innerText : '').indexOf('高亮') >= 0,
            hsents: document.querySelectorAll('.hsent').length});})()""")
        late = json.loads(late)
        res["steps"].append({"with_translation": late})
        shot("hover_sent_on.png")

        # 移开 -> 高亮与气泡同时消失
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 6, "y": 6, "button": "none"})
        time.sleep(0.9)
        off = js("""(()=>{const vm=window.__sciagent;
          return JSON.stringify({hsents: document.querySelectorAll('.hsent').length,
            visible: vm.hover.visible, si: vm.hover.si});})()""")
        off = json.loads(off)
        res["steps"].append({"after_leave": off})
        shot("hover_sent_off.png")

        res["checks"] = {
            "highlight_immediate": early["n"] >= 1 and early["si"] == t["si"],
            "highlight_on_sentence": (not fit.get("err")
                                      and all(abs(fit[k]) <= 4 for k in ("dl", "dt", "dr", "db"))),
            "translation_shown": bool(late["visible"] and late["zh"]),
            "hint_mentions_highlight": bool(late["hint"]),
            "cleared_after_leave": off["hsents"] == 0 and off["visible"] is False,
        }
        res["ok"] = all(res["checks"].values())
        return res
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["ok"] else 1)
