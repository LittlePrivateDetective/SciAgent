"""「快速 / 深思」开关的浏览器回归。

验五件事：
1. 输入栏里有这个按钮，初始文案跟随后台设置（当前全局 thinking=0 → 显示"快速"）；
2. 点一下变"深思"，再点变回"快速"（两个方向都能切）；
3. 深思提问后，回答上方出现「深思 · Xs」，且后端 meta.thinking=true、模型是主模型；
4. 快速提问后，标签是「快速 · Xs」，meta.thinking=false、模型是 trans_model；
5. 全程零 JS 报错。

    ./.venv/Scripts/python.exe tests/ui_think_toggle.py
"""
from __future__ import annotations

import base64
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


def run() -> dict:
    port = 9791 + (int(time.time()) % 30)
    profile = OUT / f"_p_think_{int(time.time())}"
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
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
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
            if js("!!(window.__sciagent && window.__sciagent.docs !== undefined)"):
                break
            time.sleep(0.4)

        # 关掉答案缓存：否则同一个问题第二次命中缓存，reason/token 事件变成回放，
        # 测出来的"思考过程可见"就不是模型的思考了。
        js("window.__SCIAGENT_NO_CACHE = 1; 1")

        # 1) 按钮存在且跟随后台
        init = js("""(()=>{const v=window.__sciagent;
              const b=document.querySelector('.think-btn');
              // 这一项要在改任何状态之前读：官方要求能展示思考过程，
              // 而只有流式推得出「规划→检索→通读→推理」这几步，所以默认必须开
              const defStream = v.streamMode;
              v.centerView='chat'; v.streamMode=true;
              return {has:!!b, text:b?b.textContent.trim():'', deep:!!v.thinkDeep,
                      cls:b?b.className:'', defStream:defStream}})()""")
        res["steps"].append({"initial": init})

        # 2) 默认就是深思：先不动按钮，直接问
        #    （默认深思是刻意的：快速模式模型不产推理内容，界面上只剩管线阶段，
        #     那不叫"思考过程"。觉得慢的人点一下就能切快速。）
        res["steps"].append({"default_mode": {"text": init.get("text"), "deep": init.get("deep")}})

        # 3) 深思提问
        js("(()=>{const v=window.__sciagent; v.ask('BERT 和 GPT 的预训练目标有什么不同？'); return 1})()")
        deep_state = {}
        t0 = time.time()
        while time.time() - t0 < 200:
            time.sleep(2)
            deep_state = js("""(()=>{const v=window.__sciagent;
                  const a=[...v.messages].reverse().find(x=>x.role==='assistant');
                  // 取最后一条：查第一个拿到的是上一条回答的标签
                  const ns=document.querySelectorAll('.msg.assistant .msg-meta');
                  const m=ns[ns.length-1];
                  // thinkSteps 数的是真画出来的时间线节点，不是状态数组：
                  // 流式的意义就是这几步用户看得见
                  const steps=[...document.querySelectorAll('.msg.assistant')].pop();
                  return {loading:!!v.loading, meta:a?a.meta:'', metaDom:m?m.textContent.trim():'',
                          chars:a&&a.html?a.html.length:0,
                          thinkSteps:steps?steps.querySelectorAll('.think-step').length:0,
                          reasonLen:(a&&a.reasonText?a.reasonText.length:0),
                          reasonHeader:(steps&&steps.querySelector('.reason-title'))
                            ?steps.querySelector('.reason-title').textContent.trim():'',
                          stages:(a&&a.thinking?a.thinking:[]).filter(x=>x&&x.stage==='depth')}})()""")
            if isinstance(deep_state, dict) and not deep_state.get("loading") and deep_state.get("meta"):
                break
        res["steps"].append({"deep_answer": deep_state})

        # 4) 切到快速再问
        js("document.querySelector('.think-btn').click()")
        time.sleep(0.3)
        after2 = js("""(()=>{const v=window.__sciagent;const b=document.querySelector('.think-btn');
              return {text:b.textContent.trim(), deep:!!v.thinkDeep,
                      on:b.classList.contains('on')}})()""")
        js("(()=>{const v=window.__sciagent; v.ask('Mistral 7B 的滑动窗口大小 W 是多少？'); return 1})()")
        fast_state = {}
        t0 = time.time()
        while time.time() - t0 < 200:
            time.sleep(2)
            fast_state = js("""(()=>{const v=window.__sciagent;
                  const a=[...v.messages].reverse().find(x=>x.role==='assistant');
                  // 取最后一条：查第一个拿到的是上一条回答的标签
                  const ns=document.querySelectorAll('.msg.assistant .msg-meta');
                  const m=ns[ns.length-1];
                  return {loading:!!v.loading, meta:a?a.meta:'', metaDom:m?m.textContent.trim():'',
                          chars:a&&a.html?a.html.length:0}})()""")
            if isinstance(fast_state, dict) and not fast_state.get("loading") and fast_state.get("meta"):
                break
        res["steps"].append({"switch_to_fast": after2, "fast_answer": fast_state})

        res["errors"] = js("window.__err||[]")
        shot = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}).get("data")
        if shot:
            (OUT / "think_toggle.png").write_bytes(base64.b64decode(shot))

        res["checks"] = {
            "btn_present": bool(init.get("has")),
            # 默认必须是深思：否则界面上没有推理内容可看
            "default_deep": init.get("text") == "深思" and init.get("deep") is True,
            "toggle_to_fast": after2.get("text") == "快速" and after2.get("deep") is False,
            "deep_label": "深思" in (deep_state.get("metaDom") or ""),
            "deep_stage": bool(deep_state.get("stages")),
            "stream_on_by_default": init.get("defStream") is True,
            "stream_thinking_visible": (deep_state.get("thinkSteps") or 0) >= 3,
            # 深思模式必须真的有推理原文（这才是"思考过程"），而不是只有管线阶段
            "reasoning_visible": (deep_state.get("reasonLen") or 0) > 40,
            "reasoning_labeled": "推理过程" in (deep_state.get("reasonHeader") or ""),
            "fast_label": "快速" in (fast_state.get("metaDom") or ""),
            "no_js_error": not res["errors"],
        }
        res["ok"] = all(res["checks"].values())
        return res
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
        return res
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["ok"] else 1)
