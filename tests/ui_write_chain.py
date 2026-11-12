"""写作「分步」链路回归：框架 → 正文引用跳转 → 架构图（带着大纲去画）→ tab 往返不丢状态。

为什么现有两条写作用例不够：
    ui_writer 验的是"框架生成得对不对"（章节、References 全来自库内、无越界引用），
    ui_write_viz 验的是"架构图/图表画得对不对"，两条都是各自独立输入 idea 起步。
    真实使用是一条链：先出框架，再点架构图，架构图应该**接着上一段的大纲画**，
    而不是让用户重敲一遍、画出一套跟框架对不上的结构。
    这条链只有把两步连起来跑才验得到，任何一步单独跑都看不出来。

另外补一个纯交互点：正文里的 [n] 徽章点下去要滚到对应 References 条目
（这是"无幻觉"最直观的呈现），之前只验了"点条目→开原文"，没验"点徽章→跳条目"。

用法：
    ./.venv/Scripts/python.exe tests/ui_write_chain.py
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = "http://127.0.0.1:8013"

IDEA = "用块稀疏注意力降低长文档检索增强生成的显存占用，同时用重排保持检索质量"


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9841 + (int(time.time()) % 60)
    profile = OUT / f"_p_wchain_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1700,1000", "about:blank"],
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

        def wait_for(expr: str, seconds: float = 40, interval: float = 0.4) -> bool:
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        def shot(name: str) -> None:
            try:
                data = call("Page.captureScreenshot", {"format": "png"}).get("data")
                if data:
                    (OUT / name).write_bytes(base64.b64decode(data))
            except Exception:
                pass

        call("Page.enable")
        call("Runtime.enable")
        # 记录所有 fetch 的请求体：要证明"架构图这一步真的带上了上一段的框架"，
        # 只看结果页看不出来——结果对不对可能是模型自己蒙对的
        call("Page.addScriptToEvaluateOnNewDocument", {"source": """
          window.__reqs = [];
          const _f = window.fetch;
          window.fetch = function(url, opt) {
            try { window.__reqs.push({url: String(url), body: (opt && opt.body) || ''}); } catch(e) {}
            return _f.apply(this, arguments);
          };
          window.__err = '';
          window.addEventListener('error', e => { window.__err += (e.message||'') + '; '; });
        """})
        call("Page.navigate", {"url": BASE + "/"})
        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 0", 60):
            res["error"] = "首页未就绪"
            return res

        # 1) 框架
        js("(async()=>{const vm=window.__sciagent; vm.centerView='write'; vm.wr.tab='outline';"
           "vm.wr.idea=%s; await new Promise(r=>setTimeout(r,300)); return 1;})()"
           % json.dumps(IDEA, ensure_ascii=False))
        clicked = js("""(()=>{const b=[...document.querySelectorAll('.write-head .el-button')]
              .find(e=>e.textContent.includes('生成框架')); if(!b) return 0; b.click(); return 1;})()""")
        if not clicked:
            res["error"] = "生成框架按钮不存在"
            return res
        t0 = time.time()
        if not wait_for("!!(window.__sciagent.wr.result || window.__sciagent.wr.error)", 240, 0.8):
            res["error"] = "框架生成超时"
            return res
        outline_wait = round(time.time() - t0, 1)

        wr = js("""(()=>{
          const vm = window.__sciagent, r = vm.wr.result;
          if (!r) return JSON.stringify({err: vm.wr.error});
          return JSON.stringify({
            title: r.title, sections: r.sections.length,
            names: r.sections.map(s=>s.name),
            refs: r.references.length, cited: r.references.filter(x=>x.cited).length,
            checks: r.checks,
            domSecs: document.querySelectorAll('.write-sec').length,
            domRefs: document.querySelectorAll('.write-ref').length,
            badges: document.querySelectorAll('.wcite').length,
          });
        })()""")
        o = json.loads(wr)
        o["wait_s"] = outline_wait
        res["steps"].append({"outline": o})
        outline_ok = bool(o.get("sections", 0) >= 3
                          and o["checks"]["refs_from_library"] == o["refs"]
                          and o["checks"]["refs_generated_by_llm"] == 0
                          and not o["checks"]["citations_invalid"]
                          and o["domSecs"] == o["sections"] and o["domRefs"] == o["refs"]
                          and o["badges"] > 0)
        shot("chain_outline.png")
        if not outline_ok:
            res["checks"] = {"outline": False}
            res["error"] = "框架/渲染不符合预期"
            return res

        # 2) 点正文里的 [n] 徽章 → 对应的 References 条目必须被高亮（滚过去）
        jumped = js("""(async()=>{
          const b = document.querySelector('.wcite');
          if (!b) return JSON.stringify({err:'没有引用徽章'});
          const n = b.getAttribute('data-i');
          const targets = [...document.querySelectorAll('.write-ref')].filter(el => {
            const t = el.querySelector('.write-ref-idx');
            return t && t.textContent.trim() === '[' + n + ']';
          });
          b.click();
          await new Promise(r=>setTimeout(r, 250));
          const hit = [...document.querySelectorAll('.write-ref')].filter(el =>
            el.classList.contains('flash'));
          const t = hit[0] ? hit[0].querySelector('.write-ref-idx') : null;
          return JSON.stringify({n, matched: targets.length, flashed: hit.length,
            flashIdx: t ? t.textContent.trim() : '', flashText: hit[0] ? hit[0].innerText.slice(0,50) : ''});
        })()""")
        j = json.loads(jumped)
        res["steps"].append({"cite_jump": j})
        jump_ok = bool(j.get("matched") == 1 and j.get("flashed") == 1
                       and j.get("flashIdx") == "[" + str(j.get("n")) + "]")
        shot("chain_cite_jump.png")

        # 3) 切到架构图 → 生成 → 请求体必须带着上一步的章节
        js("""(()=>{const t=[...document.querySelectorAll('.wtab')].find(e=>e.textContent.trim()==='架构图');
              t.click(); return 1;})()""")
        time.sleep(0.4)
        js("window.__reqs = []")   # 只留这一步的请求
        js("""(()=>{const b=[...document.querySelectorAll('.write-head .el-button')]
              .find(e=>e.textContent.includes('生成架构图')); b.click(); return 1;})()""")
        t1 = time.time()
        if not wait_for("!!(window.__sciagent.wv.topology || window.__sciagent.wv.tError)", 240, 0.8):
            res["error"] = "架构图生成超时"
            return res
        topo_wait = round(time.time() - t1, 1)

        chain = js("""(()=>{
          const vm = window.__sciagent, t = vm.wv.topology;
          const req = (window.__reqs || []).find(r => r.url.includes('/api/write/topology'));
          let body = null;
          try { body = req ? JSON.parse(req.body) : null; } catch(e) {}
          const el = document.querySelector('.topo-chart');
          const inst = (el && window.echarts) ? window.echarts.getInstanceByDom(el) : null;
          const ser = inst ? (inst.getOption().series||[])[0] : null;
          return JSON.stringify({
            err: vm.wv.tError || '',
            nodes: t ? t.nodes.length : 0, edges: t ? t.edges.length : 0,
            source: t ? t.source : '',
            hasOutline: !!(body && body.outline && body.outline.length > 20),
            outlineHead: body && body.outline ? body.outline.slice(0, 70) : '',
            outlineBody: body && body.outline ? body.outline : '',
            ideaSent: !!(body && body.idea),
            outlineKeptWr: !!(vm.wr.result && vm.wr.result.sections.length),
            drawn: ser && ser.data ? ser.data.length : 0,
            drawnLinks: ser && ser.links ? ser.links.length : 0,
          });
        })()""")
        c = json.loads(chain)
        c["wait_s"] = topo_wait
        res["steps"].append({"topology_chain": c})
        # 关键：outline 里要出现框架某一章的标题，说明前端真的把上一步传下去了。
        # 不能只看前 70 字——那一段是英文标题，章节标题在后面
        names = o["names"]
        body_text = c.get("outlineBody") or ""
        hit_names = [nm for nm in names if nm and nm[:6] in body_text]
        res["steps"].append({"outline_names_in_body": hit_names[:6]})
        chain_ok = bool(c["hasOutline"] and c["nodes"] >= 4 and c["drawn"] == c["nodes"]
                        and len(hit_names) >= 2)
        shot("chain_topology.png")

        # 4) tab 往返：切到图表再切回来，框架结果不能丢
        keep = js("""(async()=>{
          const vm = window.__sciagent;
          const t=[...document.querySelectorAll('.wtab')].find(e=>e.textContent.trim()==='数据图表');
          t.click(); await new Promise(r=>setTimeout(r,300));
          const back=[...document.querySelectorAll('.wtab')].find(e=>e.textContent.trim()==='论文框架');
          back.click(); await new Promise(r=>setTimeout(r,400));
          return JSON.stringify({sections: vm.wr.result ? vm.wr.result.sections.length : 0,
            domSecs: document.querySelectorAll('.write-sec').length,
            topoKept: !!vm.wv.topology});
        })()""")
        k = json.loads(keep)
        res["steps"].append({"tab_roundtrip": k})
        keep_ok = bool(k["sections"] >= 3 and k["domSecs"] == k["sections"] and k["topoKept"])

        res["checks"] = {"outline": outline_ok, "cite_jump": jump_ok,
                         "chain_outline_passed": chain_ok, "tab_roundtrip": keep_ok,
                         "sections": o["sections"], "refs": o["refs"],
                         "topo_nodes": c["nodes"], "topo_source": c["source"]}
        res["ok"] = bool(outline_ok and jump_ok and chain_ok and keep_ok)
        return res
    except Exception as exc:  # noqa: BLE001 把已收集的步骤带回去，别只留一句 traceback
        res["error"] = f"{type(exc).__name__}: {exc}"
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        # 每次跑都留一个几十 MB 的 Edge 配置目录，用完自己清掉
        try:
            time.sleep(0.5)
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:6000])
    raise SystemExit(0 if r.get("ok") else 1)
