"""句级高亮回归：点证据后必须高亮「那几句」，而不是「整段一个大矩形」。

用户截图反馈的问题：点证据后正文被涂成一整块黄方块，看不出哪句支撑结论。
根因是直接拿 chunk 的 bbox 画框（一个 chunk 通常就是整段，实测 bbox 高 94pt ≈ 7 行）。

本脚本走 CDP 真实浏览器，全部看几何数字，不看"看起来对不对"：
1. 多行：高亮框数 ≥ 2，单个框高 ≤ 1.8 倍行高（说明是逐行，不是整块）；
2. 不越界：落在框内的每个词都必须属于关键句（框里不能顺带涂到无关文字）；
3. 覆盖全：关键句首词、末词都被框覆盖（不能只亮半句）；
4. 面积小：框面积之和 < chunk bbox 的 60%（旧实现恒为 100%）。
另外记录「句首从行中间开始」的框数量，这正是用户要求的"句子开头在文章中间"。
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
QUESTION = "Transformer (big) 模型在 English-to-German 任务上的 BLEU 分数是多少？当时的 State-of-the-art 模型是多少？提升了多少？"


def _launch(port: int, profile: Path):
    return subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def run() -> dict:
    port = 9611 + (int(time.time()) % 60)
    profile = OUT / f"_p_hl_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = _launch(port, profile)
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

        def wait_for(expr: str, seconds: float = 40, interval: float = 0.4):
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    if js(expr):
                        return True
                except Exception:
                    pass
                time.sleep(interval)
            return False

        def shot(name: str, clip: dict | None = None):
            params: dict = {"format": "png"}
            if clip:
                params["clip"] = clip
            data = call("Page.captureScreenshot", params).get("data")
            if data:
                (OUT / name).write_bytes(base64.b64decode(data))

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE + "/"})

        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        # 1) 走真实问答：不预设证据，让检索自己跑（复现用户的操作路径）
        asked = js("""(async()=>{
          const vm = window.__sciagent;
          vm.streamMode = false;
          vm.scope = 'all';
          vm.messages = [];
          await vm.ask(%s);
          const m = vm.messages.filter(x => x.role === 'assistant' && x.evidence);
          if (!m.length) return 'NO_EVIDENCE';
          const ev = m[m.length-1].evidence;
          return JSON.stringify({n: ev.length, focus: ev.map(e => (e.focus||[]).length),
                                 modes: ev.map(e => e.focus_mode || '')});
        })()""" % json.dumps(QUESTION, ensure_ascii=False))
        result["steps"].append({"ask": asked})
        meta = json.loads(asked) if asked and asked.startswith("{") else {}
        if not meta:
            result["error"] = "问答未返回证据"
            return result

        # 2) 点证据：优先点「文本版 PDF」的关键句（扫描版留到第二阶段验证降级）
        def pick_and_measure(mode: str, shot_name: str) -> dict:
            clicked = js("""(async()=>{
              const vm = window.__sciagent;
              const items = [...document.querySelectorAll('.evid-item')];
              const msg = vm.messages.filter(m => m.role === 'assistant' && m.evidence).pop();
              const evs = msg ? msg.evidence : [];
              let idx = -1;
              for (let i = 0; i < items.length; i++) {
                const e = evs[i] || {};
                const scan = /_img\\.pdf$/i.test(e.doc_name || '');
                const hasFocus = (e.focus || []).length > 0;
                if ('__MODE__' === 'text' && hasFocus && !scan) { idx = i; break; }
                if ('__MODE__' === 'scan' && scan) { idx = i; break; }
                // 表格段：没有可挑的句子（整块是一张表），验证它同样是逐行画
                if ('__MODE__' === 'table' && !scan && (e.focus_mode === 'whole')
                    && (e.text || '').length > 200) { idx = i; break; }
              }
              if (idx < 0) return 'NO_MATCH';
              window.__hl_ev = evs[idx];
              window.__focus_sentence = (evs[idx].focus && evs[idx].focus[0]) || evs[idx].text || '';
              items[idx].click();
              return 'OK';
            })()""".replace("__MODE__", mode))
            result["steps"].append({f"click_{mode}": clicked})
            if clicked != "OK":
                return {"error": "没有可点的目标证据"}

            if not wait_for("document.querySelectorAll('.hl').length > 0", 30):
                return {"error": "高亮框未出现"}
            time.sleep(1.2)   # 等滚动与过渡结束再量

            geo = js("""(()=>{
              const vm = window.__sciagent;
              const norm = s => String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'');
              const hl = [...document.querySelectorAll('.hl')];
              const tws = [...document.querySelectorAll('.text-layer .tw')];
              const boxes = hl.map(el => { const r = el.getBoundingClientRect();
                return {x:r.left, y:r.top, w:r.width, h:r.height, approx: el.classList.contains('approx')}; });
              const words = tws.map(el => { const r = el.getBoundingClientRect();
                return {t: norm(el.textContent), raw: el.textContent, x:r.left, y:r.top, w:r.width, h:r.height}; })
                .filter(w => w.t);
              const hs = words.map(w => w.h).sort((a,b)=>a-b);
              const lineH = hs.length ? hs[Math.floor(hs.length/2)] : 0;

              const sent = (window.__focus_sentence || '');
              const toks = String(sent).toLowerCase().split(/\\s+/).map(norm).filter(Boolean);

              const inside = (b, w, pad=1) => {
                const cx = w.x + w.w/2, cy = w.y + w.h/2;
                return cx >= b.x-pad && cx <= b.x+b.w+pad && cy >= b.y-pad && cy <= b.y+b.h+pad;
              };
              // 框内所有词：必须都属于关键句（含连字符断词的片段）
              let covered = [], stray = [];
              for (const b of boxes) {
                for (const w of words) if (inside(b, w)) {
                  covered.push(w.t);
                  const ok = toks.includes(w.t) || toks.some(t => t.includes(w.t) || w.t.includes(t));
                  if (!ok) stray.push(w.raw.trim());
                }
              }
              const covSet = new Set(covered);
              const caught = toks.filter(t => covSet.has(t) || [...covSet].some(c => c.includes(t)||t.includes(c))).length;

              // 面积比：所有框 vs chunk bbox（px）
              const meta = vm.pageSizes[vm.page] || {width:1,height:1};
              const img = document.querySelector('.pdf-page');
              const scale = img ? img.clientWidth / meta.width : 1;
              const evBox = (window.__hl_ev && window.__hl_ev.bbox) || [0,0,0,0];
              const bboxArea = Math.max(1,(evBox[2]-evBox[0])*scale) * Math.max(1,(evBox[3]-evBox[1])*scale);
              const rectArea = boxes.reduce((s,b)=>s+b.w*b.h, 0);
              const leftPx = (evBox[0]||0) * scale;
              const indented = boxes.filter(b => b.x > leftPx + 12).length;

              return {
                rects: boxes.length,
                approx: boxes.filter(b=>b.approx).length,
                lineH: +lineH.toFixed(2),
                maxRectH: +Math.max(...boxes.map(b=>b.h), 0).toFixed(2),
                ratio: +(rectArea/bboxArea).toFixed(3),
                stray: stray.length, strayWords: stray.slice(0,8),
                sentTokens: toks.length, covered: covered.length,
                coverage: toks.length ? +(caught/toks.length).toFixed(3) : 0,
                indented,
                firstWord: toks.slice(0,2).join(' '), lastWord: toks.slice(-2).join(' '),
                anyHlOverlap: boxes.some((a,i)=>boxes.some((b,j)=>j>i &&
                   Math.min(a.y+a.h,b.y+b.h) - Math.max(a.y,b.y) > 2 &&
                   Math.min(a.x+a.w,b.x+b.w) - Math.max(a.x,b.x) > 2)),
              };
            })()""")
            shot(shot_name)
            # 再抓一张 2 倍放大的局部图：答辩/截图复核时能看清"逐行贴字"而不是整块
            zoom = js("""(()=>{
              const bs = [...document.querySelectorAll('.hl')].map(e=>e.getBoundingClientRect());
              if (!bs.length) return null;
              const x = Math.min(...bs.map(b=>b.left)) - 30, y = Math.min(...bs.map(b=>b.top)) - 26;
              const r = Math.max(...bs.map(b=>b.right)), b2 = Math.max(...bs.map(b=>b.bottom));
              return {x, y, width: Math.min(r - x + 30, innerWidth - x), height: b2 - y + 26, scale: 2};
            })()""")
            if zoom:
                shot(shot_name.replace(".png", "_zoom.png"), zoom)
            return geo

        geo = pick_and_measure("text", "hl_sentence.png")
        result["steps"].append({"geo": geo})
        if geo.get("error"):
            result["error"] = geo["error"]
            return result

        multi = geo["rects"] >= 2
        per_line = geo["lineH"] > 0 and geo["maxRectH"] <= geo["lineH"] * 1.8
        no_stray = geo["stray"] == 0
        covered = geo["coverage"] >= 0.85
        small = geo["ratio"] <= 0.6
        result["checks"] = {
            "multi_rect": multi, "per_line": per_line, "no_stray": no_stray,
            "coverage": covered, "area_ratio": small,
            "rects": geo["rects"], "lineH": geo["lineH"], "maxRectH": geo["maxRectH"],
            "ratio": geo["ratio"], "coverage_value": geo["coverage"],
            "stray": geo["stray"], "indented": geo["indented"],
        }

        # 3) 扫描版（无词级文本层）：必须如实降级为虚线近似框，而不是假装定位到句子
        scan = pick_and_measure("scan", "hl_sentence_scan.png")
        result["steps"].append({"scan": scan})
        scan_ok = bool(scan.get("rects") == 1 and scan.get("approx") == 1)
        result["checks"]["scan_fallback"] = scan_ok

        # 4) 表格段（整块没有单句可挑）：仍要逐行，不能退回一个大矩形
        table = pick_and_measure("table", "hl_table.png")
        result["steps"].append({"table": table})
        table_ok = bool(table.get("rects", 0) >= 3 and table.get("maxRectH", 999) <= table.get("lineH", 1) * 1.8)
        result["checks"]["table_per_line"] = table_ok

        # 5) 正文锚点（答案里的 p7 ▸）：必须先切到证据所属文献，再按句高亮。
        #    旧实现直接用"当前打开的文献"，点 Transformer 的 p7 会跳到正在读的
        #    ViT 第 7 页同一块坐标上——高亮跑到无关段落，正是用户截图里的现象。
        anchor = js("""(async()=>{
          const vm = window.__sciagent;
          const other = vm.docs.find(d => /2010.11929/.test(d.filename));
          if (other) await vm.openDoc(other);
          const before = vm.activeDoc ? vm.activeDoc.filename : '';
          // 挑一个指向"文本版 PDF"的锚点：扫描版没有词级坐标，只能近似框，
          // 那条路径已由 scan_fallback 单独覆盖
          let a = null, ev = null;
          for (const el of document.querySelectorAll('.anchor')) {
            const e = vm.evidenceAt(Number(el.dataset.page), el.dataset.bbox.split(',').map(Number));
            if (e && !/_img\.pdf$/i.test(e.doc_name || '')) { a = el; ev = e; break; }
          }
          if (!a) return 'NO_ANCHOR';
          const page = Number(a.dataset.page);
          window.__focus_sentence = ev ? ((ev.focus && ev.focus[0]) || ev.text || '') : '';
          a.click();
          await new Promise(r => setTimeout(r, 1500));
          const hl = [...document.querySelectorAll('.hl')];
          const lineH = (() => {
            const hs = [...document.querySelectorAll('.tw')].map(e=>e.getBoundingClientRect().height).sort((x,y)=>x-y);
            return hs.length ? hs[Math.floor(hs.length/2)] : 0;
          })();
          return JSON.stringify({
            before, after: vm.activeDoc ? vm.activeDoc.filename : '',
            evDoc: ev ? ev.doc_name : '', page: vm.page,
            rects: hl.length, approx: hl.filter(e=>e.classList.contains('approx')).length,
            maxH: +Math.max(...hl.map(e=>e.getBoundingClientRect().height), 0).toFixed(2),
            lineH: +lineH.toFixed(2), switched: !!(other && vm.activeDoc && vm.activeDoc.id !== other.id),
          });
        })()""")
        result["steps"].append({"anchor": anchor})
        a = json.loads(anchor) if anchor and anchor.startswith("{") else {}
        anchor_ok = bool(a.get("switched") and a.get("rects", 0) >= 2
                         and a.get("approx", 1) == 0
                         and a.get("maxH", 99) <= a.get("lineH", 1) * 1.8)
        result["checks"]["anchor_switch_doc"] = anchor_ok

        result["ok"] = bool(multi and per_line and no_stray and covered and small and scan_ok and table_ok and anchor_ok)
        return result
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:4000])
