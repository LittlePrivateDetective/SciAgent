"""文本层交互回归：页图对齐、角标跳转、划词翻译。

不用无头浏览器的 --virtual-time-budget（它会让 SSE 的 reader.read() 永不 resolve），
而是走 CDP 真实时间，等条件出现而不是等固定秒数。

三个断言都要"看得到几何数字"，不接受"看起来能跑"：
1. 文本层缩放后的尺寸必须等于页图渲染尺寸（差 > 2px 就说明 scale 算错，文字会整体漂移）；
2. 选中一段正文后必须弹出「译」按钮，点完必须有中文；
3. 点正文 [n] 必须真的翻到 References 所在页，并高亮该条。
"""
from __future__ import annotations

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


def run(url: str = BASE + "/", shot_dir: Path = OUT) -> dict:
    port = 9411 + (int(time.time()) % 200)
    profile = shot_dir / f"_p_tl_{int(time.time())}"
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

        def shot(name: str, clip: dict | None = None):
            p = shot_dir / name
            params = {"format": "png", "captureBeyondViewport": True}
            if clip:
                params["clip"] = clip
            data = call("Page.captureScreenshot", params).get("data")
            if data:
                import base64
                p.write_bytes(base64.b64decode(data))
            return str(p)

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})

        # 1) 等 Vue 挂载并载入文献
        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        opened = js("""(async()=>{
          const vm = window.__sciagent;
          const d = vm.docs.find(x => x.filename === '1706.03762v1.pdf');
          if (!d) return 'NOT_FOUND';
          await vm.openDoc(d);
          vm.goPage(2);
          return 'OK';
        })()""")
        result["steps"].append({"open": opened})
        if opened != "OK":
            result["error"] = "打开目标文献失败"
            return result

        # 2) 等文本层渲染
        if not wait_for("document.querySelectorAll('.text-layer .tw').length > 50", 40):
            result["error"] = "文本层未渲染"
            result["steps"].append({"tw": js("document.querySelectorAll('.text-layer .tw').length")})
            return result

        geo = js("""(()=>{
          const img = document.querySelector('.pdf-page');
          const layer = document.querySelector('.text-layer');
          const frame = document.querySelector('.page-frame');
          const ir = img.getBoundingClientRect(), lr = layer.getBoundingClientRect();
          const fr = frame.getBoundingClientRect();
          const tws = [...document.querySelectorAll('.text-layer .tw')];
          // 抽查最后一个词：位置远但同样受同一个 scale 约束，最能暴露缩放错误
          const last = tws[tws.length-1].getBoundingClientRect();
          const inside = tws.filter(el=>{
            const r = el.getBoundingClientRect();
            return r.left >= fr.left-2 && r.right <= fr.right+2 && r.top >= fr.top-2 && r.bottom <= fr.bottom+2;
          }).length;
          return {
            imgW:+ir.width.toFixed(1), imgH:+ir.height.toFixed(1),
            layerW:+lr.width.toFixed(1), layerH:+lr.height.toFixed(1),
            frameW:+fr.width.toFixed(1), frameH:+fr.height.toFixed(1),
            dw:+(lr.width-ir.width).toFixed(1), dh:+(lr.height-ir.height).toFixed(1),
            tws:tws.length,
            inside_ratio:+(inside/tws.length).toFixed(3),
            cites:document.querySelectorAll('.tw.cite').length,
            // 有文本层的页不该出现「扫描图」提示条：它现在挂在阅读区外层（不随页图滚动），
            // 一旦 v-if 条件写错就会常驻挡在正文下方
            scanNote: !!document.querySelector('.tl-note'),
            refhit:document.querySelectorAll('.tw.cite.refhit').length,
            lastWord:{t:tws[tws.length-1].textContent, x:+last.left.toFixed(1), y:+last.top.toFixed(1)},
            scale:window.__sciagent.tl.scale
          };
        })()""")
        result["steps"].append({"geometry": geo})
        aligned = (abs(geo["dw"]) <= 2 and abs(geo["dh"]) <= 2
                   and geo["inside_ratio"] >= 0.98 and not geo["scanNote"])
        result["steps"].append({"align": "PASS" if aligned else "FAIL"})

        # 3) 划词翻译：程序化选中正文一句，再派发 mouseup
        sel = js("""(async()=>{
          const tws = [...document.querySelectorAll('.text-layer .tw')];
          // 找一段连续 8 个词、以字母开头的正文，避开表格和公式
          let start = tws.findIndex(el => /^[A-Z][a-z]{3,}$/.test(el.textContent));
          if (start < 0) start = 20;
          const seg = tws.slice(start, start + 8);
          const range = document.createRange();
          range.setStartBefore(seg[0]);
          range.setEndAfter(seg[seg.length-1]);
          const s = window.getSelection();
          s.removeAllRanges(); s.addRange(range);
          const text = s.toString().trim();
          document.querySelector('.text-layer').dispatchEvent(
            new MouseEvent('mouseup', {bubbles:true, clientX:100, clientY:100}));
          await new Promise(r=>setTimeout(r,300));
          const pop = document.querySelector('.sel-pop');
          const btnEl = pop ? pop.querySelector('.sel-body button') : null;
          return {text, popVisible: !!pop, btn: btnEl ? btnEl.textContent.trim() : ''};
        })()""")
        result["steps"].append({"select": sel})
        if not sel or not sel.get("popVisible"):
            result["error"] = "选中后未弹出翻译按钮"
            return result

        # 浮层头部现在还有一个关闭按钮 ×，必须点「译」那个，按位置取会误关浮层
        js("(()=>{const b=document.querySelector('.sel-body button'); if(b) b.click();})()")
        got = wait_for("(()=>{const z=document.querySelector('.sel-zh');return z && z.textContent.trim().length>2;})()", 90)
        zh = js("(document.querySelector('.sel-zh')||{}).textContent||''") if got else ""
        result["steps"].append({"translate": {"ok": got, "zh": (zh or "")[:120]}})
        shot("tl_select.png")

        # 4) 点正文角标 → 必须翻到 References 页并高亮
        js("window.getSelection().removeAllRanges(); document.querySelector('.sel-pop') && (window.__sciagent.sel.visible=false)")
        time.sleep(0.3)
        clicked = js("""(async()=>{
          const el = document.querySelector('.tw.cite.refhit');
          if (!el) return {err:'本页无可跳转角标'};
          const label = el.textContent;
          const before = window.__sciagent.page;
          el.click();
          await new Promise(r=>setTimeout(r,1500));
          const vm = window.__sciagent;
          const st = document.querySelector('.page-stage');
          const hle = st && st.querySelector('.hl');
          const sr = st ? st.getBoundingClientRect() : null;
          const hr = hle ? hle.getBoundingClientRect() : null;
          const fr = document.querySelector('.page-frame');
          return {label, before, after: vm.page,
                  pop: !!document.querySelector('.cite-pop'),
                  popText: (document.querySelector('.cite-pop-text')||{}).textContent||'',
                  hl: vm.highlights.length, hlBox: vm.highlights[0] ? JSON.stringify(vm.highlights[0].bbox) : '',
                  scrollTop: st ? Math.round(st.scrollTop) : -1,
                  stageScrollH: st ? st.scrollHeight : -1,
                  stageClientH: st ? st.clientHeight : -1,
                  hlInline: hle ? hle.getAttribute('style') : '',
                  hlTopComputed: hle ? getComputedStyle(hle).top : '',
                  hlHComputed: hle ? getComputedStyle(hle).height : '',
                  frameTopInView: sr ? Math.round(document.querySelector('.page-frame').getBoundingClientRect().top - sr.top) : -1,
                  pageSizesHas11: !!(vm.pageSizes && vm.pageSizes[11]),
                  hlW: hr ? Math.round(hr.width) : -1,
                  hlH: hr ? Math.round(hr.height) : -1,
                  frameW: fr ? Math.round(fr.getBoundingClientRect().width) : -1,
                  hlVisible: !!(sr && hr && hr.top >= sr.top - 2 && hr.bottom <= sr.bottom + 2),
                  hlTopInView: sr && hr ? Math.round(hr.top - sr.top) : -1};
        })()""")
        result["steps"].append({"cite": clicked})
        # 只判"在视口内"会被 1% 宽、2px 高的畸形框骗过（实测如此）：
        # 参考文献条目横跨页面大半，宽度必须达到页面宽的一半以上，高度至少 8px。
        box_ok = (clicked.get("hlW", 0) >= clicked.get("frameW", 1) * 0.5
                  and clicked.get("hlH", 0) >= 8)
        result["steps"].append({"hl_box": "PASS" if box_ok else "FAIL",
                                "w": clicked.get("hlW"), "h": clicked.get("hlH"),
                                "frameW": clicked.get("frameW")})
        cite_ok = (bool(clicked.get("pop")) and clicked.get("after") != clicked.get("before")
                   and clicked.get("hl", 0) >= 1 and bool(clicked.get("hlVisible")) and box_ok)
        result["steps"].append({"cite_jump": "PASS" if cite_ok else "FAIL"})
        time.sleep(0.6)
        shot("tl_cite.png")

        result["ok"] = aligned and got and cite_ok
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
