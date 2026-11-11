"""写作助手 1.6.2 / 1.6.3 前端回归：架构拓扑图 + 数据图表与图注。

断言的重点不是"页面上有东西"，而是三件容易被漂亮界面掩盖的事：

1. **四种格式描述的是同一张图**——节点数、边数在四个格式里必须一致，
   否则用户复制 TikZ 出来会发现图跟预览长得不一样；
2. **Mermaid 真的渲染出来了**（<svg> 有节点文字），不是只显示了一段代码；
3. **图注里的数字能回查原表**——checks.foreign 必须为空，这是"不编数"的机械化兑现。
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

IDEA = "用块稀疏注意力降低长文档 RAG 的显存占用，同时用重排保持检索质量"
CSV = "Model,BLEU,Params\nTransformer,28.4,65\nBERT,34.5,110\nGPT-3,41.0,175"


def run(url: str = BASE + "/") -> dict:
    port = 9600 + (int(time.time()) % 150)
    profile = OUT / f"_p_viz_{int(time.time())}"
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
                raise RuntimeError(str(r["exceptionDetails"])[:400])
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
        # CDN 脚本报错在跨域下只会是 "Script error."，页内错误必须自己接管
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
             "window.__errs=[];window.addEventListener('error',e=>{"
             "window.__errs.push(String((e&&e.message)||'')+' @'+(e&&e.filename||''));});"})
        call("Page.navigate", {"url": url})
        if not wait_for("!!(window.__sciagent && window.__sciagent.info)", 60):
            res["error"] = "主界面未就绪"
            return res

        js("""(()=>{const vm=window.__sciagent; vm.centerView='write'; vm.onViewChange('write'); return 1;})()""")
        time.sleep(0.6)
        tabs = js("[...document.querySelectorAll('.wtab')].map(e=>e.textContent.trim())")
        res["steps"].append({"tabs": tabs})
        if tabs != ["论文框架", "架构图", "数据图表"]:
            res["error"] = f"三个 tab 未渲染：{tabs}"
            return res

        # ---------------- 1.6.2 架构拓扑图 ----------------
        clicked = js("""(()=>{const t=[...document.querySelectorAll('.wtab')].find(e=>e.textContent.trim()==='架构图');
              if(!t) return 0; t.click(); return 1;})()""")
        time.sleep(0.4)
        js("window.__sciagent.wv.idea = %s" % json.dumps(IDEA, ensure_ascii=False))
        time.sleep(0.2)
        # 走真实按钮，顺带验证按钮在 tab 内的绑定点到了正确的方法
        btn = js("""(()=>{const b=[...document.querySelectorAll('.write-head .el-button')]
              .find(e=>e.textContent.includes('生成架构图'));
              if(!b) return 0; b.click(); return 1;})()""")
        if not btn:
            res["error"] = "架构图生成按钮不存在"
            return res
        t0 = time.time()
        if not wait_for("!!(window.__sciagent.wv.topology || window.__sciagent.wv.tError)", 180, 1.0):
            res["error"] = "架构图生成超时"
            return res
        topo_wait = round(time.time() - t0, 1)

        topo = js("""(()=>{
          const vm = window.__sciagent, t = vm.wv.topology;
          if (!t) return {err: vm.wv.tError};
          const codes = {};
          for (const k of Object.keys(t.formats)) codes[k] = t.formats[k].code;
          // 预览是 ECharts 分层图：直接查实例里实际画了多少节点/边，
          // 这比"页面上有 svg"更硬——画少了说明布局丢了数据
          const el = document.querySelector('.topo-chart');
          const inst = (el && window.echarts) ? window.echarts.getInstanceByDom(el) : null;
          const opt = inst ? inst.getOption() : null;
          const ser = opt && opt.series ? opt.series[0] : null;
          return {
            err: '', title: t.title, source: t.source,
            checks: JSON.parse(JSON.stringify(t.checks)),
            nodes: t.nodes.length, edges: t.edges.length, groups: JSON.parse(JSON.stringify(t.groups)),
            codeKeys: Object.keys(codes),
            codeLen: Object.fromEntries(Object.entries(codes).map(([k,v])=>[k, v.length])),
            // 四种格式里同一批节点 id 必须都出现，否则格式之间的图不一致
            idHits: Object.fromEntries(Object.entries(codes).map(
              ([k,v])=>[k, t.nodes.filter(n=>v.includes(n.id)).length])),
            canvas: !!(el && el.querySelector('canvas')),
            drawn: ser && ser.data ? ser.data.length : 0,
            drawnLinks: ser && ser.links ? ser.links.length : 0,
            chartH: el ? el.getBoundingClientRect().height : 0,
            chars: Object.fromEntries(Object.entries(codes).map(([k,v])=>[k, v.slice(0,60)])),
          };
        })()""")
        topo["wait_s"] = topo_wait
        res["steps"].append({"topology": topo})

        # 切到每一种格式，代码必须跟着换（不是只改了个高亮）
        fmt_probe = js("""(async()=>{
          const vm = window.__sciagent, out = {};
          const label = {mermaid:'Mermaid', dot:'Graphviz', tikz:'TikZ', matplotlib:'Matplotlib'};
          for (const k of ['mermaid','dot','tikz','matplotlib']) {
            const el = [...document.querySelectorAll('.viz-fmt')].find(e=>e.textContent.trim()===label[k]);
            if (!el) { out[k] = {miss:true}; continue; }
            el.click();
            await new Promise(r=>setTimeout(r,150));
            const pre = document.querySelector('.viz-pre');
            out[k] = { clicked:true, len: pre?pre.textContent.length:0,
                       content: pre ? pre.textContent : '' };
          }
          return out;
        })()""")
        res["steps"].append({"fmt_switch": {k: {"len": v.get("len"), "miss": v.get("miss", False)}
                                          for k, v in (fmt_probe or {}).items()}})

        # 截图：回到 Mermaid 查看效果
        js("""(()=>{const el=[...document.querySelectorAll('.viz-fmt')].find(e=>e.textContent.trim()==='Mermaid');
              if(el) el.click(); return 1;})()""")
        time.sleep(0.8)
        shot = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}).get("data")
        if shot:
            (OUT / "viz_topology.png").write_bytes(base64.b64decode(shot))

        # 预览区单独来一张：写视图中栏窄，整页截图里图被压到看不清
        js("""(()=>{const e=document.querySelector('.topo-chart');
              if(e) e.scrollIntoView({block:'center'}); return 1;})()""")
        time.sleep(0.6)
        rect = js("""(()=>{const e=document.querySelector('.topo-chart'); if(!e) return null;
              const r=e.getBoundingClientRect();
              return {x:Math.max(0,r.x), y:Math.max(0,r.y), width:r.width, height:r.height};})()""")
        if rect and rect.get("width", 0) > 50:
            shot = call("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True,
                "clip": {**rect, "scale": 1},
            }).get("data")
            if shot:
                (OUT / "viz_topology_zoom.png").write_bytes(base64.b64decode(shot))

        # ---------------- 1.6.3 数据图表 + 图注 ----------------
        js("""(()=>{const t=[...document.querySelectorAll('.wtab')].find(e=>e.textContent.trim()==='数据图表');
              t.click(); return 1;})()""")
        time.sleep(0.4)
        js("(()=>{const vm=window.__sciagent; vm.wv.csv=%s; vm.wv.cKind='bar'; vm.wv.cFmt='mermaid';"
           "vm.wv.cTitle='BLEU and parameter comparison'; return 1;})()" % json.dumps(CSV))
        time.sleep(0.3)
        btn2 = js("""(()=>{const b=[...document.querySelectorAll('.write-head .el-button')]
              .find(e=>e.textContent.includes('生成图表'));
              if(!b) return 0; b.click(); return 1;})()""")
        if not btn2:
            res["error"] = "图表生成按钮不存在"
            return res
        t0 = time.time()
        if not wait_for("!!(window.__sciagent.wv.chart || window.__sciagent.wv.cError)", 180, 1.0):
            res["error"] = "图表生成超时"
            return res
        chart_wait = round(time.time() - t0, 1)

        chart = js("""(()=>{
          const vm = window.__sciagent, c = vm.wv.chart;
          if (!c) return {err: vm.wv.cError};
          const svg = document.querySelector('.viz-prev svg');
          const cap = c.caption || {};
          const table = document.querySelector('.cap-card');
          return {
            err:'', format:c.format, kind:c.kind, rows:c.rows, columns:c.columns,
            labels: c.labels,
            codeHead: (c.code||'').slice(0,50),
            valuesInCode: ['28.4','34.5','41.0','65','110','175'].filter(v=>String(c.code).includes(v)),
            svg: !!svg,
            capSource: cap.source, capZh: cap.zh || '', capEn: cap.en || '',
            capTrend: cap.trend_zh || '',
            capChecks: cap.checks ? JSON.parse(JSON.stringify(cap.checks)) : null,
            capCardDom: !!table,
            trendDom: !!document.querySelector('.cap-trend'),
            warnDom: (document.querySelector('.cap-warn')||{}).textContent || '',
            metaDom: [...document.querySelectorAll('.viz-meta .wc-item')].map(e=>e.textContent.trim()),
          };
        })()""")
        chart["wait_s"] = chart_wait
        res["steps"].append({"chart": chart})

        shot = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}).get("data")
        if shot:
            (OUT / "viz_chart.png").write_bytes(base64.b64decode(shot))

        # 表格类型：kind 一换，format 必须自动落到 latex（Mermaid 画不了表）
        tbl = js("""(async()=>{
          const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));
          const vm = window.__sciagent;
          vm.wv.cKind='table'; vm.syncChartFmt();
          await sleep(80);
          const fmtAfterKind = vm.wv.cFmt;

          vm.genChart();
          for (let i=0;i<200;i++){ await sleep(150);
            if(!vm.wv.cLoading && vm.wv.chart && vm.wv.chart.format==='latex') break; }
          const latexCode = (document.querySelector('.viz-pre')||{textContent:''}).textContent;

          vm.wv.cFmt='markdown'; vm.genChart();
          for (let i=0;i<200;i++){ await sleep(150);
            if(!vm.wv.cLoading && vm.wv.chart && vm.wv.chart.format==='markdown') break; }
          await sleep(400);   // 等 mdTableHtml 的 v-html 落地
          const t = document.querySelector('.viz-table');
          return {
            fmtAfterKind,
            latexHasToprule: latexCode.includes('\\\\toprule') && latexCode.includes('\\\\bottomrule'),
            mdCodeHead: (vm.wv.chart.code||'').slice(0,40),
            tableRows: t ? t.querySelectorAll('tr').length : 0,
            tableCells: t ? t.querySelectorAll('td').length : 0,
            ths: t ? [...t.querySelectorAll('th')].map(e=>e.textContent) : [],
          };
        })()""")
        res["steps"].append({"table": tbl})

        # 雷达图：Mermaid 不支持时必须给出明确提示而不是空白
        radar = js("""(async()=>{
          const vm = window.__sciagent;
          vm.wv.cKind='radar'; vm.syncChartFmt();
          const fmt = vm.wv.cFmt;
          const opts = [...document.querySelectorAll('.el-select__wrapper, .el-input__inner')].length;
          vm.genChart();
          for (let i=0;i<160 && vm.wv.cLoading;i++) await new Promise(r=>setTimeout(r,150));
          const c = vm.wv.chart;
          return {
            fmt, opts,
            ok: !vm.wv.cError, err: vm.wv.cError,
            hasPolar: String(c && c.code).includes('polar'),
            hintDom: (document.querySelector('.viz-prev-hint')||{}).textContent||'',
            cap: c && c.caption ? c.caption.source : '',
          };
        })()""")
        res["steps"].append({"radar": radar})

        errs = js("window.__errs || []") or []
        res["steps"].append({"page_errors": errs})

        # ---------------- 判定 ----------------
        f_ok = (topo.get("err") == "" and 4 <= topo.get("nodes", 0) <= 14
                and topo.get("edges", 0) >= 3
                # 画出来的节点/边必须与数据结构一一对应，少一个都算预览失真
                and topo.get("canvas") and topo.get("drawn") == topo.get("nodes")
                and topo.get("drawnLinks") == topo.get("edges")
                and topo.get("chartH", 0) >= 220)
        # 四格式一致性：每个格式里出现的节点 id 数必须等于节点总数
        ids_ok = all(v == topo.get("nodes") for v in (topo.get("idHits") or {}).values())
        probe = fmt_probe or {}
        def code_of(k: str) -> str:
            return (probe.get(k) or {}).get("content") or ""

        # 每个格式的"签名"必须真实存在于代码里：只改高亮不改内容的话这里过不了
        switch_ok = ("flowchart" in code_of("mermaid")
                     and "digraph" in code_of("dot")
                     and "\\begin{tikzpicture}" in code_of("tikz")
                     and "import matplotlib" in code_of("matplotlib"))
        c_ok = (chart.get("err") == "" and chart.get("format") == "mermaid"
                and chart.get("svg") and len(chart.get("valuesInCode", [])) == 6
                and chart.get("capCardDom") and len(chart.get("capZh", "")) > 40
                and len(chart.get("capEn", "")) > 40)
        # 图注里出现表外数字时 capChecks.foreign 非空，这一项就必须挂
        num_ok = bool(chart.get("capChecks")) and not chart["capChecks"].get("foreign")
        t_ok = (tbl.get("fmtAfterKind") == "latex" and tbl.get("tableRows") == 4
                and tbl.get("tableCells") == 9 and "|" in tbl.get("mdCodeHead", ""))
        r_ok = radar.get("ok") and radar.get("hasPolar") and "polaraxis" not in radar.get("hintDom", "")
        err_ok = not errs

        res["steps"].append({
            "topology_formats": "PASS" if (f_ok and ids_ok and switch_ok) else "FAIL",
            "chart_caption": "PASS" if (c_ok and num_ok) else "FAIL",
            "table": "PASS" if t_ok else "FAIL",
            "radar": "PASS" if r_ok else "FAIL",
            "no_page_error": "PASS" if err_ok else "FAIL",
        })
        res["ok"] = bool(f_ok and ids_ok and switch_ok and c_ok and num_ok and t_ok and r_ok and err_ok)
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
    # 完整结果落盘：终端只打摘要，细节回 json 里查
    (OUT / "viz_result.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "steps"}, ensure_ascii=False, indent=1))
    for s in r.get("steps", []):
        if any(str(v).startswith(("PASS", "FAIL")) for v in s.values()):
            print(s)
    sys.exit(0 if r["ok"] else 1)
