"""阅读器三项增强的回归：反向引用跳转 / 悬停翻译 / 中文译本导出。

对应的官方条目：
- 1.3.2 第 2 点后半：从 References 快速跳回正文引用位置（原来只有单向）
- 1.3.3.1：划词「或悬停」翻译（原来只有划词）
- 1.3.3.2：整篇翻译的可读性输出（Markdown 导出）+ 公式/代码保留英文

全部用 CDP 真实浏览器与真实鼠标事件，断言看数字和 DOM 状态：
1. 反向：点正文角标 → 浮层出现「正文引用 N 处」→ 点一条 → 回到那页且按句高亮；
2. 悬停：开关打开后把鼠标停在一个词上 → 0.4s 后浮层出现，译文含中文，
   浮层 pointer-events=none（否则会自我抖动）；关掉开关后不再触发；
3. 导出：中文译本接口 200、统计口径对得上、预览里 Markdown 标题被渲染、
   公式段落在导出里是原样围栏块而不是被翻译。
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

# 有参考文献角标的英文论文（Mistral 7B）
DOC_CITES = "d201464196e6f"
# 含大量公式块的文档（1607.06450 p5 已译，用于验证公式原样保留）
DOC_FORMULA = "dc7092cdf7b5c"


def _launch(port: int, profile: Path):
    return subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}",
         "--window-size=1700,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def run() -> dict:
    port = 9701 + (int(time.time()) % 50)
    profile = OUT / f"_p_enh_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = _launch(port, profile)
    result: dict = {"ok": False, "steps": [], "checks": {}, "error": ""}
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
        # 捕获页面运行期异常：新加的悬停/导出只要有一条 JS 报错，
        # 界面上往往只是"没反应"，没有这个就只能靠猜
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
            "window.__err='';"
            "window.addEventListener('error',e=>{window.__err+=(e.message||'')+' @'+(e.filename||'')+':'+(e.lineno||0)+'; ';});"
            "window.addEventListener('unhandledrejection',e=>{window.__err+='PROMISE:'+((e.reason&&e.reason.message)||'')+'; ';});"})
        call("Page.navigate", {"url": BASE + "/"})
        if not wait_for("window.__sciagent && window.__sciagent.docs.length > 3", 60):
            result["error"] = "主界面未就绪"
            return result

        # ============ 阶段 1：References → 正文反向跳转（1.3.2） ============
        found = js("""(async()=>{
          const vm = window.__sciagent;
          const doc = vm.docs.find(d => d.id === %s);
          if (!doc) return 'NO_DOC';
          await vm.openDoc(doc);
          // 找一页真的有引用角标的：不同 PDF 的角标分布差别很大，不能假定第 1 页有
          for (let p = 1; p <= Math.min(8, vm.pageCount); p++) {
            vm.page = p;
            await vm.loadTextLayer();
            await new Promise(r => setTimeout(r, 260));
            if (document.querySelector('.tw.cite')) {
              return JSON.stringify({page: p, cites: document.querySelectorAll('.tw.cite').length});
            }
          }
          return 'NO_CITE';
        })()""" % json.dumps(DOC_CITES))
        result["steps"].append({"find_cite": found})
        if not found or not str(found).startswith("{"):
            result["error"] = f"没找到含引用角标的页面：{found}"
            return result
        f = json.loads(found)

        opened = js("""(async()=>{
          const el = document.querySelector('.tw.cite');
          if (!el) return 'NO_EL';
          el.click();
          return 'OK';
        })()""")
        result["steps"].append({"click_cite": opened})
        if not wait_for("window.__sciagent.citePop.visible", 20):
            result["error"] = "引用浮层未出现"
            return result
        # 反查是异步的，等它有结果
        wait_for("window.__sciagent.citePop.occ > 0", 15)
        pop = js("""JSON.stringify({
          index: window.__sciagent.citePop.index,
          occ: window.__sciagent.citePop.occ,
          rows: document.querySelectorAll('.cite-pop-hit').length,
          sub: (document.querySelector('.cite-pop-sub')||{}).textContent || '',
          page: window.__sciagent.page
        })""")
        result["steps"].append({"popup": json.loads(pop)})
        p1 = json.loads(pop)
        shot("enh_cite_pop.png")

        jumped = js("""(async()=>{
          const hit = document.querySelector('.cite-pop-hit');
          if (!hit) return 'NO_HIT';
          hit.click();
          await new Promise(r => setTimeout(r, 1600));
          const boxes = [...document.querySelectorAll('.hl')];
          const hs = [...document.querySelectorAll('.tw')].map(e=>e.getBoundingClientRect().height).sort((a,b)=>a-b);
          const lineH = hs.length ? hs[Math.floor(hs.length/2)] : 0;
          return JSON.stringify({
            page: window.__sciagent.page,
            popupVisible: window.__sciagent.citePop.visible,
            rects: boxes.length,
            maxH: +Math.max(...boxes.map(e=>e.getBoundingClientRect().height), 0).toFixed(2),
            lineH: +lineH.toFixed(2),
            approx: boxes.filter(e=>e.classList.contains('approx')).length
          });
        })()""")
        result["steps"].append({"back_jump": json.loads(jumped)})
        j = json.loads(jumped)
        shot("enh_cite_back.png")

        back_ok = bool(
            p1["occ"] >= 1 and p1["rows"] >= 1
            and "正文引用" in p1["sub"]
            and j["rects"] >= 1 and not j["popupVisible"]
            and j["approx"] == 0
            and (j["maxH"] <= j["lineH"] * 1.8 if j["lineH"] else False)
        )
        result["checks"]["reverse_cite"] = back_ok

        # ============ 阶段 2：悬停翻译（1.3.3.1） ============
        # 选词三条硬约束（上一版每条都踩过）：
        #   ① 句子够长：作者行/页眉（"Mistral 7B Albert Q."）译出来没有信息量；
        #   ② 必须在视口内：CDP 的鼠标坐标不能用负数，上一版 y=-243 直接空转；
        #   ③ 不是引用角标：角标是点击目标，悬停弹窗会挡住点击。
        prep = js("""(async()=>{
          const vm = window.__sciagent;
          vm.hover.on = true;
          vm.saveHoverPref(true);
          vm.hideHover(true);
          vm.citePop.visible = false;
          vm.sel.visible = false;

          const pick = () => {
            const words = vm.tl.words || [];
            const tws = document.querySelectorAll('.tw');
            for (let i = 0; i < words.length; i++) {
              const w = words[i];
              if (w.cite || w.t.length < 5 || w.si == null) continue;
              const sent = (vm.tl.sents[w.si] || {}).text || '';
              if (sent.length < 60 || /arXiv:|Abstract We/.test(sent)) continue;
              const r = tws[i].getBoundingClientRect();
              if (r.top < 130 || r.bottom > innerHeight - 50) continue;
              if (r.left < 10 || r.right > innerWidth - 10) continue;
              // 必须"鼠标打在这个词上真的命中它"：PDF 文本层里页眉/水印的 span
              // 可能盖在正文词上面，那时悬停会译出别的内容——测试要反映真实交互，
              // 所以用 elementFromPoint 复核，而不是假设位置对就一定命中
              const cx = Math.round(r.left + r.width / 2);
              const cy = Math.round(r.top + r.height / 2);
              const hit = document.elementFromPoint(cx, cy);
              if (hit !== tws[i]) continue;
              return i;
            }
            return -1;
          };
          let idx = pick();
          if (idx < 0) {
            // 视口里没有合适的词就往下滚半屏再找一次：跳转后视口常停在标题页
            const stage = document.querySelector('.page-stage');
            if (stage) stage.scrollTo({top: stage.scrollTop + 420});
            await new Promise(r => setTimeout(r, 600));
            idx = pick();
          }
          if (idx < 0) return 'NO_WORD';
          const el = document.querySelectorAll('.tw')[idx];
          const r = el.getBoundingClientRect();
          return JSON.stringify({i: idx, x: Math.round(r.left + r.width/2),
                                 y: Math.round(r.top + r.height/2),
                                 t: vm.tl.words[idx].t, si: vm.tl.words[idx].si,
                                 sent: (vm.tl.sents[vm.tl.words[idx].si]||{}).text || ''});
        })()""")
        result["steps"].append({"hover_prep": prep})
        if not prep or not str(prep).startswith("{"):
            result["error"] = f"悬停准备失败：{prep}"
            return result
        hp = json.loads(prep)

        # 真实鼠标移动：dispatchEvent 造的不会触发 mouseenter 的浏览器内部状态
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": hp["x"] - 40, "y": hp["y"]})
        time.sleep(0.15)
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": hp["x"], "y": hp["y"]})
        got = wait_for("!!document.querySelector('.hover-tip .hover-tip-zh')", 25)
        time.sleep(0.4)
        tip = js("""(()=>{
          const tip = document.querySelector('.hover-tip');
          if (!tip) return JSON.stringify({visible: false});
          const zh = (tip.querySelector('.hover-tip-zh')||{}).textContent || '';
          const r = tip.getBoundingClientRect();
          const cs = getComputedStyle(tip);
          return JSON.stringify({
            visible: true, zh, zhLen: zh.length,
            han: (zh.match(/[\\u4e00-\\u9fff]/g) || []).length,
            pe: cs.pointerEvents,
            inViewport: r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
            rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
            css: {pos: cs.position, width: cs.width, maxW: cs.maxWidth, display: cs.display,
                  parent: tip.parentElement ? (tip.parentElement.className || tip.parentElement.id) : ''},
            src: (tip.querySelector('.hover-tip-src')||{}).textContent || ''
          });
        })()""")
        result["steps"].append({"hover_tip": json.loads(tip)})
        t = json.loads(tip)
        shot("enh_hover.png")
        # 局部 2 倍图：整屏缩放后浮层只有指甲盖大，看不清是否被压成竖排
        shot("enh_hover_zoom.png", {"x": t.get("rect", [480, 520])[0] - 20,
                                    "y": t.get("rect", [480, 520])[1] - 20,
                                    "width": 400, "height": 160, "scale": 2})
        text_w = js("""(()=>{
          const z = document.querySelector('.hover-tip-zh');
          if (!z) return 0;
          const r = z.getBoundingClientRect();
          return JSON.stringify({w: Math.round(r.width), h: Math.round(r.height),
                                 lines: Math.round(r.height / parseFloat(getComputedStyle(z).lineHeight))});
        })()""")
        result["steps"].append({"hover_text_box": json.loads(text_w) if text_w else {}})

        # 竖排词框（arXiv 侧边水印）必须让指针穿透，否则悬停正文会命中水印
        vert = js("""(()=>{
          const vm = window.__sciagent;
          const tws = document.querySelectorAll('.tw');
          let tall = 0, disabled = 0;
          (vm.tl.words || []).forEach((w, i) => {
            if (w.h > 40) {
              tall++;
              if (tws[i] && getComputedStyle(tws[i]).pointerEvents === 'none') disabled++;
            }
          });
          return JSON.stringify({tall, disabled});
        })()""")
        result["steps"].append({"vertical_words": json.loads(vert)})
        vw = json.loads(vert)

        # 缓存：移开再回来，应该立刻出现（不发第二次请求）
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5})
        time.sleep(0.6)
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": hp["x"], "y": hp["y"]})
        t0 = time.time()
        fast = wait_for("!!document.querySelector('.hover-tip .hover-tip-zh')", 8, 0.1)
        cache_ms = int((time.time() - t0) * 1000)
        diag = js("""JSON.stringify({
          on: window.__sciagent.hover.on,
          visible: window.__sciagent.hover.visible,
          loading: window.__sciagent.hover.loading,
          srcLen: (window.__sciagent.hover.src||'').length,
          zhLen: (window.__sciagent.hover.zh||'').length,
          cacheN: Object.keys(window.__sciagent.hover.cache||{}).length,
          at: (()=>{const e=document.elementFromPoint(%d,%d); return e ? (e.className||e.tagName) : 'null';})()
        })""" % (hp["x"], hp["y"]))
        result["steps"].append({"hover_diag": json.loads(diag)})

        # 关掉开关后不再触发
        off = js("""(async()=>{
          const vm = window.__sciagent;
          vm.hover.on = false;
          vm.saveHoverPref(false);
          vm.hideHover(true);
          return 'OK';
        })()""")
        time.sleep(0.3)
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5})
        time.sleep(0.2)
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": hp["x"], "y": hp["y"]})
        time.sleep(1.6)
        after_off = js("!!document.querySelector('.hover-tip')")
        call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5})

        sent_len = len(hp.get("sent", ""))
        hover_ok = bool(
            got and t.get("visible") and t.get("han", 0) >= 6
            and t.get("pe") == "none" and t.get("inViewport")
            and t.get("rect", [0, 0, 0, 0])[2] >= 300
            and sent_len <= 240
            and vw.get("tall", 0) >= 1 and vw.get("disabled") == vw.get("tall")
            and fast and cache_ms < 1200 and not after_off
        )
        result["checks"]["hover_translate"] = hover_ok
        result["steps"].append({"hover_cache": {"hit": fast, "ms": cache_ms},
                                "hover_off": {"triggered_after_off": after_off}})

        # ============ 阶段 3：中文译本导出（1.3.3.2） ============
        exp = js("""(async()=>{
          const vm = window.__sciagent;
          vm.centerView = 'bilingual';
          await new Promise(r => setTimeout(r, 700));
          const btns = [...document.querySelectorAll('.viewer-tools button')];
          const b = btns.find(x => x.textContent.includes('导出中译'));
          if (!b) return 'NO_BTN';
          b.click();
          return 'OK';
        })()""")
        result["steps"].append({"export_click": exp})
        if exp != "OK":
            result["error"] = f"没找到导出按钮：{exp}"
            return result
        if not wait_for("window.__sciagent.zh.markdown.length > 100", 40):
            result["error"] = "中文译本未加载"
            return result
        zh = js("""JSON.stringify({
          translated: window.__sciagent.zh.translated,
          total: window.__sciagent.zh.total,
          missing: window.__sciagent.zh.missing,
          verbatim: window.__sciagent.zh.verbatim,
          chars: window.__sciagent.zh.chars,
          ratio: window.__sciagent.zh.ratio,
          h1: document.querySelectorAll('.en-preview h1').length,
          h2: document.querySelectorAll('.en-preview h2').length,
          hasRawHash: /(^|\\n)#{1,2} /.test((document.querySelector('.en-preview')||{}).textContent || ''),
          drawer: !!document.querySelector('.el-drawer')
        })""")
        result["steps"].append({"zh_preview": json.loads(zh)})
        z = json.loads(zh)
        shot("enh_zh_export.png")

        # 公式保留：切到含公式的文档再导一次。
        # 统计的是**下载全文**而不是预览：preview 只有前 6000 字符，
        # 公式都在第 5 页以后，拿预览数围栏永远是 0（上一版就这么误判成失败）。
        fm_js = """(async()=>{
          const vm = window.__sciagent;
          vm.zh.visible = false;
          const doc = vm.docs.find(d => d.id === """ + json.dumps(DOC_FORMULA) + """);
          if (!doc) return 'NO_DOC';
          await vm.openDoc(doc);
          await vm.loadZh();
          const r = await fetch('/api/documents/' + doc.id + '/translate-zh.md');
          const txt = await r.text();
          return JSON.stringify({
            translated: vm.zh.translated, total: vm.zh.total,
            verbatim: vm.zh.verbatim,
            fences: (txt.match(/```/g) || []).length,
            hasCmd: txt.indexOf('\\\\begin') >= 0 || txt.indexOf('\\\\frac') >= 0,
            // 有些论文的公式解析出来是 Unicode 数学（σ μ ∑ ∂ ≈），不是 LaTeX 命令。
            // 断言"公式没被译成散文"要看这两者之一，只认 \\frac 会把正确结果判成失败。
            hasSymbols: /[μσΣ√∑∂≈]/.test(txt),
            chars: txt.length
          });
        })()"""
        fm = js(fm_js)
        result["steps"].append({"zh_formula": fm})
        fmj = json.loads(fm) if fm and str(fm).startswith("{") else {}

        # 下载接口：必须 200 且内容与预览同源
        dl = js("""(async()=>{
          const vm = window.__sciagent;
          const id = vm.zh.docId || %s;
          const r = await fetch(`/api/documents/${id}/translate-zh.md`);
          const txt = await r.text();
          return JSON.stringify({status: r.status, chars: txt.length,
            head: txt.slice(0, 60),
            hasFence: txt.includes('```'),
            hasTitle: /^# /.test(txt)});
        })()""" % json.dumps(DOC_FORMULA))
        result["steps"].append({"download": json.loads(dl)})
        d = json.loads(dl)

        export_ok = bool(
            z["translated"] >= 5 and z["total"] >= z["translated"]
            and z["h1"] >= 1 and not z["hasRawHash"] and z["drawer"]
            and d["status"] == 200 and d["hasTitle"]
            and fmj.get("verbatim", 0) >= 20 and fmj.get("fences", 0) >= 40
            and (fmj.get("hasCmd") or fmj.get("hasSymbols")) and d["hasFence"]
        )
        result["checks"]["zh_export"] = export_ok
        errs = js("window.__err || ''")
        result["checks"]["console_clean"] = not bool(errs)
        result["steps"].append({"page_errors": errs or ""})
        result["ok"] = bool(back_ok and hover_ok and export_ok and not errs)
        return result
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    t0 = time.time()
    r = run()
    r["elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    raise SystemExit(0 if r.get("ok") else 1)
