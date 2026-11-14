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

        # 1) 初始状态：默认云端，是否有切换按钮取决于本机有没有本地模型
        init = js("""(()=>{const v=window.__sciagent;
              const sw=document.querySelector('.badges .model-switch');
              const btns=sw?[...sw.querySelectorAll('.ms-btn')].map(b=>b.textContent.trim()):[];
              const name=document.querySelector('.badges .ms-name');
              return {route:v.askRoute, ready:!!sw, btns:btns,
                      name:name?name.textContent.trim():'',
                      label:(document.querySelector('.badges .el-tag')||{textContent:''}).textContent.trim(),
                      available:v.info && v.info.local ? !!v.info.local.available : false,
                      model:v.info && v.info.local ? v.info.local.model : '',
                      reason:v.info && v.info.local ? v.info.local.reason : ''}})()""")
        res["steps"].append({"initial": init})

        # 公网那种部署（没有 Ollama）：只验"不给按钮"，后面的切换一律跳过。
        #   EXPECT_LOCAL=0 BASE=... 跑这一支
        if os.environ.get("EXPECT_LOCAL", "1") != "1":
            res["errors"] = js("window.__err||[]")
            res["checks"] = {
                "no_button_without_local": not bool(js("!!document.querySelector('.badges .model-switch')")),
                "badge_still_shows_model": bool((init.get("label") or "").strip()),
                "no_js_errors": not res["errors"],
            }
            res["ok"] = all(res["checks"].values())
            return res

        # 2) 点「本地 3B」按钮（原来这里是展开下拉再选项；改成按钮后一次点击到位，
        #    也顺手验证了"按钮真的在、真的可点"）
        js("""(()=>{const b=[...document.querySelectorAll('.badges .ms-btn')]
              .find(e=>e.textContent.includes('本地')); if(b) b.click(); return 1})()""")
        time.sleep(0.8)
        picked = js("""(()=>{const v=window.__sciagent;
              const on=document.querySelector('.badges .ms-btn.on');
              const name=document.querySelector('.badges .ms-name');
              const styles=[...document.querySelectorAll('.badges .ms-btn')].map(b=>{
                const s=getComputedStyle(b);
                return {t:b.textContent.trim(), on:b.classList.contains('on'),
                        bg:s.backgroundColor, fg:s.color, bd:s.borderColor};});
              return {route:v.askRoute, active:on?on.textContent.trim():'',
                      hasLocalClass:on?on.classList.contains('local'):false,
                      styles:styles,
                      name:name?name.textContent.trim():''}})()""")
        res["steps"].append({"picked_local": picked})

        # 只查渲染、不真调模型（排查"界面上看不到按钮"用）：
        #   CHECK_ONLY=1 BASE=... 跑这一支，2 秒出结果
        if os.environ.get("CHECK_ONLY") == "1":
            res["errors"] = js("window.__err||[]")
            res["checks"] = {
                "switch_rendered": bool(init.get("ready")),
                "two_buttons": len(init.get("btns") or []) == 2,
                "click_switches_to_local": picked.get("route") == "local",
                "name_follows_route": (init.get("model") or "") in (picked.get("name") or ""),
                "no_js_errors": not res["errors"],
            }
            res["ok"] = all(res["checks"].values())
            shot = call("Page.captureScreenshot", {"format": "png"}).get("data")
            if shot:
                (OUT / "model_switch_check.png").write_bytes(base64.b64decode(shot))
            return res

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
        js("""(()=>{const b=[...document.querySelectorAll('.badges .ms-btn')]
              .find(e=>e.textContent.includes('云端')); if(b) b.click(); return 1})()""")
        time.sleep(0.6)
        back = js("""(()=>{const v=window.__sciagent;const on=document.querySelector('.badges .ms-btn.on');
              return {route:v.askRoute, active:on?on.textContent.trim():''}})()""")
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
            "switch_when_available": (init.get("ready") is True) or (init.get("available") is False),
            "two_buttons": len(init.get("btns") or []) == 2,
            "click_switches_to_local": picked.get("route") == "local",
            "active_btn_is_local": picked.get("hasLocalClass") is True,
            "name_follows_route": (init.get("model") or "") in (picked.get("name") or ""),
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
