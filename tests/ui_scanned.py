"""扫描件交互回归：页图不能被拖走 + 无文本层时能整页翻译。

背景（用户实测反馈）：扫描件页面上按住鼠标想选字，浏览器把整张页图当成可拖动
图片拽走，只剩一个幽灵影像跟着光标，看起来像"文档被拖跑了"；而且这页没有
文本层，划词翻译天然不可用，界面却只说"不可用"，没给出路。

三条断言：
1. 页图 `draggable=false`，CSS 也禁了 `-webkit-user-drag`；
2. 无文本层的页必须出现「翻译本页」入口，而不是一句死提示；
3. 点下去要真拿到中文，且浮层能拖——扫描件用户同样需要把译文挪开。
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
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = "http://127.0.0.1:8013"
SCANNED = "1706.03762v1_img.pdf"


def run(url: str = BASE + "/", shot_dir: Path = OUT) -> dict:
    port = 9611 + (int(time.time()) % 200)
    profile = shot_dir / f"_p_scan_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    result: dict = {"ok": False, "steps": [], "error": ""}
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
            result["error"] = "CDP 端口未就绪"
            return result
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        seq = [0]

        def call(method: str, params: dict | None = None):
            seq[0] += 1
            i = seq[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == i:
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(expr: str):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return (r.get("result") or {}).get("value")

        def wait_for(expr: str, seconds: float = 30, interval: float = 0.4):
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        def shot(name: str):
            data = call("Page.captureScreenshot", {"format": "png"}).get("data")
            if data:
                (shot_dir / name).write_bytes(base64.b64decode(data))
            return str(shot_dir / name)

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})

        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        info = js("""(async()=>{
          const vm = window.__sciagent;
          const d = vm.docs.find(x => x.filename === '%s');
          if (!d) return {err:'NOT_FOUND'};
          await vm.openDoc(d);
          vm.goPage(2);
          return {id:d.id, scanned:!!d.is_scanned, pages:d.page_count};
        })()""" % SCANNED)
        result["steps"].append({"doc": info})
        if not info or info.get("err"):
            result["error"] = "未找到扫描件"
            return result

        if not wait_for("!!document.querySelector('.tl-note')", 40):
            result["error"] = "扫描页未出现提示条"
            return result
        time.sleep(0.5)

        note = js("""(()=>{
          const img = document.querySelector('.pdf-page');
          const note = document.querySelector('.tl-note');
          const btn = note ? note.querySelector('button') : null;
          return {
            words: document.querySelectorAll('.text-layer .tw').length,
            hasLayer: !!document.querySelector('.text-layer'),
            draggableAttr: img ? img.getAttribute('draggable') : null,
            draggableProp: img ? img.draggable : null,
            userDrag: img ? getComputedStyle(img).webkitUserDrag : null,
            noteText: note ? note.textContent.trim() : '',
            btnText: btn ? btn.textContent.trim() : '',
            docTagTitle: (document.querySelector('.doc-item.active .tag-warn')||{}).title || ''
          };
        })()""")
        result["steps"].append({"note": note})
        shot("scan_note.png")

        drag_ok = (note["draggableProp"] is False and note["userDrag"] == "none")
        result["steps"].append({"img_not_draggable": "PASS" if drag_ok else "FAIL"})

        # 点「翻译本页」
        js("(()=>{const b=document.querySelector('.tl-note button'); if(b) b.click();})()")
        got = wait_for(
            "(()=>{const z=document.querySelector('.sel-zh');return z && z.textContent.trim().length>50;})()",
            240)
        if not got:
            result["error"] = "整页翻译未返回"
            result["steps"].append({"note2": js("document.querySelector('.sel-pop')?'pop':'nopop'")})
            return result
        time.sleep(0.5)

        pop = js("""(()=>{
          const pop = document.querySelector('.sel-pop');
          const pr = pop.getBoundingClientRect();
          const z = document.querySelector('.sel-zh');
          const head = document.querySelector('.sel-head').getBoundingClientRect();
          return {
            title: (document.querySelector('.sel-head-t')||{}).textContent||'',
            zhLen: z ? z.textContent.length : 0,
            zhHead: z ? z.textContent.slice(0, 60) : '',
            paras: z ? z.textContent.split('\\n\\n').filter(x=>x.trim()).length : 0,
            left:+pr.left.toFixed(1), top:+pr.top.toFixed(1), h:+pr.height.toFixed(1),
            head:{x:+(head.left+head.width/2).toFixed(1), y:+(head.top+head.height/2).toFixed(1)},
            inViewport: pr.top >= -0.5 && pr.bottom <= innerHeight + 0.5
          };
        })()""")
        result["steps"].append({"popup": pop})
        shot("scan_translate.png")

        # 浮层拖动（扫描件用户同样要挪开译文）
        hx, hy = pop["head"]["x"], pop["head"]["y"]
        tx, ty = hx - 260, min(hy + 260, js("innerHeight") - 200)

        def mouse(kind: str, x: float, y: float):
            call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y,
                "button": "left" if kind != "mouseMoved" else "none",
                "buttons": 1 if kind != "mouseReleased" else 0,
                "clickCount": 1,
            })

        mouse("mousePressed", hx, hy)
        for k in range(1, 6):
            mouse("mouseMoved", hx + (tx - hx) * k / 5, hy + (ty - hy) * k / 5)
            time.sleep(0.06)
        mouse("mouseReleased", tx, ty)
        time.sleep(0.4)
        moved = js("(()=>{const r=document.querySelector('.sel-pop').getBoundingClientRect();return {left:+r.left.toFixed(1), top:+r.top.toFixed(1)};})()")
        result["steps"].append({"drag": {"from": {"left": pop["left"], "top": pop["top"]},
                                         "to": moved}})
        drag_moved = abs(moved["left"] - pop["left"]) + abs(moved["top"] - pop["top"]) >= 100
        result["steps"].append({"popup_draggable": "PASS" if drag_moved else "FAIL"})
        shot("scan_dragged.png")

        ok = (drag_ok and pop["zhLen"] > 50 and pop["inViewport"]
              and "整页翻译" in pop["title"] and drag_moved)
        result["ok"] = ok
        return result
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r["ok"] else 1)
