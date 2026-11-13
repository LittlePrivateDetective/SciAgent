"""手机版布局回归：390×844 下不能横向溢出，三栏改成「一栏 + 底部切换」。

要验的不是"有个 media query"，而是四件在真机上会立刻穿帮的事：
1. 整页不能左右拖（原来 260+1fr+380 三栏硬挤，手机上必然溢出）；
2. 一次只显示一栏，底部导航能切，切过去那一栏真的是 flex 显示；
3. 阅读器页图要落在视口内（原来页图按原始像素铺，手机上要横着拖）；
4. 抽屉/弹窗不能按 640/720px 的桌面宽度弹出来（Element 用的是行内 width，
   只能在媒体查询里 !important 盖掉，这里就是验这行有没有生效）。

用法：
    ./.venv/Scripts/python.exe tests/ui_mobile.py
    SCIAGENT_BASE="https://sciagent-research.app.workbuddy.host/?sharecode=CHBCOXLVVO" \
        ./.venv/Scripts/python.exe tests/ui_mobile.py     # 直接测公网
证据：docs/qa/evidence/mobile_*.png
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = __import__("os").environ.get("SCIAGENT_BASE", "http://127.0.0.1:8013")


def entry_url() -> str:
    """把 base 拼成可直接打开的首页地址。

    base 可能带查询串（公网实例必须带 sharecode），直接 `BASE + "/"` 会拼成
    `...?sharecode=xxx/` —— 分享码后面多了个斜杠，平台判为无效，页面 403。
    """
    from urllib.parse import urlsplit, urlunsplit

    u = urlsplit(BASE)
    path = u.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((u.scheme, u.netloc, path, u.query, ""))
W, H = 390, 844          # iPhone 14/15 的 CSS 像素尺寸


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9851 + (int(time.time()) % 40)
    profile = Path(tempfile.gettempdir()) / f"_p_mobile_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "about:blank"],
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
            ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == seq[0]:
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(expr: str):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return (r.get("result") or {}).get("value")

        def wait(expr: str, seconds: float = 40, interval: float = 0.35) -> bool:
            end = time.time() + seconds
            while time.time() < end:
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
        call("Emulation.setDeviceMetricsOverride",
             {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": True})
        call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        call("Page.navigate", {"url": entry_url()})
        if not wait("window.__sciagent && window.__sciagent.docs.length > 0", 60):
            res["error"] = "首页未就绪"
            return res
        time.sleep(1.2)

        # 1) 不横向溢出 + 导航存在
        base = js("""(()=>{
          const de=document.documentElement, b=document.body;
          const nav=document.querySelector('.mnav');
          const btns=[...document.querySelectorAll('.mnav-btn')];
          return JSON.stringify({
            vw: window.innerWidth, vh: window.innerHeight,
            scrollW: Math.max(de.scrollWidth, b.scrollWidth),
            navShown: !!nav && getComputedStyle(nav).display !== 'none',
            navH: nav ? Math.round(nav.getBoundingClientRect().height) : 0,
            labels: btns.map(x => (x.querySelector('.mnav-t')||x).textContent.trim()),
            pane: window.__sciagent.mobilePane,
            right: getComputedStyle(document.querySelector('.panel.right')).display,
            left: getComputedStyle(document.querySelector('.panel.left')).display,
            center: getComputedStyle(document.querySelector('.panel.center')).display,
          });
        })()""")
        b = json.loads(base)
        res["steps"].append({"base": b})
        no_overflow = b["scrollW"] <= b["vw"] + 1
        nav_ok = b["navShown"] and b["navH"] >= 50 and len(b["labels"]) == 3
        single_pane = (b["pane"] == "qa" and b["right"] != "none"
                       and b["left"] == "none" and b["center"] == "none")
        shot("mobile_qa.png")

        # 2) 切到文献库 → 只显示左栏
        js("""(()=>{const b=[...document.querySelectorAll('.mnav-btn')]
              .find(x=>x.textContent.includes('文献库')); b.click(); return 1;})()""")
        time.sleep(0.6)
        docs = js("""(()=>{
          const de=document.documentElement;
          const left=document.querySelector('.panel.left');
          // 量整行（含 padding），手指能点到的范围是 .doc-item 而不是里面那层
          const first=document.querySelector('.doc-item');
          const r=first?first.getBoundingClientRect():null;
          return JSON.stringify({
            display: getComputedStyle(left).display,
            pane: window.__sciagent.mobilePane,
            scrollW: de.scrollWidth, vw: window.innerWidth,
            items: document.querySelectorAll('.doc-item').length,
            rowH: r?Math.round(r.height):0,
            on: [...document.querySelectorAll('.mnav-btn')].findIndex(x=>x.classList.contains('on')),
          });
        })()""")
        d = json.loads(docs)
        res["steps"].append({"docs": d})
        docs_ok = (d["display"] != "none" and d["pane"] == "docs"
                   and d["scrollW"] <= d["vw"] + 1 and d["items"] > 10
                   and d["rowH"] >= 44 and d["on"] == 0)
        shot("mobile_docs.png")

        # 3) 点第一篇 → 切到阅读器，页图必须在视口内
        js("""(()=>{document.querySelector('.doc-item .doc-main').click(); return 1;})()""")
        wait("window.__sciagent.mobilePane === 'reader' && !!document.querySelector('.pdf-page')", 30)
        # 必须等页图真的解码完（complete && naturalWidth>0）：
        # 图片还在加载时 getBoundingClientRect 拿到的宽度是原始尺寸或 0，
        # 断言就会随机失败（实测同一版本跑两次，一次 reader_fit=true 一次 false）。
        wait("(()=>{const i=document.querySelector('.pdf-page');"
             " return !!(i && i.complete && i.naturalWidth > 0)})()", 25)
        time.sleep(0.6)
        reader = js("""(()=>{
          const vm=window.__sciagent, de=document.documentElement;
          const img=document.querySelector('.pdf-page');
          const ir=img?img.getBoundingClientRect():null;
          const pane=document.querySelector('.panel.center');
          return JSON.stringify({
            pane: vm.mobilePane, display: getComputedStyle(pane).display,
            scrollW: de.scrollWidth, vw: window.innerWidth,
            imgW: ir?Math.round(ir.width):0, imgLeft: ir?Math.round(ir.left):0,
            stageScrollW: (()=>{const s=document.querySelector('.page-stage');
              return s?Math.round(s.scrollWidth):0;})(),
            title: (document.querySelector('.panel.center .panel-head small')||{}).textContent||'',
          });
        })()""")
        r = json.loads(reader)
        res["steps"].append({"reader": r})
        reader_ok = (r["pane"] == "reader" and r["display"] != "none"
                     and r["imgW"] <= r["vw"] and r["imgLeft"] >= -1
                     and r["scrollW"] <= r["vw"] + 1
                     and r["stageScrollW"] <= r["vw"] + 1)
        shot("mobile_reader.png")

        # 4) 抽屉：发现页按视口宽弹出（桌面是 640px，手机上必须缩）
        js("""(()=>{window.__sciagent.openDiscover('search'); return 1;})()""")
        time.sleep(1.0)
        drawer = js("""(()=>{
          const el=document.querySelector('.el-drawer');
          const r=el?el.getBoundingClientRect():null;
          // 搜索行单独量一次：输入框 width:100% 会把「搜索」按钮顶出可视区，
          // 只看抽屉宽度是发现不了的
          const row=document.querySelector('.disc-search');
          const kids=row?[...row.children].map(x=>{const b=x.getBoundingClientRect();
            return {cls:x.className.split(' ')[0], l:Math.round(b.left), w:Math.round(b.width)};}):[];
          return JSON.stringify({shown: !!el && (r?r.width:0) > 0,
            w: r?Math.round(r.width):0, vw: window.innerWidth,
            searchRow: kids,
            overflow: document.documentElement.scrollWidth > window.innerWidth + 1});
        })()""")
        dr = json.loads(drawer)
        res["steps"].append({"drawer": dr})
        # 搜索行必须有 2 个子节点（输入框 + 按钮）。曾经这里只有 1 个：
        # <el-input ... /> 在浏览器内模板里是"未闭合"的，把后面的按钮吞成了子节点，
        # 按钮从来没渲染过，而只看抽屉宽度是发现不了的。
        drawer_ok = bool(dr["shown"] and dr["w"] <= dr["vw"] and not dr["overflow"]
                         and len(dr.get("searchRow") or []) >= 2)
        shot("mobile_drawer.png")
        js("window.__sciagent.disc.visible=false")

        # 5) 回问答栏，示例问题要能点（触屏上 44px 的可点高度）
        js("""(()=>{const b=[...document.querySelectorAll('.mnav-btn')]
              .find(x=>x.textContent.includes('问答')); b.click(); return 1;})()""")
        time.sleep(0.5)
        qa = js("""(()=>{
          const vm=window.__sciagent;
          const s=document.querySelector('.sample-q');
          const r=s?s.getBoundingClientRect():null;
          return JSON.stringify({pane: vm.mobilePane, samples: document.querySelectorAll('.sample-q').length,
            sampleH: r?Math.round(r.height):0, overflow: document.documentElement.scrollWidth > window.innerWidth+1});
        })()""")
        q = json.loads(qa)
        res["steps"].append({"qa": q})
        qa_ok = q["pane"] == "qa" and q["samples"] >= 3 and q["sampleH"] >= 34 and not q["overflow"]

        res["checks"] = {"no_horizontal_overflow": no_overflow, "nav": nav_ok,
                         "single_pane": single_pane, "docs": docs_ok,
                         "reader_fit": reader_ok, "drawer_fit": drawer_ok, "qa": qa_ok,
                         "viewport": f"{b['vw']}x{b['vh']}"}
        res["ok"] = all([no_overflow, nav_ok, single_pane, docs_ok, reader_ok, drawer_ok, qa_ok])
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, ensure_ascii=False, indent=2)[:5000])
    raise SystemExit(0 if out.get("ok") else 1)
