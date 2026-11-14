"""中英对照交互回归：鼠标指向段落 → 左右同时高亮；滚动联动不抖。

断言：
1. 鼠标进入左栏某段后，右栏同 data-i 的段落也带 hov 类（"指哪看哪"）；
2. 移开后两侧都清空高亮；
3. 开启联动滚动左栏，右栏跟随到同一段（不是同比例，是同段落）；
4. 连续滚动 20 次的事件里，右栏 scrollTop 不出现来回抖动（相邻两次变化符号不反复）。
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
    port = 9960 + (int(time.time()) % 30)
    profile = OUT / f"_p_bi_{int(time.time())}"
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
             "window.__errs=[];window.addEventListener('error',e=>{"
             "window.__errs.push(String((e&&e.message)||''));});"})
        call("Page.navigate", {"url": BASE + "/"})
        if not wait("window.__sciagent && window.__sciagent.docs.length>3", 60):
            res["error"] = "主界面未就绪"
            return res

        js("""(async()=>{const vm=window.__sciagent;
          const d=vm.docs.find(x=>(x.page_count||0)>10)||vm.docs[0];
          await vm.openDoc(d); vm.centerView='bilingual'; vm.onViewChange('bilingual');
          return d.filename;})()""")
        if not wait("document.querySelectorAll('.bi-seg').length > 6", 40):
            res["error"] = "对照段落未渲染"
            return res
        # 多加载几页，段落太少时两侧都滚不动，联动测不出来
        js("""(async()=>{const vm=window.__sciagent;
          for(let k=0;k<3;k++){ await vm.loadBi(false); } return 1;})()""")
        time.sleep(1.0)

        # 1) hover 联动的核心：指左亮右（等 Vue 渲染一拍再读 class）
        hov = js("""(async()=>{
          const en=[...document.querySelectorAll('.bi-pane:not(.zh) .bi-seg')][2];
          const i=en.dataset.i;
          en.dispatchEvent(new MouseEvent('mouseenter',{bubbles:false}));
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          const zh=document.querySelector('.bi-pane.zh .bi-seg[data-i="'+i+'"]');
          return {i:+i, zhHasHov: zh ? zh.classList.contains('hov') : null,
                  hovCount: document.querySelectorAll('.bi-seg.hov').length,
                  hoverState: window.__sciagent.bi.hover};})()""")
        res["steps"].append({"hover_left": hov})

        # 2) 移开清空
        clear = js("""(async()=>{
          const en=[...document.querySelectorAll('.bi-pane:not(.zh) .bi-seg')][2];
          en.dispatchEvent(new MouseEvent('mouseleave',{bubbles:false}));
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          return {hovCount: document.querySelectorAll('.bi-seg.hov').length,
                  hoverState: window.__sciagent.bi.hover};})()""")
        res["steps"].append({"hover_clear": clear})

        # 3) 滚动联动：滚左栏后右栏落到同一段
        sync = js("""(async()=>{const vm=window.__sciagent;
          const en=document.querySelector('.bi-pane:not(.zh)');
          const zh=document.querySelector('.bi-pane.zh');
          const segs=[...en.querySelectorAll('.bi-seg')];
          // 取中段：靠近末尾时右栏可能已经到底，scrollTop 被夹住，测不出"同一段"
          const k=Math.min(Math.floor(segs.length/2), segs.length-1);
          en.scrollTop = segs[k].offsetTop + 10;
          en.dispatchEvent(new Event('scroll'));
          await new Promise(r=>setTimeout(r,500));
          const zs=[...zh.querySelectorAll('.bi-seg')];
          const topZh = zs.find(s=>s.offsetTop+s.offsetHeight>zh.scrollTop+2);
          return {segCount: segs.length, picked: segs[k].dataset.i,
                  zhTopSeg: topZh?topZh.dataset.i:null,
                  zhScroll: Math.round(zh.scrollTop),
                  same: !!topZh && topZh.dataset.i===segs[k].dataset.i};})()""")
        res["steps"].append({"scroll_sync": sync})

        # 4) 抖动检测：连续滚 20 步，记录右栏 scrollTop 变化方向翻转次数
        jitter = js("""(async()=>{const vm=window.__sciagent;
          const en=document.querySelector('.bi-pane:not(.zh)');
          const zh=document.querySelector('.bi-pane.zh');
          const seq=[];
          for(let k=0;k<20;k++){
            en.scrollTop += 40;
            en.dispatchEvent(new Event('scroll'));
            await new Promise(r=>requestAnimationFrame(()=>r()));
            seq.push(Math.round(zh.scrollTop));
          }
          let flips=0, dir=0;
          for(let i=1;i<seq.length;i++){
            const d=seq[i]-seq[i-1];
            if(!d) continue;
            const s=d>0?1:-1;
            if(dir && s!==dir) flips++;
            dir=s;
          }
          return {samples:seq, flips, enTop:Math.round(en.scrollTop), zhTop:Math.round(zh.scrollTop)};})()""")
        res["steps"].append({"jitter": jitter})

        errs = js("window.__errs||[]")
        res["steps"].append({"page_errors": errs})
        ok = (hov.get("zhHasHov") is True and clear.get("hovCount") == 0
              and sync.get("same") is True and jitter.get("flips", 99) <= 2 and not errs)
        res["ok"] = ok
        if not ok:
            res["error"] = "断言未通过"
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
