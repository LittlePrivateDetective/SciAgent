"""公式渲染（KaTeX）回归 —— 修 D-66 的验收脚本。

验四件事：
1. KaTeX 的 CDN 资源真的加载了（`window.katex` 存在），不是"代码写了但没生效"；
2. 行内公式 `$...$` 渲染成 `.katex` 节点，页面上不再出现裸美元符号；
3. 块级公式 `$$...$$` 渲染成 `.katex-display`（居中独立成行）；
4. 语法错误的公式（`$\\frac{1}{$`）被标红而不是抛异常，且正文其余部分照常显示。

不调模型：直接往 Vue 实例的 messages 里塞一条带公式的回答，
这样测的是渲染链路本身，不受检索与生成波动影响。

    ./.venv/Scripts/python.exe tests/ui_math.py
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

# 一条仿真回答：行内公式、块级公式、错误公式各一处
SAMPLE = (
    "<p>多头注意力的计算复杂度是 $O(n^{2} \\cdot d)$，其中 "
    "$d_k = d_v = d_{model}/h$，且 $h=8$、$d_{model}=512$。</p>"
    "<p>$$\\mathrm{Attention}(Q,K,V)=\\mathrm{softmax}\\left(\\frac{QK^{T}}{\\sqrt{d_k}}\\right)V$$</p>"
    "<p>错误示例：$\\frac{1}{$ 后面还有正常文字。</p>"
)


def run() -> dict:
    port = 9861 + (int(time.time()) % 30)
    profile = OUT / f"_p_math_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1600,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    res: dict = {"ok": False, "checks": {}, "error": ""}
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
                time.sleep(0.3)
        page = [t for t in targets if t.get("type") == "page"][0]
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
        seq = [0]

        def call(method, params=None):
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == seq[0]:
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    return d.get("result", {})

        def js(expr: str):
            r = call("Runtime.evaluate", {
                "expression": expr, "returnByValue": True, "awaitPromise": True,
            })
            return r.get("result", {}).get("value")

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE})
        time.sleep(3.5)

        res["checks"]["katex_loaded"] = bool(js("typeof window.katex !== 'undefined'"))
        res["checks"]["autorender_loaded"] = bool(
            js("typeof window.renderMathInElement === 'function'"))
        res["checks"]["app_mounted"] = bool(js("!!window.__sciagent"))

        # 注入一条回答，等价于收到一个完成的消息
        payload = json.dumps(SAMPLE)
        js(f"window.__sciagent.messages.push({{role:'assistant', html:{payload}}}); 1")
        time.sleep(1.6)  # 等指令的 260ms 防抖 + KaTeX 渲染

        stats = js("""(() => {
          const msgs = document.querySelectorAll('.msg .md');
          const last = msgs[msgs.length - 1];
          const ps = last ? last.querySelectorAll('p') : [];
          const txt = (i) => (ps[i] ? ps[i].innerText : '');
          return {
            msg_count: msgs.length,
            total_katex: last ? last.querySelectorAll('.katex').length : 0,
            p1_katex: ps[0] ? ps[0].querySelectorAll('.katex').length : 0,
            p1_dollar: /\\$/.test(txt(0)),
            p2_display: ps[1] ? ps[1].querySelectorAll('.katex-display').length : 0,
            p3_keeps_text: txt(2).includes('后面还有正常文字'),
            p3_degraded: ps[2] ? (ps[2].querySelectorAll('.katex-error').length > 0
                                  || txt(2).includes('\\\\frac')) : false,
            p3_dollar: /\\$/.test(txt(2)),
            sw: document.documentElement.scrollWidth,
            vw: window.innerWidth,
          };
        })()""")
        res["stats"] = stats

        checks = res["checks"]
        # 正常公式：第 1 段有 4 处行内（复杂度 + d_k 定义 + h=8 + d_model=512）全部渲染
        checks["inline_rendered"] = stats["p1_katex"] == 4
        checks["inline_no_raw_dollar"] = not stats["p1_dollar"]
        # 块级公式：独立居中一块
        checks["display_rendered"] = stats["p2_display"] == 1
        # 语法错误：要么标红，要么原样保留（安静降级），且后面的正文照常显示
        checks["error_degraded_not_crash"] = stats["p3_degraded"] and stats["p3_keeps_text"]
        checks["no_horizontal_overflow"] = stats["sw"] <= stats["vw"] + 2

        shot = call("Page.captureScreenshot", {"format": "png"})
        (OUT / "ui_math.png").write_bytes(__import__("base64").b64decode(shot["data"]))

        res["ok"] = all(checks.values())
    except Exception as exc:  # noqa: BLE001
        res["error"] = repr(exc)
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


if __name__ == "__main__":
    r = run()
    print("RESULT:", "PASS" if r["ok"] else "FAIL")
    raise SystemExit(0 if r["ok"] else 1)
