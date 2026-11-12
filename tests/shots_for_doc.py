"""给技术文档 / 作品介绍 / 演示视频封面用的成品截图：一次跑完，全部取真实状态。

为什么不用手工截图：截图最容易出现"图上还是旧版本"——手动截完忘了重截。
这个脚本每次跑都是当前代码 + 当前语料的真界面，并把每张图的耗时与关键状态
一起记进 `docs/交付材料/截图/manifest.json`，方便文档里引用"这张图是哪天截的"。

输出：`docs/交付材料/截图/01_home.png` … 12_admin_overview.png（1680×1050 @2x）

用法：
    ./.venv/Scripts/python.exe tests/shots_for_doc.py
    ./.venv/Scripts/python.exe tests/shots_for_doc.py --only answer,graph
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "交付材料" / "截图"
BASE = "http://127.0.0.1:8013"

ADMIN_PWD = (os.environ.get("ADMIN_PASSWORD") or "").strip()
if not ADMIN_PWD:
    try:
        sys.path.insert(0, str(ROOT))
        from app.core.config import settings as _s  # noqa: E402

        ADMIN_PWD = (_s.admin_password or "").strip()
    except Exception:  # noqa: BLE001
        ADMIN_PWD = ""


class Browser:
    def __init__(self, scale: float = 1.5) -> None:
        port = 9861 + (int(time.time()) % 40)
        # 浏览器 profile 放系统临时目录：放项目里 Windows 会锁文件删不掉，
        # 一次跑十几个截图就是几百 MB 的垃圾（第一次就踩到了）
        self.profile = Path(tempfile.gettempdir()) / f"_p_shots_{int(time.time())}"
        self.profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             f"--user-data-dir={self.profile}", "--window-size=1680,1050", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.ws = None
        self.seq = 0
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
            raise RuntimeError("CDP 端口未就绪")
        page = next(t for t in targets if t.get("type") == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=300)
        self.call("Page.enable")
        self.call("Runtime.enable")
        # 2 倍像素密度：文档里缩放、视频里放大都不糊
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": 1680, "height": 1050, "deviceScaleFactor": scale, "mobile": False})

    def call(self, method: str, params: dict | None = None):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            d = json.loads(self.ws.recv())
            if d.get("id") == self.seq:
                if "error" in d:
                    raise RuntimeError(d["error"])
                return d.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
        return (r.get("result") or {}).get("value")

    def wait(self, expr: str, seconds: float = 40, interval: float = 0.4) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if self.js(expr):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def shot(self, name: str) -> dict:
        data = self.call("Page.captureScreenshot", {"format": "png"}).get("data")
        path = OUT / f"{name}.png"
        path.write_bytes(base64.b64decode(data))
        return {"file": path.name, "kb": round(path.stat().st_size / 1024)}

    def close(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(self.profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑指定几张，逗号分隔，如 answer,graph")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="像素密度。1.5 更清晰，但 headless 下截图偶发卡死（踩过两次），默认 1.0")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    OUT.mkdir(parents=True, exist_ok=True)

    b = Browser(scale=args.scale)
    rows: list[dict] = []

    def take(name: str, wait_expr: str = "true", settle: float = 0.8) -> None:
        if only and not any(o in name for o in only):
            return
        t0 = time.time()
        b.wait(wait_expr, 12, 0.3)
        try:
            b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 4, "y": 4})
        except Exception:
            pass
        time.sleep(settle)
        info = b.shot(name)
        row = {**info, "wait_s": round(time.time() - t0, 1), "at": time.strftime("%H:%M:%S")}
        rows.append(row)
        print(f"  [OK] {name:22s} {row['kb']:4d} KB  {row['wait_s']}s")

    try:
        # ---------- 主应用 ----------
        b.goto(BASE + "/")
        if not b.wait("window.__sciagent && window.__sciagent.docs.length > 0", 60):
            print("首页未就绪")
            return 2
        b.wait("!window.__sciagent.warmHint", 40)
        take("01_home", settle=1.2)

        # 问答：用演示里最稳的那道复杂度题（D-68/D-69 修完才答得出）
        if not only or "answer" in only:
            # 先把 Transformer 那篇打开再问：否则中间栏还是默认的中文样本，
            # 图和问题对不上，放进文档里会被追问"你这问的是哪篇"
            b.js("""(async()=>{const vm=window.__sciagent;
                  const d=vm.docs.find(x=>x.filename.startsWith('1706.03762'))||vm.docs[0];
                  vm.centerView='reader'; await vm.openDoc(d);
                  await new Promise(r=>setTimeout(r,600)); return 1;})()""")
            b.js("window.__sciagent.ask('Transformer 论文里，自注意力每层的计算复杂度是多少')")
            b.wait("!window.__sciagent.loading && window.__sciagent.messages.length > 0", 120, 0.6)
            # 回到对话顶部：要的是"问题 + 答案开头 + 引用编号"，滚到底只剩证据列表，
            # 图里会看不到问的是什么
            b.js("""(()=>{const el=document.querySelector('.chat-list')||document.querySelector('.qa-wrap');
                  if(el) el.scrollTop = 0; return 1;})()""")
            take("02_answer", settle=1.0)

        # 阅读器：定位到 Transformer 含复杂度表的那一页
        if not only or "reader" in only:
            b.js("""(async()=>{
              const vm=window.__sciagent;
              const d=vm.docs.find(x=>x.filename.startsWith('1706.03762'))||vm.docs[0];
              vm.centerView='reader'; await vm.openDoc(d);
              await new Promise(r=>setTimeout(r,800));
              return d.filename;})()""")
            b.wait("window.__sciagent.tl.loaded && window.__sciagent.pageCount>0", 60, 0.5)
            b.js("""(async()=>{const vm=window.__sciagent; vm.page=4; await vm.loadTextLayer();
                  await new Promise(r=>setTimeout(r,900)); return 1;})()""")
            take("03_reader", "window.__sciagent.tl.loaded && window.__sciagent.page===4", 1.4)

        # 中英对照
        if not only or "bilingual" in only:
            b.js("""(async()=>{const vm=window.__sciagent; vm.centerView='bilingual';
                  await vm.loadBi(true); return 1;})()""")
            take("04_bilingual", "window.__sciagent.bi.segs.length>0", 1.6)

        # 引用图谱：点中最大入度节点，右侧侧卡才有内容
        if not only or "graph" in only:
            b.js("""(async()=>{const vm=window.__sciagent; vm.centerView='graph';
                  await vm.loadGraph(false); return 1;})()""")
            b.wait("(()=>{const c=document.querySelector('.graph-canvas');if(!c||!window.echarts)return false;"
                   "const ch=window.echarts.getInstanceByDom(c);"
                   "return ch&&window.__sciagent.graph.nodes.length>20;})()", 60, 0.4)
            time.sleep(2.0)   # 力导向布局动画
            pos = b.js("""(()=>{
              const vm=window.__sciagent, c=document.querySelector('.graph-canvas');
              const ch=window.echarts.getInstanceByDom(c);
              const data=ch.getModel().getSeriesByIndex(0).getData();
              let idx=0,best=-1; vm.graph.nodes.forEach((n,i)=>{if(n.in_degree>best){best=n.in_degree;idx=i;}});
              const l=data.getItemLayout(idx);
              const xy=[l[0],l[1]], r=c.getBoundingClientRect();
              return JSON.stringify({x:r.left+xy[0], y:r.top+xy[1]});})()""")
            p = json.loads(pos)
            for t, extra in (("mouseMoved", {}), ("mousePressed", {"button": "left", "clickCount": 1}),
                             ("mouseReleased", {"button": "left", "clickCount": 1})):
                b.call("Input.dispatchMouseEvent", {"type": t, "x": p["x"], "y": p["y"], **extra})
                time.sleep(0.12)
            take("05_graph", "!!document.querySelector('.node-card')", 1.2)
            # 领域综述弹窗
            b.js("(async()=>{await window.__sciagent.genSurvey(); return 1;})()")
            take("06_survey", "window.__sciagent.survey.visible && window.__sciagent.survey.papers.length>0", 1.4)
            b.js("window.__sciagent.survey.visible=false")

        # 写作：框架 → 架构图 → 图表
        want_write = (not only) or any(k in o for o in only for k in ("write", "topology", "chart"))
        if want_write:
            b.js("""(async()=>{const vm=window.__sciagent; vm.centerView='write'; vm.wr.tab='outline';
                  vm.wr.idea='用块稀疏注意力降低长文档检索增强生成的显存占用，同时用重排保持检索质量';
                  await vm.genOutline(); return 1;})()""")
            take("07_write_outline", "!!window.__sciagent.wr.result", 1.2)

            b.js("""(async()=>{const vm=window.__sciagent; vm.wr.tab='topology';
                  vm.wv.idea='检索增强问答系统：解析、检索、校验、生成'; await vm.genTopology(); return 1;})()""")
            take("08_write_topology", "!!window.__sciagent.wv.topology", 2.0)

            b.js("""(async()=>{const vm=window.__sciagent; vm.wr.tab='chart';
                  vm.wv.csv=vm.wv.sampleCsv; vm.wv.cKind='bar'; vm.wv.cFmt='mermaid';
                  await vm.genChart(); return 1;})()""")
            take("09_write_chart", "!!window.__sciagent.wv.chart", 1.6)

        # 论文发现
        if not only or "discover" in only:
            b.js("""(async()=>{const vm=window.__sciagent; vm.centerView='reader';
                  await new Promise(r=>setTimeout(r,300)); vm.openDiscover('search');
                  vm.disc.q='retrieval augmented generation'; await vm.doSearch(); return 1;})()""")
            take("10_discover", "window.__sciagent.disc.searched && window.__sciagent.disc.results.length>0", 1.2)
            b.js("window.__sciagent.disc.visible=false")

        # ---------- 后台 ----------
        if not only or "admin" in only:
            b.goto(BASE + "/admin")
            b.wait("window.__admin && window.__admin.authReady", 45)
            if ADMIN_PWD:
                b.js("""(async()=>{const vm=window.__admin; vm.pwd=%s; await vm.doLogin();
                      vm.view='params'; return 1;})()""" % json.dumps(ADMIN_PWD))
                b.wait("window.__admin.params.length>20", 45)
                take("11_admin_params", "window.__admin.view==='params'", 1.2)
                b.js("window.__admin.view='overview'")
                take("12_admin_overview", "window.__admin.view==='overview'", 1.6)
    finally:
        b.close()

    # 与已有 manifest 合并：--only 只重拍一两张时，不要把另外十张的记录抹掉
    mf = OUT / "manifest.json"
    old_rows = {}
    if mf.exists():
        try:
            old_rows = {r["file"]: r for r in json.loads(mf.read_text(encoding="utf-8")).get("shots", [])}
        except Exception:
            old_rows = {}
    merged = {**old_rows, **{r["file"]: r for r in rows}}
    manifest = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "base": BASE,
                "viewport": "1680x1050 @1x", "shots": sorted(merged.values(), key=lambda r: r["file"])}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n共 {len(rows)} 张 -> {OUT}")
    return 0 if len(rows) >= (len(only) or 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
