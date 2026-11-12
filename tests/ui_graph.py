"""引用图谱前端交互回归：点节点 → 侧卡 → 打开原文；滚轮缩放；生成领域综述。

为什么单独写这一条：图谱是本项目唯一「全部交互都在 canvas 里」的视图，
接口测试能证明 /api/graph 返回了 38 个节点 221 条边，却证明不了
「点上去有没有反应」——ECharts 的 click 事件、roam 缩放、侧卡渲染
全在浏览器里，只有真实输入事件能验。

做法：用 CDP 的 `Input.dispatchMouseEvent` 在节点真实像素坐标上按鼠标。
坐标不是猜的：从 `getZr().storage.getDisplayList()` 里按 dataIndex 找到那个
节点图元，读它的 shape 中心，再叠上 zrender 的全局变换矩阵。
这样点下去的坐标就是用户眼睛看到的那个圆点。

用法：
    ./.venv/Scripts/python.exe tests/ui_graph.py
"""
from __future__ import annotations

import base64
import json
import shutil
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


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9831 + (int(time.time()) % 60)
    profile = OUT / f"_p_graph_{int(time.time())}"
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

        def wait_for(expr: str, seconds: float = 40, interval: float = 0.3) -> bool:
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        def shot(name: str) -> bool:
            try:
                data = call("Page.captureScreenshot", {"format": "png"}).get("data")
                if data:
                    (OUT / name).write_bytes(base64.b64decode(data))
                    return True
            except Exception:
                pass
            return False

        def mouse(kind: str, x: float, y: float, **extra) -> None:
            p = {"type": kind, "x": x, "y": y, **extra}
            call("Input.dispatchMouseEvent", p)

        call("Page.enable")
        call("Runtime.enable")
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
            "window.__err='';window.addEventListener('error',e=>{window.__err+=(e.message||'')+' @'+(e.filename||'')+':'+(e.lineno||0)+'; ';});"})
        call("Page.navigate", {"url": BASE + "/"})

        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 0", 60):
            res["error"] = "首页未就绪"
            return res

        # 1) 切到图谱视图并等图渲染出来
        js("(async()=>{const vm=window.__sciagent; vm.centerView='graph';"
           "await vm.loadGraph(false); return 1;})()")
        ready = wait_for(
            "(()=>{const vm=window.__sciagent;"
            "const c=document.querySelector('.graph-canvas');"
            "if(!c||!window.echarts) return false;"
            "const ch=window.echarts.getInstanceByDom(c);"
            "return ch && vm.graph.nodes && vm.graph.nodes.length>20 && "
            "ch.getModel().getSeriesByIndex(0).getData().count()>20;})()", 60, 0.4)
        res["steps"].append({"render": ready})
        if not ready:
            res["error"] = "图谱未渲染"
            res["steps"].append({"debug": js("JSON.stringify({err:window.__err,n:(window.__sciagent.graph.nodes||[]).length,canvas:!!document.querySelector('.graph-canvas'),ech:!!window.echarts})")})
            return res

        base = js("""(()=>{
          const vm = window.__sciagent;
          const c = document.querySelector('.graph-canvas');
          const ch = window.echarts.getInstanceByDom(c);
          const data = ch.getModel().getSeriesByIndex(0).getData();
          const r = c.getBoundingClientRect();
          return JSON.stringify({
            nodes: vm.graph.nodes.length, edges: vm.graph.edges.length,
            drawn: data.count(), canvas: [Math.round(r.width), Math.round(r.height)],
            stat: (document.querySelector('.graph-stat')||{}).textContent.trim().slice(0,60),
            legend: [...document.querySelectorAll('.legend .dot')].length,
            card: !!document.querySelector('.node-card'),
          });
        })()""")
        res["steps"].append({"base": json.loads(base)})
        b = json.loads(base)
        base_ok = (b["drawn"] == b["nodes"] and b["nodes"] > 20 and b["legend"] == 4
                   and b["canvas"][0] > 400 and b["canvas"][1] > 300 and b["card"] is False)
        shot("graph_overview.png")

        # 2) 在真实像素坐标上点一个节点 → 侧卡必须出现，且是这一篇
        #
        # 坐标怎么来的：不手算 zrender 的变换矩阵（力导向布局 + roam 组变换叠在
        # 一起，手算容易差一层），直接用 ECharts 自己的 `getItemLayout(i)`——
        # 它给的就是这个节点圆心在 canvas 里的像素坐标，跟画出来的是同一个数。
        #
        # 注意：力导向布局有 ~700ms 动画，布局坐标在动画结束前一直在变。
        # 先等它停下来再取坐标，否则读到的点是"上一帧"的位置，点下去打空。
        time.sleep(1.8)
        click_ok = False
        for attempt in range(3):
            target = js("""(()=>{
              const vm = window.__sciagent;
              const c = document.querySelector('.graph-canvas');
              const ch = window.echarts.getInstanceByDom(c);
              const data = ch.getModel().getSeriesByIndex(0).getData();
              // 挑被引最多的那篇：它一定常显标签、也一定有人点
              let idx = 0, best = -1;
              vm.graph.nodes.forEach((n, i) => { if (n.in_degree > best) { best = n.in_degree; idx = i; } });
              const l = data.getItemLayout(idx);
              const xy = Array.isArray(l) ? [l[0], l[1]] : (l ? [l.x, l.y] : null);
              if (!xy) return JSON.stringify({err: '拿不到节点布局'});
              const n = vm.graph.nodes[idx];
              const r = c.getBoundingClientRect();
              return JSON.stringify({id: n.id, label: n.label, in_degree: n.in_degree,
                index: idx, gx: xy[0], gy: xy[1], w: r.width, h: r.height,
                x: r.left + xy[0], y: r.top + xy[1]});
            })()""")
            t = json.loads(target)
            res["steps"].append({"target": t, "attempt": attempt})
            if "x" not in t:
                break
            # 点之前先确认这一像素下面确实有图形（没有就说明坐标已经过期）
            mouse("mouseMoved", t["x"], t["y"])
            time.sleep(0.2)
            hover = js("(()=>{const c=document.querySelector('.graph-canvas');"
                       "const ch=window.echarts.getInstanceByDom(c);"
                       "const h=ch.getZr().handler.findHover(%f,%f);"
                       "return h&&h.target?h.target.type:'';})()" % (t["gx"], t["gy"]))
            if not hover:
                time.sleep(0.8)
                continue
            mouse("mousePressed", t["x"], t["y"], button="left", clickCount=1)
            time.sleep(0.12)
            mouse("mouseReleased", t["x"], t["y"], button="left", clickCount=1)
            # 等"侧卡已经渲染出来"再读，光等 selectedNode 会读到 v-if 还没画完的那一帧
            clicked = wait_for("!!window.__sciagent.selectedNode && "
                               "!!document.querySelector('.node-card')", 6)
            card = js("""(()=>{
              const vm = window.__sciagent;
              const el = document.querySelector('.node-card');
              return JSON.stringify({
                selected: vm.selectedNode && vm.selectedNode.id,
                label: vm.selectedNode && vm.selectedNode.label,
                visible: !!el && el.offsetHeight > 0,
                text: el ? el.innerText.replace(/\\s+/g,' ').slice(0,120) : '',
                btn: !!(el && [...el.querySelectorAll('button')].find(b=>b.textContent.includes('打开这篇'))),
              });
            })()""")
            cd = json.loads(card)
            res["steps"].append({"click": clicked, "card": cd, "hover": hover})
            # 三个条件缺一不可：侧卡出现、写的就是点中的这篇、卡上有「打开这篇」
            click_ok = bool(clicked and cd["selected"] == t["id"] and cd["visible"]
                            and cd["btn"] and str(t["in_degree"]) in cd["text"])
            if click_ok:
                break
            time.sleep(0.8)
        shot("graph_node_card.png")

        # 3) 滚轮缩放：roam 必须真的动（group.scaleX 变化）
        zoom_before = js("""(()=>{
          const c = document.querySelector('.graph-canvas');
          const ch = window.echarts.getInstanceByDom(c);
          const g = ch.getViewOfSeriesModel(ch.getModel().getSeriesByIndex(0)).group;
          const r = c.getBoundingClientRect();
          return JSON.stringify({scale: g.scaleX, x: r.left+r.width/2, y: r.top+r.height/2});
        })()""")
        zb = json.loads(zoom_before)
        mouse("mouseWheel", zb["x"], zb["y"], deltaX=0, deltaY=-260)
        time.sleep(0.8)
        z_after = js("""(()=>{
          const c = document.querySelector('.graph-canvas');
          const ch = window.echarts.getInstanceByDom(c);
          const g = ch.getViewOfSeriesModel(ch.getModel().getSeriesByIndex(0)).group;
          return g.scaleX;
        })()""")
        z = {"before": zb["scale"], "after": z_after}
        res["steps"].append({"zoom": z})
        zoom_ok = bool(z_after and z["after"] > z["before"] * 1.02)

        # 4) 「打开这篇」→ 真的进阅读器，且打开的是这一个 doc
        o = {"view": "", "doc": "", "want": ""}
        if click_ok:
            opened = js("""(async()=>{
              const vm = window.__sciagent;
              const want = vm.selectedNode.id;
              const el = document.querySelector('.node-card');
              const btn = [...el.querySelectorAll('button')].find(b=>b.textContent.includes('打开这篇'));
              btn.click();
              await new Promise(r=>setTimeout(r, 2500));
              return JSON.stringify({view: vm.centerView, doc: vm.activeDocId, want,
                title: (vm.activeDoc && vm.activeDoc.filename) || ''});
            })()""")
            o = json.loads(opened)
        res["steps"].append({"open_doc": o})
        open_ok = bool(o["view"] == "reader" and o["doc"] == o["want"])

        # 5) 生成领域综述：弹窗、正文、可点的文献条目
        js("(async()=>{const vm=window.__sciagent; vm.centerView='graph';"
           "await new Promise(r=>setTimeout(r,400)); await vm.genSurvey(); return 1;})()")
        survey_ready = wait_for("window.__sciagent.survey.visible && "
                                "window.__sciagent.survey.papers.length > 0", 150, 0.6)
        sv = js("""(()=>{
          const vm = window.__sciagent;
          const dlg = document.querySelector('.el-dialog');
          const body = document.querySelector('.survey-body');
          const refs = document.querySelectorAll('.survey-refs .evid-item');
          return JSON.stringify({
            visible: vm.survey.visible, papers: vm.survey.papers.length,
            htmlLen: (vm.survey.html||'').length,
            dialogShown: !!dlg && dlg.offsetHeight > 0,
            bodyLen: body ? body.innerText.length : 0,
            refRows: refs.length,
            first: (refs[0] && refs[0].innerText || '').slice(0,50),
          });
        })()""")
        s = json.loads(sv)
        res["steps"].append({"survey": s})
        survey_ok = bool(survey_ready and s["dialogShown"] and s["bodyLen"] > 120
                         and s["refRows"] == s["papers"] and s["papers"] >= 1)
        shot("graph_survey.png")

        # 6) 综述里点一条文献 → 打开原文（条目必须指向库内真实 doc）
        ro = {"view": "", "doc": "", "want": ""}
        if survey_ok:
            ref_open = js("""(async()=>{
              const vm = window.__sciagent;
              const want = vm.survey.papers[0].doc_id;
              const row = document.querySelector('.survey-refs .evid-item');
              row.click();
              await new Promise(r=>setTimeout(r, 2500));
              return JSON.stringify({view: vm.centerView, doc: vm.activeDocId, want});
            })()""")
            ro = json.loads(ref_open)
        res["steps"].append({"survey_ref_open": ro})
        ref_ok = bool(ro["view"] == "reader" and ro["doc"] == ro["want"])

        res["checks"] = {"render": True, "base": base_ok, "node_click": click_ok,
                         "zoom": zoom_ok, "open_doc": open_ok, "survey": survey_ok,
                         "survey_ref": ref_ok,
                         "nodes": b["nodes"], "edges": b["edges"],
                         "papers": s["papers"]}
        res["ok"] = bool(base_ok and click_ok and zoom_ok and open_ok and survey_ok and ref_ok)
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        # 每次跑都留一个几十 MB 的 Edge 配置目录，攒起来很占地方；用完自己清掉
        try:
            time.sleep(0.5)
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:5000])
    raise SystemExit(0 if r.get("ok") else 1)
