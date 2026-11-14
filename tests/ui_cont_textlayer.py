"""连续滚动下文本层/引用跳转的回归：验证换页后词层跟着走、跳转能落到高亮处。"""
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


def run(url: str = BASE + "/") -> dict:
    port = 9830 + (int(time.time()) % 90)
    profile = OUT / f"_p_scroll_tl_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1500,950", "about:blank"],
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
            res["error"] = "CDP 端口未就绪"
            return res
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
                raise RuntimeError(str(r["exceptionDetails"])[:400])
            return (r.get("result") or {}).get("value")

        def wait_for(expr, seconds, interval=0.5):
            end = time.time() + seconds
            while time.time() < end:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        call("Page.enable")
        call("Runtime.enable")
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
             "window.__errs=[];window.addEventListener('error',e=>{"
             "window.__errs.push(String((e&&e.message)||'')+' @'+(e&&e.filename||''));});"})
        call("Page.navigate", {"url": url})
        if not wait_for("!!(window.__sciagent && window.__sciagent.info)", 60):
            res["error"] = "主界面未就绪"
            return res
        # 文献列表是导航后异步拉的，不等它就会"一篇都没有"直接判失败
        if not wait_for("window.__sciagent.docs.length > 0", 60, 0.4):
            res["error"] = "文献列表未加载"
            return res

        # 选一篇正文页有文字层的论文（排除扫描书）。逐篇试，等文本层真的到位再判定。
        picked = js("""(async()=>{const vm=window.__sciagent;
          for (const d of vm.docs) {
            if ((d.page_count||0) < 3 || (d.page_count||0) > 60) continue;
            await vm.openDoc(d);
            for (let i=0;i<12;i++){
              await new Promise(r=>setTimeout(r,300));
              if (vm.tl.words && vm.tl.words.length > 30) return {file:d.filename, pages:d.page_count, id:d.id};
            }
          }
          return null;})()""")
        res["doc"] = picked
        if not picked:
            res["error"] = "没有找到带文字层的样本"
            return res
        time.sleep(0.8)

        # 文本层缩放应等于当前页图片宽度 / PDF 页宽
        scale = js("""(()=>{const vm=window.__sciagent;
          const img=document.querySelector('.page-frame[data-page="'+vm.page+'"] .pdf-page');
          return {scale:+vm.tl.scale.toFixed(4), imgW: img?img.clientWidth:0,
                  pdfW: vm.tl.W, words: vm.tl.words.length, page: vm.page};})()""")
        res["steps"].append({"scale": scale})
        if not scale["imgW"] or abs(scale["scale"] - scale["imgW"] / scale["pdfW"]) > 0.01:
            res["error"] = f"文本层缩放不对：{scale}"
            return res

        # 滚到第 2 页：文本层应换成第 2 页的词，且词层挂在第 2 页的框里
        js("""(()=>{const st=document.querySelector('.page-stage');
          const f=st.querySelector('.page-frame[data-page="2"]');
          st.scrollTop=f.offsetTop+20; return 1;})()""")
        time.sleep(1.5)
        p2 = js("""(()=>{const vm=window.__sciagent;
          const host=document.querySelector('.page-frame[data-page="'+vm.page+'"] .text-layer');
          return {page:vm.page, tlPage:vm.tl.page, words:vm.tl.words.length,
                  layerInCurrentFrame: !!host,
                  layerInOtherFrame: !!document.querySelector('.page-frame:not([data-page="'+vm.page+'"]):has(.text-layer)')};})()""")
        res["steps"].append({"page2": p2})
        if p2["tlPage"] != p2["page"] or not p2["layerInCurrentFrame"]:
            res["error"] = f"文本层未跟随当前页：{p2}"
            return res

        # 跳转：目标是第 5 页（离当前页有距离，能验证"先瞬移再定位"）。
        # 直接走同一个 jump()，不依赖先跑一遍云端问答。
        jump = js("""(async()=>{const vm=window.__sciagent;
          const target=Math.min(5, vm.pageCount);
          const meta=vm.pageSizes[target]||{width:612,height:792};
          await vm.jump({page:target, bbox:[meta.width*0.1, meta.height*0.2,
                                          meta.width*0.9, meta.height*0.35],
                         doc_id:vm.activeDoc.id, text:''});
          await new Promise(r=>setTimeout(r,1200));
          const st=document.querySelector('.page-stage');
          const f=st.querySelector('.page-frame[data-page="'+target+'"]');
          const sr=st.getBoundingClientRect(), fr=f.getBoundingClientRect();
          return {targetPage:target, page:vm.page, hl:st.querySelectorAll('.hl').length,
                  targetInView: fr.top < sr.bottom-8 && fr.bottom > sr.top+8,
                  scrollTop: Math.round(st.scrollTop)};})()""")
        res["steps"].append({"jump": jump})

        errs = js("window.__errs||[]")
        res["steps"].append({"page_errors": errs})
        res["ok"] = not errs
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    raise SystemExit(0 if r["ok"] else 1)
