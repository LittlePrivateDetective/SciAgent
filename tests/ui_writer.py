"""写作 Copilot 前端回归：切到「写作」→ 输入 idea → 生成 → 校验渲染与无幻觉数字。

关键断言不是"文字出来了"，而是：
1. 四段（Abstract / Introduction / Related Work / Research Plan）都有正文；
2. References 每一条都能回指库内 doc_id（点得开原文），且 refs_generated_by_llm 必须为 0；
3. 正文里的 [n] 没有越界编号（越界说明模型在编引文）。
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

IDEA = "把稀疏注意力用到长文档检索增强生成上，降低显存占用同时保持检索质量"


def run(url: str = BASE + "/") -> dict:
    port = 9521 + (int(time.time()) % 200)
    profile = OUT / f"_p_wr_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
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
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
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

        def wait_for(expr: str, seconds: float, interval: float = 0.5):
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
        call("Page.navigate", {"url": url})
        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            res["error"] = "主界面未就绪"
            return res

        # 切到写作视图并填 idea
        js("""(()=>{const vm=window.__sciagent;
              vm.centerView='write';
              vm.onViewChange('write');
              vm.wr.idea=%s; return 1;})()""" % json.dumps(IDEA, ensure_ascii=False))
        time.sleep(0.6)
        vis = js("(()=>{const w=document.querySelector('.write-wrap');return w?getComputedStyle(w).display:'none';})()")
        res["steps"].append({"write_view_display": vis})
        if vis == "none":
            res["error"] = "写作视图未显示"
            return res

        t0 = time.time()
        js("window.__sciagent.genOutline()")
        if not wait_for("!!(window.__sciagent.wr.result || window.__sciagent.wr.error)", 180, 1.0):
            res["error"] = "生成超时（180s）"
            return res
        elapsed = round(time.time() - t0, 1)

        out = js("""(()=>{
          const vm = window.__sciagent, r = vm.wr.result;
          if (!r) return {err: vm.wr.error};
          const secs = [...document.querySelectorAll('.write-sec')].map(el=>({
            name: el.querySelector('.write-sec-head span').textContent.trim(),
            len: (el.querySelector('.write-sec-text')||{textContent:''}).textContent.trim().length,
            html: (el.innerHTML||'').slice(0,220)
          }));
          const refs = [...document.querySelectorAll('.write-ref')].map(el=>({
            idx: el.querySelector('.write-ref-idx').textContent.trim(),
            text: el.querySelector('.write-ref-text').textContent.trim()
          }));
          return {
            err:'', title: r.title,
            // Vue 的响应式代理直接 returnByValue 会序列化成 {}，必须解包成普通对象
            elapsed: JSON.parse(JSON.stringify(r.checks)),
            secs, refs: refs.length, refsSample: refs.slice(0,2),
            dimRefs: document.querySelectorAll('.write-ref.dim').length,
            checksDom: [...document.querySelectorAll('.wc-item')].map(e=>e.textContent.trim()),
            allRefsHaveDoc: r.references.every(x=>!!x.doc_id && vm.docs.some(d=>d.id===x.doc_id)),
            bodyScroll: (document.querySelector('.write-body')||{}).scrollHeight
          };
        })()""")
        out["elapsed_s"] = elapsed
        res["steps"].append({"render": out})

        # 断言
        need = {"Abstract", "Introduction", "Related Work", "Research Plan"}
        got = {s["name"].split(" ·")[0] for s in out.get("secs", [])}
        sec_ok = need.issubset(got) and all(s["len"] > 120 for s in out.get("secs", []))
        refs_ok = (out.get("refs", 0) >= 1 and out.get("allRefsHaveDoc")
                   and out.get("elapsed", {}).get("refs_generated_by_llm") == 0)
        cite_ok = not out.get("elapsed", {}).get("citations_invalid")
        res["steps"].append({"sections": "PASS" if sec_ok else "FAIL"})
        res["steps"].append({"refs_real": "PASS" if refs_ok else "FAIL"})
        res["steps"].append({"no_invalid_cite": "PASS" if cite_ok else "FAIL"})

        # 点第一条 Reference，应该打开对应文献
        clicked = js("""(async()=>{
          const el = document.querySelector('.write-ref');
          if (!el) return {err:'no ref'};
          const before = window.__sciagent.centerView;
          el.click();
          await new Promise(r=>setTimeout(r,1800));
          const vm = window.__sciagent;
          return {before, after: vm.centerView, doc: vm.activeDoc ? vm.activeDoc.filename : '',
                  docId: vm.activeDoc ? vm.activeDoc.id : ''};
        })()""")
        res["steps"].append({"open_ref": clicked})
        open_ok = clicked.get("after") == "reader" and bool(clicked.get("docId"))

        time.sleep(0.5)
        # 回到写作视图截图（点开文献后会切走）
        js("window.__sciagent.centerView='write'")
        time.sleep(0.7)
        shot = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}).get("data")
        if shot:
            (OUT / "wr_outline.png").write_bytes(base64.b64decode(shot))

        res["ok"] = sec_ok and refs_ok and cite_ok and open_ok
        return res
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
