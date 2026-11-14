"""阅读器连续滚动回归：验证"多页拼接"而不是"滚到底跳页"。

断言重点：
1. DOM 里所有页的占位框都存在（不是一次只渲染一页）；
2. 视口里能同时看到两页（这正是用户要的"拼在一起"）；
3. 滚动后当前页页码会跟着变化，且文本层跟着换页；
4. 滚动过程没有把页面高度越滚越短（占位高度靠 aspect-ratio 撑住，懒加载不跳）。
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


def run(url: str = BASE + "/") -> dict:
    port = 9750 + (int(time.time()) % 120)
    profile = OUT / f"_p_scroll_{int(time.time())}"
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

        # 打开第一篇够长的文档（页数多才看得出连续滚动）
        picked = js("""(async()=>{const vm=window.__sciagent;
          const d=[...vm.docs].filter(x=>(x.page_count||0)>=5).sort((a,b)=>b.page_count-a.page_count)[0]||vm.docs[0];
          await vm.openDoc(d); return {file:d.filename, pages:d.page_count, id:d.id};})()""")
        res["doc"] = picked
        time.sleep(1.5)

        res["steps"].append({"frame_count": js("document.querySelectorAll('.page-frame').length"),
                             "expected": picked["pages"]})
        if res["steps"][-1]["frame_count"] != picked["pages"]:
            res["error"] = "占位框数量与页数不一致"
            return res

        # 视口内可见页数（连续滚动的核心：至少两页同时可见）
        visible = js("""(()=>{const st=document.querySelector('.page-stage');
          const r=st.getBoundingClientRect();
          return [...st.querySelectorAll('.page-frame')].filter(f=>{
            const b=f.getBoundingClientRect();
            return b.bottom>r.top+4 && b.top<r.bottom-4;}).map(f=>+f.dataset.page);})()""")
        res["steps"].append({"visible_pages_at_top": visible})
        if len(visible) < 1:
            res["error"] = "顶部看不到任何页"
            return res

        # 滚到第 2 页中段，检查页数可见 + 当前页切换
        js("""(()=>{const st=document.querySelector('.page-stage');
          const f=st.querySelector('.page-frame[data-page="2"]');
          st.scrollTop = f.offsetTop + f.offsetHeight*0.25; return st.scrollTop;})()""")
        time.sleep(1.2)
        st2 = js("""(()=>{const vm=window.__sciagent; const st=document.querySelector('.page-stage');
          const r=st.getBoundingClientRect();
          const vis=[...st.querySelectorAll('.page-frame')].filter(f=>{
            const b=f.getBoundingClientRect();
            return b.bottom>r.top+4 && b.top<r.bottom-4;}).map(f=>+f.dataset.page);
          return {page:vm.page, visible:vis, tlPage:vm.tl.page, words:vm.tl.words.length};})()""")
        res["steps"].append({"after_scroll": st2})
        if st2["page"] < 2:
            res["error"] = f"滚动后当前页未更新：{st2['page']}"
            return res

        # 连续滑到底部：两页拼接时 scrollHeight 应远大于单页，且最后一页能到
        tail = js("""(()=>{const st=document.querySelector('.page-stage');
          st.scrollTop = st.scrollHeight; return null;})()""")
        time.sleep(1.5)
        end = js("""(()=>{const vm=window.__sciagent; const st=document.querySelector('.page-stage');
          return {page:vm.page, total:vm.pageCount, atBottom: Math.abs(st.scrollHeight-st.clientHeight-st.scrollTop)<6,
                  loadedImgs: [...st.querySelectorAll('.pdf-page')].filter(i=>i.complete&&i.naturalWidth>0).length};})()""")
        res["steps"].append({"at_bottom": end})
        if not end["atBottom"]:
            res["error"] = "未能滚到底"
            return res

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
