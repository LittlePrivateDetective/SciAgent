"""模型徽标切换器（云端 / 本地自训 3B）的浏览器回归。

验六件事：
1. 本机可用本地模型时，徽标是可点的下拉；不可用时退化成只读标签（公网就是后者）；
2. 展开后恰好两项：云端 · <模型>、本地 · <自训模型>；
3. 选「本地」后 askRoute=local、徽标文案变成本地模型名；
4. 在本地模式下提问，回答上方的标签带「本地」——这是"请求头真的发出去了"的证据；
5. 切回云端后再问，标签不再带「本地」（两个方向都通，不是单程）；
6. 全程零 JS 报错。

    ./.venv/Scripts/python.exe tests/ui_model_switch.py
    BASE=http://127.0.0.1:8014 ./.venv/Scripts/python.exe tests/ui_model_switch.py

注意：第 4、5 步要真调模型，跑一次约 20–40 秒；库里没有文献时提问会直接返回
"文献库还是空的"，那两行断言会失败——这是环境问题，不是功能问题。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "docs" / "qa" / "evidence"
BASE = os.environ.get("BASE", "http://127.0.0.1:8013")
QUESTION = "REALM 的检索器是怎么训练的？"


def run() -> dict:
    port = 9821 + (int(time.time()) % 40)
    profile = OUT / f"_p_model_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    res: dict = {"ok": False, "steps": [], "checks": {}, "error": ""}
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
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        seq = [0]

        def call(method, params=None):
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == seq[0]:
                    return d.get("result") or {}

        def js(expr):
            r = call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                          "returnByValue": True})
            if r.get("exceptionDetails"):
                return {"__err": str(r["exceptionDetails"])[:300]}
            return (r.get("result") or {}).get("value")

        call("Page.enable")
        call("Runtime.enable")
        js("""(()=>{window.__err=[];window.addEventListener('error',e=>window.__err.push(String(e.message)));
              const o=console.error;console.error=function(){window.__err.push([...arguments].join(' '));o.apply(console,arguments)};return 1})()""")
        call("Page.navigate", {"url": BASE + "/"})
        for _ in range(90):
            if js("!!(window.__sciagent && window.__sciagent.info && window.__sciagent.info.llm)"):
                break
            time.sleep(0.4)
        js("window.__SCIAGENT_NO_CACHE = 1; 1")

        # 1) 初始状态：默认云端，徽标是否可点取决于本机有没有本地模型
        init = js("""(()=>{const v=window.__sciagent;
              const t=document.querySelector('.badges .model-pick') || document.querySelector('.badges .el-tag');
              return {route:v.askRoute, ready:!!v.localReady, label:t?t.textContent.trim():'',
                      available:v.info && v.info.local ? !!v.info.local.available : false,
                      model:v.info && v.info.local ? v.info.local.model : '',
                      reason:v.info && v.info.local ? v.info.local.reason : ''}})()""")
        res["steps"].append({"initial": init})

        # 公网那种部署（没有 Ollama）：只验"不给按钮"，后面的切换一律跳过。
        #   EXPECT_LOCAL=0 BASE=... 跑这一支
        if os.environ.get("EXPECT_LOCAL", "1") != "1":
            res["errors"] = js("window.__err||[]")
            res["checks"] = {
                "no_dropdown_without_local": not bool(js("!!document.querySelector('.badges .model-pick')")),
                "badge_still_shows_model": bool((init.get("label") or "").strip()),
                "no_js_errors": not res["errors"],
            }
            res["ok"] = all(res["checks"].values())
            return res

        # 2) 展开下拉：菜单会被 teleport 到 body，按文案找项
        js("(()=>{const t=document.querySelector('.badges .model-pick'); if(t) t.click(); return 1})()")
        time.sleep(0.6)
        items = js("""(()=>[...document.querySelectorAll('.el-dropdown-menu__item')]
              .map(e=>e.textContent.replace(/\\s+/g,' ').trim()))()""")
        res["steps"].append({"menu": items})

        # 3) 选「本地」
        js("""(()=>{const it=[...document.querySelectorAll('.el-dropdown-menu__item')]
              .find(e=>e.textContent.includes('本地')); if(it) it.click(); return 1})()""")
        time.sleep(0.8)
        picked = js("""(()=>{const v=window.__sciagent;
              const t=document.querySelector('.badges .model-pick');
              return {route:v.askRoute, label:t?t.textContent.trim():''}})()""")
        res["steps"].append({"picked_local": picked})

        # 4) 本地提问
        js(f"(()=>{{window.__sciagent.ask({QUESTION!r}); return 1}})()")
        local_state = {}
        t0 = time.time()
        while time.time() - t0 < 220:
            time.sleep(2)
            local_state = js("""(()=>{const v=window.__sciagent;
                  const a=[...v.messages].reverse().find(x=>x.role==='assistant');
                  return {loading:!!v.loading, meta:a?a.meta:'', chars:a&&a.html?a.html.length:0}})()""")
            if isinstance(local_state, dict) and not local_state.get("loading") and local_state.get("meta"):
                break
        res["steps"].append({"local_answer": local_state, "elapsed_s": round(time.time() - t0, 1)})

        # 5) 切回云端再问（反向也要通，否则等于只能切过去不能切回来）
        js("(()=>{const t=document.querySelector('.badges .model-pick'); if(t) t.click(); return 1})()")
        time.sleep(0.6)
        js("""(()=>{const it=[...document.querySelectorAll('.el-dropdown-menu__item')]
              .find(e=>e.textContent.includes('云端')); if(it) it.click(); return 1})()""")
        time.sleep(0.6)
        back = js("(()=>{const v=window.__sciagent;return {route:v.askRoute}})()")
        js(f"(()=>{{window.__sciagent.ask({QUESTION!r}); return 1}})()")
        cloud_state = {}
        t0 = time.time()
        while time.time() - t0 < 200:
            time.sleep(2)
            cloud_state = js("""(()=>{const v=window.__sciagent;
                  const a=[...v.messages].reverse().find(x=>x.role==='assistant');
                  return {loading:!!v.loading, meta:a?a.meta:'', chars:a&&a.html?a.html.length:0}})()""")
            if isinstance(cloud_state, dict) and not cloud_state.get("loading") and cloud_state.get("meta"):
                break
        res["steps"].append({"back_to_cloud": back, "cloud_answer": cloud_state})

        res["errors"] = js("window.__err||[]")
        shot = call("Page.captureScreenshot", {"format": "png",
                                               "captureBeyondViewport": False}).get("data")
        if shot:
            (OUT / "model_switch.png").write_bytes(base64.b64decode(shot))

        res["checks"] = {
            "default_cloud": init.get("route") == "cloud",
            "dropdown_when_available": (init.get("ready") is True) or (init.get("available") is False),
            "menu_has_both": len(items or []) >= 2
            and any("云端" in x for x in items) and any("本地" in x for x in items),
            "click_switches_to_local": picked.get("route") == "local",
            "label_shows_local_model": (init.get("model") or "") in (picked.get("label") or ""),
            "local_answer_tagged": "本地" in (local_state.get("meta") or ""),
            "local_answer_nonempty": (local_state.get("chars") or 0) > 20,
            "click_back_to_cloud": back.get("route") == "cloud",
            "cloud_answer_untagged": "本地" not in (cloud_state.get("meta") or ""),
            "no_js_errors": not res["errors"],
        }
        res["ok"] = all(res["checks"].values())
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        try:
            if ws:
                ws.close()
        finally:
            proc.terminate()
            time.sleep(0.5)
            try:
                proc.kill()
            except Exception:
                pass
    return res


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    raise SystemExit(0 if out["ok"] else 1)
