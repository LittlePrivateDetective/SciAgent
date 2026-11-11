"""论文发现与推荐（官方 1.4）前端回归。

三块能力都要看到真实网络结果，不接受"页面打开了就算过"：
1. 搜索下载：arXiv 检索出结果，库里已有的标注「已在库里」；
2. 补全参考文献：候选按正文被引次数排序，能勾选、能给 job_id；
3. 新论文推荐：带中文推荐理由、相关度、GitHub 标记。

顺带收集页面 console 报错——Vue 模板里写错一个字段名，页面不会崩，
只会静默渲染空白，靠截图看不出来。
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


def run(url: str = BASE + "/", shot_dir: Path = OUT) -> dict:
    port = 9711 + (int(time.time()) % 200)
    profile = shot_dir / f"_p_disc_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    result: dict = {"ok": False, "steps": [], "error": "", "console": []}
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
                if d.get("method") == "Runtime.consoleAPICalled":
                    args = [str(a.get("value", "")) for a in d["params"].get("args", [])]
                    if d["params"].get("type") == "error":
                        result["console"].append(" ".join(args)[:200])
                if d.get("method") == "Runtime.exceptionThrown":
                    desc = (d["params"].get("exceptionDetails") or {}).get("text", "")
                    result["console"].append("EXC " + str(desc)[:200])

        def js(expr: str):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return (r.get("result") or {}).get("value")

        def wait_for(expr: str, seconds: float = 40, interval: float = 0.5):
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

        # 1) 打开抽屉 + 搜索
        js("window.__sciagent.openDiscover('search')")
        time.sleep(0.8)
        js("""(async()=>{
          const vm = window.__sciagent;
          vm.disc.q = 'attention is all you need';
          await vm.doSearch();
        })()""")
        ok = wait_for("document.querySelectorAll('.disc-card').length > 0", 60)
        if not ok:
            result["error"] = "搜索无结果卡片"
            return result
        search = js("""(()=>{
          const pane = [...document.querySelectorAll('.el-tab-pane')].find(p => p.offsetParent);
          const cards = [...pane.querySelectorAll('.disc-card')];
          const first = cards[0];
          return {
            n: cards.length,
            title: (first.querySelector('.disc-name')||{}).textContent||'',
            aid: (first.querySelector('.disc-aid')||{}).textContent||'',
            inLib: cards.filter(c => c.textContent.includes('已在库里')).length,
            hasDownload: cards.some(c => [...c.querySelectorAll('button')].some(b => b.textContent.includes('下载并入库'))),
            ms: window.__sciagent.disc.searchMs
          };
        })()""")
        result["steps"].append({"search": search})
        shot("disc_search.png")

        # 2) 参考文献补全：先开一篇真实文献
        opened = js("""(async()=>{
          const vm = window.__sciagent;
          const d = vm.docs.find(x => x.filename.indexOf('1706.03762v1.pdf') === 0) || vm.docs[0];
          await vm.openDoc(d);
          vm.disc.tab = 'refs';
          await vm.loadRefs();
          return {doc: d.filename, id: d.id};
        })()""")
        wait_for("document.querySelectorAll('.disc-card.ref').length > 0", 90)
        refs = js("""(()=>{
          const cards = [...document.querySelectorAll('.disc-card.ref')];
          const cites = cards.map(c => {
            const t = (c.querySelector('.ref-cites')||{}).textContent||'';
            const m = t.match(/被引\\s*(\\d+)/); return m ? +m[1] : -1;
          });
          return {
            n: cards.length, cites,
            sortedDesc: cites.every((v,i) => i === 0 || cites[i-1] >= v),
            selected: window.__sciagent.disc.refSelected.length,
            total: window.__sciagent.disc.refTotal,
            matched: window.__sciagent.disc.refMatched,
            first: (cards[0] && cards[0].querySelector('.disc-name').textContent) || '',
            hasAnchors: cards.every(c => !!c.querySelector('.tag-aid, .tag-ok, .tag-miss'))
          };
        })()""")
        result["steps"].append({"refs": {**refs, "doc": opened}})
        shot("disc_refs.png")

        # 3) 新论文推荐
        js("window.__sciagent.disc.tab = 'rec'; window.__sciagent.loadRec()")
        ok = wait_for("(()=>{const c=document.querySelectorAll('.disc-card');return c.length>0 && !window.__sciagent.disc.recLoading;})()", 120)
        rec = js("""(()=>{
          const pane = [...document.querySelectorAll('.el-tab-pane')].find(p => p.offsetParent);
          const cards = [...pane.querySelectorAll('.disc-card')];
          const reasons = cards.map(c => ((c.querySelector('.disc-reason')||{}).textContent||'').trim());
          return {
            n: cards.length,
            topics: window.__sciagent.disc.recTopics,
            reasonsFilled: reasons.filter(r => r.length > 8).length,
            firstReason: reasons[0] || '',
            withGh: cards.filter(c => (c.querySelector('.disc-gh')||{}).textContent).length,
            hasFeedbackBtn: cards.length ? [...cards[0].querySelectorAll('button')].map(b=>b.textContent.trim()) : []
          };
        })()""")
        result["steps"].append({"rec": rec})
        shot("disc_rec.png")

        # 4) 反馈：踩一条应立即从列表消失并落库
        fb = js("""(async()=>{
          const vm = window.__sciagent;
          const it = vm.disc.recItems[0];
          if (!it) return {err:'no item'};
          const aid = it.arxiv_id;
          await vm.sendFeedback(it, 'dislike');
          await new Promise(r=>setTimeout(r,600));
          return {aid, stillInList: vm.disc.recItems.some(x => x.arxiv_id === aid)};
        })()""")
        result["steps"].append({"feedback": fb})

        ok = (search["n"] > 0 and refs["n"] >= 3 and refs["sortedDesc"]
              and rec["n"] > 0 and rec["reasonsFilled"] >= max(1, rec["n"] - 1)
              and fb and not fb.get("stillInList"))
        result["ok"] = bool(ok)
        if result["console"]:
            result["steps"].append({"console_errors": result["console"][:5]})
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
