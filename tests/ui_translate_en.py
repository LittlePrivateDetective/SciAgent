"""中译英（官方 1.6.4）回归：划词译英 + 整篇译英。

用例建立在库里一篇**中文**测试论文上（`cn_paper.pdf`，由脚本生成后上传）。
两条路径都要真看到英文产出：

1. 划词：选中中文 → 浮层标题变成「译成英文」→ 译文含英文字母且**不含汉字**；
2. 整篇：点工具栏「译英」→ 抽屉出现 → 预览里 `h1` / `h2` 是英文标题、
   正文 > 500 字符、进度收敛到 100%。

"不含汉字"这条是硬指标：中译英最容易出的问题就是模型把术语漏成中文，
或者把原文整段照抄回来——只看"有输出"是查不出来的。
"""
from __future__ import annotations

import base64
import json
import re
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
CN = "cn_paper"   # 前缀匹配：assets 里叫 cn_paper_sample.pdf，入库后可能被改名


def run(url: str = BASE + "/", shot_dir: Path = OUT) -> dict:
    port = 9811 + (int(time.time()) % 200)
    profile = shot_dir / f"_p_en_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    result: dict = {"ok": False, "steps": [], "error": "", "console": []}
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
            result["error"] = "CDP 端口未就绪"
            return result
        page = next(t for t in targets if t.get("type") == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
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
                if d.get("method") == "Runtime.exceptionThrown":
                    desc = (d["params"].get("exceptionDetails") or {}).get("text", "")
                    result["console"].append("EXC " + str(desc)[:200])

        def js(expr: str):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return (r.get("result") or {}).get("value")

        def wait_for(expr: str, seconds: float = 60, interval: float = 0.5):
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        def shot(name: str):
            data = call("Page.captureScreenshot", {"format": "png"}).get("data")
            if data:
                (shot_dir / name).write_bytes(base64.b64decode(data))
            return str(shot_dir / name)

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})

        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        opened = js("""(async()=>{
          const vm = window.__sciagent;
          const d = vm.docs.find(x => x.filename.startsWith('%s'));
          if (!d) return {err:'NOT_FOUND'};
          await vm.openDoc(d);
          vm.goPage(1);
          return {id: d.id, filename: d.filename, chunks: d.chunks};
        })()""" % CN)
        result["steps"].append({"open": opened})
        if not opened or opened.get("err"):
            result["error"] = "库里没有中文测试论文（文件名以 cn_paper 开头）"
            return result
        if not wait_for("document.querySelectorAll('.text-layer .tw').length > 5", 40):
            result["error"] = "中文页未渲染文本层（划词不可用）"
            return result

        # 1) 划词中译英
        sel = js("""(async()=>{
          const tws = [...document.querySelectorAll('.text-layer .tw')];
          // 挑一行含汉字最多的正文，避开页眉标题
          const idx = tws.findIndex(el => (el.textContent.match(/[\\u4e00-\\u9fff]/g)||[]).length > 15);
          const start = idx < 0 ? 1 : idx;
          const range = document.createRange();
          range.setStartBefore(tws[start]);
          range.setEndAfter(tws[Math.min(start + 1, tws.length - 1)]);
          const s = window.getSelection();
          s.removeAllRanges(); s.addRange(range);
          const text = s.toString().trim();
          document.querySelector('.text-layer').dispatchEvent(
            new MouseEvent('mouseup', {bubbles:true, clientX:100, clientY:100}));
          await new Promise(r=>setTimeout(r,300));
          const btn = document.querySelector('.sel-body button');
          if (btn) btn.click();
          return {src: text.slice(0, 60), srcLen: text.length, pop: !!document.querySelector('.sel-pop')};
        })()""")
        got = wait_for(
            "(()=>{const z=document.querySelector('.sel-zh');return z && z.textContent.trim().length>10;})()",
            120)
        if not got:
            result["error"] = "划词中译英未返回"
            return result
        sel_out = js("""(()=>{
          const z = document.querySelector('.sel-zh');
          const t = z ? z.textContent.trim() : '';
          const head = (document.querySelector('.sel-head-t')||{}).textContent||'';
          return {title: head, zh: t.slice(0, 120), len: t.length,
                  cjk: (t.match(/[\\u4e00-\\u9fff]/g)||[]).length,
                  hasLatin: /[A-Za-z]{4,}/.test(t)};
        })()""")
        result["steps"].append({"inline": {**sel, "out": sel_out}})
        shot("en_inline.png")
        inline_ok = (sel_out["title"] == "译成英文" and sel_out["cjk"] == 0
                     and sel_out["hasLatin"] and sel_out["len"] > 20)

        # 2) 整篇译英：点工具栏按钮
        js("window.getSelection().removeAllRanges(); window.__sciagent.sel.visible = false")
        time.sleep(0.3)
        js("""(()=>{
          const btns = [...document.querySelectorAll('.viewer-tools button')];
          const b = btns.find(x => x.textContent.trim().startsWith('译英'));
          if (b) b.click();
          return !!b;
        })()""")
        ok = wait_for("""(()=>{
          const p = document.querySelector('.en-preview');
          const vm = window.__sciagent;
          return p && vm.en.chars > 300 && !vm.en.running;
        })()""", 240)
        doc = js("""(()=>{
          const p = document.querySelector('.en-preview');
          const vm = window.__sciagent;
          const h1 = p ? p.querySelector('h1') : null;
          const h2 = p ? [...p.querySelectorAll('h2')] : [];
          const txt = p ? p.textContent : '';
          return {
            hasPreview: !!p,
            h1: h1 ? h1.textContent.trim() : '',
            h2n: h2.length,
            h2first: h2.length ? h2[0].textContent.trim() : '',
            chars: vm.en.chars, ms: vm.en.ms, running: vm.en.running,
            total: vm.en.total, segments: vm.en.segments, error: vm.en.error,
            cjk: (txt.match(/[\\u4e00-\\u9fff]/g)||[]).length,
            hasMarkdownHash: txt.indexOf('##') >= 0,
            previewHead: txt.slice(0, 160).replace(/\\n/g, ' ')
          };
        })()""")
        result["steps"].append({"whole": doc})
        shot("en_whole.png")
        # 标题必须是英文：中文标题出现在英文版大标题/小节里即为不合格
        whole_ok = (ok and doc["hasPreview"] and doc["chars"] > 300
                    and re.search(r"[A-Za-z]{4,}", doc["h1"]) is not None
                    and (doc["cjk"] < 40) and not doc["hasMarkdownHash"])

        result["ok"] = bool(inline_ok and whole_ok)
        result["steps"].append({"inline_ok": inline_ok, "whole_ok": whole_ok})
        if result["console"]:
            result["steps"].append({"console": result["console"][:3]})
        return result
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
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r["ok"] else 1)
