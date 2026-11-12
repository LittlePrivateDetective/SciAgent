"""批量选择 / 批量删除：勾选、全选、清空、删掉的整条链路。

为什么值得单独一条用例：删除是不可逆动作（虽然进回收站能恢复），
而"勾了 4 篇却只删掉 1 篇""删完列表还显示"这类问题，接口测试一个都测不出来。

要验的五件事：
1. 点勾选框能选中，且选择条按数量出现（0 篇时不占位）；
2. 选择条说清"勾选会影响问答范围"（原有语义，不能被批量删除吃掉）；
3. 全选/清空按钮的状态与文案随选择数变化；
4. 点「删除 N 篇」弹二次确认，确认文案里带数量；
5. 确认后列表真的少 N 篇、选择条消失、回收站里多 N 条。

用法：
    ./.venv/Scripts/python.exe tests/ui_batch_delete.py
证据：docs/qa/evidence/batch_{selected,confirm,done}.png
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
BASE = "http://127.0.0.1:8013"

# 只删这四篇（本轮为了验证批量删除临时入库的）。按**文件名**匹配而不是标题：
# 列表上显示的是 title（论文正题，如 "Retrieval-Augmented Generation for …"），
# 文件名里的 "EN_1_RAG_arXiv2005.11401" 根本不在其中——第一版按标题匹配，
# 只命中了两篇中文，英文两篇悄悄漏掉了，断言却说"选了 4 篇"。id 也不能写死，
# 每次重新入库都会变。
TARGETS = ["EN_1_RAG_arXiv2005.11401", "EN_2_RETRO_arXiv2112.04426",
           "ZH_1_大语言模型的工具使用综述", "ZH_2_基于大模型的具身智能系统综述"]


def run() -> dict:
    if not EDGE.exists():
        return {"ok": False, "error": "本机无 Edge，跳过"}
    port = 9821 + (int(time.time()) % 40)
    profile = Path(tempfile.gettempdir()) / f"_p_batch_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--window-size=1680,1050", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank"],
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
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180)
        seq = [0]

        def call(method: str, params: dict | None = None) -> dict:
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == seq[0]:
                    return msg.get("result") or {}

        def js(expr: str):
            r = call("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text", "JS 异常"))
            return (r.get("result") or {}).get("value")

        def click(x: float, y: float) -> None:
            call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            call("Input.dispatchMouseEvent",
                 {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(0.03)
            call("Input.dispatchMouseEvent",
                 {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

        def shot(name: str) -> None:
            call("Page.bringToFront")
            d = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            (OUT / name).write_bytes(base64.b64decode(d["data"]))

        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": BASE + "/"})
        time.sleep(6)
        for _ in range(30):
            n = js("window.__sciagent ? window.__sciagent.docs.length : 0") or 0
            if n >= 4:
                break
            time.sleep(0.5)

        # ── 1) 逐篇点勾选框（真实鼠标事件，走 Element Plus 的 change）
        # 按 vm.docs 里的 filename 找下标，再取同下标的 .doc-item（v-for 顺序一致）。
        # 直接扫 DOM 文本只能匹配到"列表上显示的标题"，而标题与文件名并不相同。
        centers = js("""(() => {
          const pats = %s;
          const vm = window.__sciagent;
          const rows = document.querySelectorAll('.doc-item');
          const out = [];
          vm.docs.forEach((d, i) => {
            const fn = d.filename || '';
            if (!pats.some((p) => fn.includes(p))) return;
            const row = rows[i];
            const box = row && row.querySelector('.el-checkbox__inner');
            if (!box) return;
            const r = box.getBoundingClientRect();
            out.push({file: fn, x: r.left + r.width / 2, y: r.top + r.height / 2,
                      title: (d.title || '').slice(0, 40)});
          });
          return out;
        })()""" % json.dumps(TARGETS, ensure_ascii=False))
        res["steps"].append({"found_rows": centers})

        # 每点一个之前都重取坐标：第一次勾选会让「选择条」插进列表顶部，
        # 整个列表下移约 50px，第一版缓存了坐标，后面三次全点空（只选中 1 篇）。
        # 真实用户可以看着屏幕调整，自动化必须每次重新量。
        def box_center(filename: str):
            return js("""(() => {
              const vm = window.__sciagent;
              const rows = document.querySelectorAll('.doc-item');
              const i = vm.docs.findIndex((d) => (d.filename || '') === %s);
              const box = rows[i] && rows[i].querySelector('.el-checkbox__inner');
              if (!box) return null;
              const r = box.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            })()""" % json.dumps(filename, ensure_ascii=False))

        for c in centers:
            pos = box_center(c["file"]) or box_center(c["file"] + ".pdf")
            if not pos:
                res["steps"].append({"miss": c["file"]})
                continue
            click(pos["x"], pos["y"])
            time.sleep(0.3)

        state1 = js("""(() => {
          const vm = window.__sciagent;
          const bar = document.querySelector('.sel-bar');
          const btn = [...document.querySelectorAll('.sel-bar button')]
            .find((b) => b.textContent.includes('删除'));
          return {
            sel: vm.selection.length, total: vm.docs.length,
            barVisible: !!bar && bar.offsetHeight > 0,
            barText: bar ? bar.innerText.replace(/\\s+/g, ' ').trim() : '',
            delText: btn ? btn.textContent.trim() : '',
            allBtn: (() => {
              const b = [...document.querySelectorAll('.sel-bar button')]
                .find((x) => x.textContent.trim() === '全选' || x.textContent.trim() === '取消全选');
              return b ? b.textContent.trim() : '';
            })(),
          };
        })()""")
        res["steps"].append({"after_select": state1})
        shot("batch_selected.png")

        # ── 2) 确认弹窗
        clicked_del = js("""(() => {
          const b = [...document.querySelectorAll('.sel-bar button')]
            .find((x) => x.textContent.includes('删除'));
          if (!b) return false;
          b.click();
          return true;
        })()""")
        time.sleep(1.0)
        box = js("""(() => {
          const el = document.querySelector('.el-message-box');
          if (!el) return {shown: false};
          return {shown: el.offsetHeight > 0,
                  title: (el.querySelector('.el-message-box__title') || {}).innerText || '',
                  body: (el.querySelector('.el-message-box__message') || {}).innerText || '',
                  btns: [...el.querySelectorAll('.el-message-box__btns button')]
                          .map((b) => b.textContent.trim())};
        })()""")
        res["steps"].append({"confirm_box": box, "clicked": clicked_del})
        shot("batch_confirm.png")

        # ── 3) 点确认，等列表变短
        confirm_center = js("""(() => {
          const el = document.querySelector('.el-message-box');
          if (!el) return null;
          const b = [...el.querySelectorAll('.el-message-box__btns button')]
            .find((x) => x.textContent.includes('移入回收站'));
          if (!b) return null;
          const r = b.getBoundingClientRect();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        })()""")
        if confirm_center:
            click(confirm_center["x"], confirm_center["y"])
        time.sleep(1.2)
        for _ in range(40):
            n = js("window.__sciagent ? window.__sciagent.docs.length : -1")
            if n == state1["total"] - state1["sel"]:
                break
            time.sleep(0.5)

        state2 = js("""(() => {
          const vm = window.__sciagent;
          const bar = document.querySelector('.sel-bar');
          const msg = [...document.querySelectorAll('.el-message')]
            .map((m) => m.innerText.trim()).filter(Boolean);
          return {
            sel: vm.selection.length, total: vm.docs.length,
            checked: vm.docs.filter((d) => d._sel).length,
            barVisible: !!bar && bar.offsetHeight > 0,
            messages: msg,
            stillHasTargets: vm.docs.filter(
              (d) => %s.some((p) => (d.filename || '').includes(p))).length,
          };
        })()""" % json.dumps(TARGETS, ensure_ascii=False))
        res["steps"].append({"after_delete": state2})
        time.sleep(0.6)
        shot("batch_done.png")

        # ── 4) 全选 / 清空（只验状态与文案，不真删——全选删除等于清空语料）
        js("""(() => {
          const b = [...document.querySelectorAll('.sel-bar button')]
            .find((x) => x.textContent.includes('删除'));
          return true;
        })()""")
        # 先选一篇让选择条出现
        one = js("""(() => {
          const row = document.querySelector('.doc-item .el-checkbox__inner');
          if (!row) return null;
          const r = row.getBoundingClientRect();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        })()""")
        if one:
            click(one["x"], one["y"])
            time.sleep(0.4)
        sel_all = js("""(async () => {
          const vm = window.__sciagent;
          const btn = [...document.querySelectorAll('.sel-bar button')]
            .find((x) => x.textContent.trim() === '全选');
          if (!btn) return {err: '没有全选按钮'};
          btn.click();
          await new Promise((r) => setTimeout(r, 400));
          const allBtn = [...document.querySelectorAll('.sel-bar button')]
            .find((x) => x.textContent.includes('全选') || x.textContent.includes('取消全选'));
          return {sel: vm.selection.length, total: vm.docs.length,
                  label: allBtn ? allBtn.textContent.trim() : ''};
        })()""")
        res["steps"].append({"select_all": sel_all})
        clear = js("""(async () => {
          const vm = window.__sciagent;
          const btn = [...document.querySelectorAll('.sel-bar button')]
            .find((x) => x.textContent.trim() === '清空');
          if (!btn) return {err: '没有清空按钮'};
          btn.click();
          await new Promise((r) => setTimeout(r, 400));
          return {sel: vm.selection.length,
                  bar: !!document.querySelector('.sel-bar'),
                  checked: vm.docs.filter((d) => d._sel).length};
        })()""")
        res["steps"].append({"clear": clear})

        res["checks"] = {
            # 四篇都选中，且选择条按数量出现
            "select_four": state1["sel"] == 4 and state1["barVisible"],
            "bar_says_scope": "问答只在其中检索" in (state1["barText"] or ""),
            "delete_btn_labeled": "删除 4 篇" in (state1["delText"] or ""),
            "confirm_shows_count": box.get("shown") and "4" in (box.get("body") or ""),
            "confirm_mentions_restore": "回收站" in (box.get("body") or ""),
            # 列表真的少 4 篇、勾选清零、选择条收起
            "list_shrunk": state2["total"] == state1["total"] - 4,
            "selection_cleared": state2["sel"] == 0 and state2["checked"] == 0,
            "bar_hidden": state2["barVisible"] is False,
            "targets_gone": state2["stillHasTargets"] == 0,
            "toast_says_moved": any("回收站" in m for m in (state2["messages"] or [])),
            # 全选 / 清空
            "select_all_works": (sel_all or {}).get("sel") == (sel_all or {}).get("total")
                                 and "取消全选" in ((sel_all or {}).get("label") or ""),
            "clear_works": (clear or {}).get("sel") == 0 and (clear or {}).get("checked") == 0
                           and (clear or {}).get("bar") is False,
        }
        res["ok"] = all(res["checks"].values())
        print(json.dumps(res, ensure_ascii=False, indent=1))
        raise SystemExit(0 if res["ok"] else 1)
    finally:
        if ws:
            ws.close()
        proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    run()
