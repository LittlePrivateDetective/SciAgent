"""划词翻译浮框可拖动：几何取证。

用户反馈「译完的框正好压住正在读的那一行」。这个脚本不改需求描述，只量三个数：
1. 浮框默认落点与选区的重叠面积（证明问题真实存在，非主观感受）；
2. 按住标题栏拖动后，浮框实际平移量（CDP 真实鼠标事件，不是脚本改 state）；
3. 拖动后浮框与选区的重叠面积（必须为 0）且整框仍在视口内。

只断言"位置变了"是不够的：clamp 写错会把框钉在边上，拖动量对不上。
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

OVERLAP_JS = """
function __ov(a, b) {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return (w > 0 && h > 0) ? Math.round(w * h) : 0;
}
"""


def run(url: str = BASE + "/", shot_dir: Path = OUT) -> dict:
    port = 9511 + (int(time.time()) % 200)
    profile = shot_dir / f"_p_drag_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    result: dict = {"ok": False, "steps": [], "error": ""}
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

        def js(expr: str):
            r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                          "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return (r.get("result") or {}).get("value")

        def wait_for(expr: str, seconds: float = 30, interval: float = 0.4):
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

        def mouse(kind: str, x: float, y: float):
            call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y,
                "button": "left" if kind != "mouseMoved" else "none",
                "buttons": 1 if kind != "mouseReleased" else 0,
                "clickCount": 1,
            })

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})

        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        opened = js("""(async()=>{
          const vm = window.__sciagent;
          const d = vm.docs.find(x => x.filename === '1706.03762v1.pdf');
          if (!d) return 'NOT_FOUND';
          await vm.openDoc(d);
          vm.goPage(2);
          return 'OK';
        })()""")
        result["steps"].append({"open": opened})
        if opened != "OK":
            result["error"] = "打开目标文献失败"
            return result
        if not wait_for("document.querySelectorAll('.text-layer .tw').length > 50", 40):
            result["error"] = "文本层未渲染"
            return result

        # 1) 划词：取第 2 页正文里连续 8 个词
        picked = js("""(async()=>{
          const tws = [...document.querySelectorAll('.text-layer .tw')];
          let start = tws.findIndex(el => /^[A-Z][a-z]{3,}$/.test(el.textContent));
          if (start < 0) start = 20;
          const seg = tws.slice(start, start + 8);
          const range = document.createRange();
          range.setStartBefore(seg[0]);
          range.setEndAfter(seg[seg.length-1]);
          const s = window.getSelection();
          s.removeAllRanges(); s.addRange(range);
          const text = s.toString().trim();
          document.querySelector('.text-layer').dispatchEvent(
            new MouseEvent('mouseup', {bubbles:true, clientX:100, clientY:100}));
          await new Promise(r=>setTimeout(r,300));
          const pop = document.querySelector('.sel-pop');
          return {text, pop: !!pop};
        })()""")
        result["steps"].append({"select": picked})
        if not picked or not picked.get("pop"):
            result["error"] = "选中后未弹出浮框"
            return result

        # 2) 点「翻译这段」，等真实译文回来（浮框此时会变高，正是挡字的形态）
        js("(()=>{const b=document.querySelector('.sel-body button'); if(b) b.click();})()")
        got = wait_for("(()=>{const z=document.querySelector('.sel-zh');return z && z.textContent.trim().length>2;})()", 120)
        if not got:
            result["error"] = "译文未返回"
            return result
        time.sleep(0.5)

        before = js(OVERLAP_JS + """(()=>{
          const pop = document.querySelector('.sel-pop');
          const sel = window.getSelection().getRangeAt(0).getBoundingClientRect();
          const pr = pop.getBoundingClientRect();
          const head = document.querySelector('.sel-head').getBoundingClientRect();
          return {
            pop:{left:+pr.left.toFixed(1), top:+pr.top.toFixed(1),
                 w:+pr.width.toFixed(1), h:+pr.height.toFixed(1)},
            sel:{left:+sel.left.toFixed(1), top:+sel.top.toFixed(1),
                 right:+sel.right.toFixed(1), bottom:+sel.bottom.toFixed(1)},
            overlap: __ov(pr, sel),
            head:{x:+(head.left+head.width/2).toFixed(1), y:+(head.top+head.height/2).toFixed(1)},
            title: (document.querySelector('.sel-head-t')||{}).textContent||'',
            cursor: getComputedStyle(document.querySelector('.sel-head')).cursor,
            zh: (document.querySelector('.sel-zh')||{}).textContent||''
          };
        })()""")
        result["steps"].append({"before": {**before, "zh": (before.get("zh") or "")[:80]}})
        shot("sel_drag_before.png")

        # 3) 真实鼠标拖动标题栏 → 左下空白区
        # 目标点必须按真实视口算：--window-size 是窗口尺寸，innerHeight 只有 860 左右，
        # 传超出视口的坐标 CDP 会当作无效手势丢弃最后一帧，看起来就像"没跟手"（实测踩过）。
        vw, vh = js("[innerWidth, innerHeight]")
        result["steps"].append({"viewport": {"w": vw, "h": vh}})
        hx, hy = before["head"]["x"], before["head"]["y"]
        # 落点选在左侧空白：left = tx - 160 ≥ 4、top+高 ≤ vh-4，都不触发 clamp，
        # 这样位移差就是纯粹的手感误差，而不是被边界修正过的数字。
        tx, ty = 200, vh - 200
        mouse("mousePressed", hx, hy)
        for k in range(1, 7):
            mouse("mouseMoved", hx + (tx - hx) * k / 6, hy + (ty - hy) * k / 6)
            time.sleep(0.06)
        mouse("mouseReleased", tx, ty)
        time.sleep(0.4)

        after = js(OVERLAP_JS + """(()=>{
          const pop = document.querySelector('.sel-pop');
          const sel = window.getSelection().getRangeAt(0).getBoundingClientRect();
          const pr = pop.getBoundingClientRect();
          const z = document.querySelector('.sel-zh');
          return {
            pop:{left:+pr.left.toFixed(1), top:+pr.top.toFixed(1),
                 w:+pr.width.toFixed(1), h:+pr.height.toFixed(1)},
            overlap: __ov(pr, sel),
            inViewport: pr.left >= 0 && pr.top >= 0 &&
                        pr.right <= innerWidth + 0.5 && pr.bottom <= innerHeight + 0.5,
            dragging: window.__sciagent.sel.dragging,
            bodyLock: document.body.classList.contains('is-dragging'),
            zhStill: ((document.querySelector('.sel-zh')||{}).textContent||'').length > 2,
            zhText: z ? z.textContent : '',
            // 译文不能被 .sel-pop 的 overflow:hidden 裁掉，也不能超出半屏
            zhScrollH: z ? z.scrollHeight : 0,
            bodyScrollH: (()=>{const b=document.querySelector('.sel-body');return b?b.scrollHeight:0;})(),
            bodyClientH: (()=>{const b=document.querySelector('.sel-body');return b?b.clientHeight:0;})()
          };
        })()""")
        # 译文完整渲染：sel-zh 自身没有被压扁（clientHeight 与内容高度一致即未被裁剪）
        result["steps"].append({"zh_render": {
            "text": after["zhText"],
            "zh_h": after["zhScrollH"],
            "body_h": after["bodyClientH"],
            "body_scroll_h": after["bodyScrollH"],
            "scrollable": after["bodyScrollH"] > after["bodyClientH"] + 1,
        }})
        dx = round(after["pop"]["left"] - before["pop"]["left"], 1)
        dy = round(after["pop"]["top"] - before["pop"]["top"], 1)
        # 浮层左上角应严格等于「鼠标位移量」，偏差 > 2px 就是抓手锚点算错（会越拖越远）
        grip_err = round(max(abs(dx - (tx - hx)), abs(dy - (ty - hy))), 1)
        result["steps"].append({"after": after, "delta": {"dx": dx, "dy": dy},
                                "mouse_delta": {"dx": round(tx - hx, 1),
                                                "dy": round(ty - hy, 1)},
                                "grip_error_px": grip_err})
        shot("sel_drag_after.png")

        moved = abs(dx) + abs(dy) >= 100
        cleared = after["overlap"] == 0
        follows = grip_err <= 2
        result["steps"].append({
            "drag": "PASS" if moved and cleared and follows and after["inViewport"]
                    and not after["dragging"] else "FAIL",
            "blocked_before": before["overlap"] > 0,
            "overlap_cleared": cleared,
            "follows_cursor": follows,
            "in_viewport": after["inViewport"],
            "state_reset": not after["dragging"] and not after["bodyLock"],
        })

        # 4) 关闭按钮仍可用（头部重排后不能被拖动手柄吞掉点击）
        closed = js("""(async()=>{
          const x = document.querySelector('.sel-x');
          if (!x) return 'NO_BTN';
          x.click();
          await new Promise(r=>setTimeout(r,250));
          return document.querySelector('.sel-pop') ? 'STILL_OPEN' : 'OK';
        })()""")
        result["steps"].append({"close": closed})

        # 5) 长选区：整段译完后浮框不能变成一屏白块
        js("""(()=>{
          window.__sciagent.sel.visible = false;
          window.getSelection().removeAllRanges();
        })()""")
        time.sleep(0.3)
        long_sel = js("""(async()=>{
          const tws = [...document.querySelectorAll('.text-layer .tw')];
          let start = tws.findIndex(el => /^[A-Z][a-z]{3,}$/.test(el.textContent));
          if (start < 0) start = 20;
          // 180 个词约 1200 字符，译出来通常超过半屏，才能压到高度上限
          const seg = tws.slice(start, start + 180);
          const range = document.createRange();
          range.setStartBefore(seg[0]);
          range.setEndAfter(seg[seg.length-1]);
          const s = window.getSelection();
          s.removeAllRanges(); s.addRange(range);
          const text = s.toString().trim();
          document.querySelector('.text-layer').dispatchEvent(
            new MouseEvent('mouseup', {bubbles:true, clientX:100, clientY:100}));
          await new Promise(r=>setTimeout(r,300));
          const b = document.querySelector('.sel-body button');
          if (b) b.click();
          return {chars: text.length};
        })()""")
        long_ok = wait_for(
            "(()=>{const z=document.querySelector('.sel-zh');return z && z.textContent.trim().length>60;})()",
            150)
        time.sleep(0.4)
        lng = js(OVERLAP_JS + """(()=>{
          const pop = document.querySelector('.sel-pop');
          const body = document.querySelector('.sel-body');
          const z = document.querySelector('.sel-zh');
          if (!pop || !body) return null;
          const pr = pop.getBoundingClientRect();
          return {
            popH: +pr.height.toFixed(1),
            vh: innerHeight,
            bodyClientH: body.clientHeight,
            bodyScrollH: body.scrollHeight,
            scrollable: body.scrollHeight > body.clientHeight + 1,
            capped: body.clientHeight <= Math.round(innerHeight * 0.46) + 2,
            inViewport: pr.top >= -0.5 && pr.bottom <= innerHeight + 0.5,
            zhChars: z ? z.textContent.length : 0
          };
        })()""")
        result["steps"].append({"long_select": long_sel, "long": lng})
        shot("sel_drag_long.png")
        # 判定"没被裁"：要么内容本来就装得下，要么装不下但能在框内滚动。
        # 单看 scrollable 会把「译文很短、根本不用滚」误判成失败（上一轮就这么踩的）。
        not_clipped = bool(lng) and (lng["bodyScrollH"] <= lng["bodyClientH"] + 1
                                     or lng["scrollable"])
        long_pass = bool(long_ok and lng and lng["inViewport"] and lng["capped"] and not_clipped)
        result["steps"].append({"long_ok": "PASS" if long_pass else "FAIL"})

        result["ok"] = bool(moved and cleared and follows and after["inViewport"]
                            and not after["dragging"] and closed == "OK" and long_pass)
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
