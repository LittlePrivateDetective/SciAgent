"""流式与思考过程：截屏之外，用采样证明"字是一个个出来的、推理是实时长出来的"。

用户的原话是「流式输出没有体现」「没有思考过程」。这两件事光看最终截图都看不出来，
只能盯住中间态，所以这条用例在提问后每 80ms 采样一次：

1. 正文容器里的字符数要出现多个递增的中间值（一次性赋值只会有 0 和一个终值）；
2. 流式期间要有 .streaming 光标（说明还在写，不是卡住）；
3. 推理区字数要逐步增长（说明推理原文是流式推来的，不是生成完再贴上去）；
4. 收尾后光标消失、答案定型、标签是「深思」。

用法：./.venv/Scripts/python.exe tests/ui_stream_visible.py
证据：docs/qa/evidence/stream_visible_{mid,end}.png
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


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9869 + (int(time.time()) % 20)
    profile = Path(tempfile.gettempdir()) / f"_p_stream_{int(time.time())}"
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
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180)
        seq = [0]

        def call(m, p=None):
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": m, "params": p or {}}))
            while True:
                d = json.loads(ws.recv())
                if d.get("id") == seq[0]:
                    return d.get("result", {})

        def js(e):
            r = call("Runtime.evaluate", {"expression": e, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(str(r["exceptionDetails"])[:200])
            return (r.get("result") or {}).get("value")

        def shot(name):
            d = call("Page.captureScreenshot", {"format": "png"}).get("data")
            if d:
                (OUT / name).write_bytes(base64.b64decode(d))

        call("Page.enable")
        call("Runtime.enable")
        call("Emulation.setDeviceMetricsOverride",
             {"width": 1280, "height": 860, "deviceScaleFactor": 1, "mobile": False})
        call("Page.navigate", {"url": BASE})
        for _ in range(120):
            if js("!!(window.__sciagent && window.__sciagent.docs.length)"):
                break
            time.sleep(0.4)
        time.sleep(1.0)

        conf = js("""(()=>{const v=window.__sciagent;
              v.centerView='chat';
              return {deep: !!v.thinkDeep, stream: !!v.streamMode}})()""")
        res["steps"].append({"config": conf})

        js("(()=>{const v=window.__sciagent; v.ask('BERT 和 GPT 的预训练目标有什么不同？'); return 1})()")

        samples = []
        mid_shot = False
        t0 = time.time()
        while time.time() - t0 < 90:
            s = js("""(()=>{const v=window.__sciagent;
                  const a=[...v.messages].reverse().find(x=>x.role==='assistant');
                  const md=document.querySelectorAll('.msg.assistant .md');
                  const el=md[md.length-1];
                  const last=[...document.querySelectorAll('.msg.assistant')].pop();
                  return {text: el?el.innerText.length:0,
                          htmlLen: a&&a.html?a.html.length:0,
                          timer: a?a._timer:null,
                          streaming: !!(el&&el.querySelector('p.streaming')),
                          reason: a&&a.reasonText?a.reasonText.length:0,
                          steps: last?last.querySelectorAll('.think-step').length:0,
                          loading: !!v.loading,
                          meta: a?a.meta:''};})()""")
            samples.append({"t": round(time.time() - t0, 2), **s})
            if s.get("streaming") and not mid_shot and (s.get("text") or 0) > 0:
                shot("stream_visible_mid.png")
                mid_shot = True
            if not s.get("loading") and s.get("meta"):
                break
            time.sleep(0.08)

        shot("stream_visible_end.png")
        texts = [x["text"] for x in samples if x["text"]]
        uniq = sorted(set(texts))
        reasons = [x["reason"] for x in samples]
        res["steps"].append({
            "samples": len(samples),
            "text_points": len(uniq),
            "min_max": [min(texts) if texts else 0, max(texts) if texts else 0],
            "reason_points": len(set(reasons)),
            "streaming_seen": sum(1 for x in samples if x["streaming"]),
            "final": samples[-1] if samples else {},
        })

        # 正文长度必须出现多个中间值：一次性赋值只会有 0 和终值两个点
        res["checks"] = {
            "default_deep": conf.get("deep") is True,
            "stream_on": conf.get("stream") is True,
            "text_grows_gradually": len(uniq) >= 3,
            "caret_seen": any(x["streaming"] for x in samples),
            "reasoning_grows": len(set(reasons)) >= 3,
            "timeline_steps": (samples[-1].get("steps") or 0) >= 4 if samples else False,
            "finished": bool(samples and not samples[-1]["loading"] and samples[-1]["meta"]),
            "caret_gone_at_end": bool(samples and not samples[-1]["streaming"]),
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
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["ok"] else 1)
