"""后台控制台回归：参数中心能不能真的改到运行期，且改错了不会被吞掉。

四条断言，全部看接口回读的真实值，不看"界面上好像成功了"：
1. 打开 /admin 能拉到 37 个参数、5 个分组，密钥只显示"已配置"不回显明文；
2. 在界面上把 top_k 从 8 改成 6 → 保存 → 再读接口，值变 6 且来源标为"后台覆盖"；
3. 非法值走导入通道必须被整批拒绝，且不落库（读回仍是原值）；
4. 恢复默认后值回到 8、来源回到 .env；
另外跑一次模型连通性自测，确认自测按钮真的打到模型（有 token 用量）。
"""
from __future__ import annotations

import base64
import json
import os
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

# 口令从服务端配置读，测试里不再抄第二份（改了 .env 测试跟着走）
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.config import settings as _s

    ADMIN_PWD = (_s.admin_password or "").strip()
except Exception:  # noqa: BLE001
    # 不写死口令：仓库是公开的，口令只能来自 .env 或环境变量
    ADMIN_PWD = (os.environ.get("ADMIN_PASSWORD") or "").strip()


def run() -> dict:
    if not ADMIN_PWD:
        # 没配口令时后台本就不鉴权；这条用例是在验"口令保护"，无从验起，如实跳过
        return {"ok": False, "skipped": True,
                "error": "未设置 ADMIN_PASSWORD（.env 或环境变量），跳过后台口令用例"}
    port = 9711 + (int(time.time()) % 60)
    profile = OUT / f"_p_admin_{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--window-size=1600,1000", "about:blank"],
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

        def wait_for(expr: str, seconds: float = 30, interval: float = 0.3):
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
        # 捕获页面运行期异常：挂载失败时控制台只剩一片空白，没有这个就只能猜
        call("Page.addScriptToEvaluateOnNewDocument", {"source":
            "window.__err='';window.addEventListener('error',e=>{window.__err+=(e.message||'')+' @'+(e.filename||'')+':'+(e.lineno||0)+'; ';});"})
        call("Page.navigate", {"url": BASE + "/admin"})

        # 0) 先验证口令门：未登录必须读不到任何后台数据
        if not wait_for("window.__admin && window.__admin.authReady", 45):
            result["error"] = "控制台未就绪"
            return result
        gate = js("""(async()=>{
          const before = await fetch('/api/admin/params');
          const r = await fetch('/api/admin/session').then(x=>x.json());
          const vm = window.__admin;
          return JSON.stringify({status: before.status, enabled: r.enabled,
            authed: vm.authed, loginVisible: !!document.querySelector('.login-card')});
        })()""")
        result["steps"].append({"gate": json.loads(gate)})
        g = json.loads(gate)
        gate_ok = bool(g["status"] == 401 and g["enabled"] is True and g["loginVisible"] is True)
        shot("admin_login.png")

        # 0b) 错口令必须被拒；口令正确才进得去
        wrong = js("""(async()=>{
          const vm = window.__admin;
          vm.pwd = 'wrong-password';
          await vm.doLogin();
          const err = (document.querySelector('.login-err') || {}).textContent || '';
          return JSON.stringify({authed: vm.authed, err});
        })()""")
        result["steps"].append({"wrong_pwd": json.loads(wrong)})
        w = json.loads(wrong)
        wrong_ok = bool(w["authed"] is False and "不正确" in w["err"])

        login = js("""(async()=>{
          const vm = window.__admin;
          vm.pwd = '__PWD__';
          await vm.doLogin();
          return JSON.stringify({authed: vm.authed, params: vm.params.length});
        })()""".replace("__PWD__", ADMIN_PWD))
        result["steps"].append({"login": json.loads(login)})
        lg = json.loads(login)
        login_ok = bool(lg["authed"] is True and lg["params"] >= 30)

        if not wait_for("window.__admin && window.__admin.params.length > 20", 45):
            dbg = js("""JSON.stringify({err: window.__err, vue:!!window.Vue, ep:!!window.ElementPlus,
                 adm:!!window.__admin, title:document.title,
                 body:(document.body.innerText||'').slice(0,300),
                 html:((document.querySelector('#app')||{}).innerHTML||'').slice(0,200)})""")
            result["error"] = "控制台未就绪"
            result["steps"].append({"debug": dbg})
            return result
        time.sleep(0.8)

        base = js("""(()=>{
          const vm = window.__admin;
          const sec = vm.params.find(p => p.key === 'llm_api_key');
          return JSON.stringify({
            params: vm.params.length, groups: vm.groups.length,
            rows: document.querySelectorAll('.row').length,
            secretValue: sec.value, secretMasked: sec.masked, secretSet: sec.is_set,
            overridden: vm.meta.overridden,
            dirty: vm.dirtyKeys.length,
            topk: vm.form.top_k,
          });
        })()""")
        result["steps"].append({"base": json.loads(base)})
        b = json.loads(base)
        # dirty == 0 是一条重要断言：int 型布尔参数（0/1）与 el-switch 的 false
        # 曾经被判成"已修改"，一打开后台就顶着"未保存 1 项"
        base_ok = (b["params"] >= 30 and b["groups"] == 5 and b["rows"] >= 20
                   and b["secretValue"] == "" and b["secretSet"] is True
                   and b["dirty"] == 0 and b["overridden"] == 0)
        shot("admin_overview.png")
        layout = js("""(()=>{
          const g2 = document.querySelector('.grid2');
          const cs = getComputedStyle(g2);
          const kids = [...g2.children].map(el => ({
            cls: el.className, w: Math.round(el.getBoundingClientRect().width) }));
          const kv = document.querySelector('.kv');
          const head = document.querySelector('.card-head');
          return JSON.stringify({cols: cs.gridTemplateColumns, display: cs.display, kids,
            kvW: kv ? Math.round(kv.getBoundingClientRect().width) : 0,
            headW: head ? Math.round(head.getBoundingClientRect().width) : 0,
            gridW: Math.round(g2.getBoundingClientRect().width)});
        })()""")
        result["steps"].append({"layout": layout})
        lay = json.loads(layout) if layout else {}
        kids = (lay.get("kids") or [{}, {}])
        # 概览左右两卡必须大致等宽：fr 轨道曾被 min-content 撑爆，左卡只剩 129px
        lay_ok = bool(lay and len(kids) == 2 and lay["gridW"] > 800
                      and abs(kids[0]["w"] - kids[1]["w"]) < lay["gridW"] * 0.35
                      and lay["kvW"] > lay["gridW"] * 0.4)

        # 2) 界面上改 top_k → 保存 → 接口回读
        changed = js("""(async()=>{
          const vm = window.__admin;
          vm.view = 'params';
          await new Promise(r => setTimeout(r, 300));
          const row = [...document.querySelectorAll('.row')].find(el =>
            el.querySelector('.row-title') && el.querySelector('.row-title').textContent.includes('召回条数'));
          if (!row) return 'NO_ROW';
          const input = row.querySelector('input');
          input.value = '6';
          input.dispatchEvent(new Event('input', {bubbles:true}));
          input.dispatchEvent(new Event('change', {bubbles:true}));
          await new Promise(r => setTimeout(r, 300));
          const dirty = document.querySelectorAll('.row.dirty').length;
          // 点保存
          const btn = [...document.querySelectorAll('.toolbar button')].find(x => x.textContent.includes('保存'));
          if (!btn) return 'NO_BTN';
          btn.click();
          await new Promise(r => setTimeout(r, 1500));
          const r = await fetch('/api/admin/params').then(x => x.json());
          const p = r.params.find(x => x.key === 'top_k');
          return JSON.stringify({dirty, value: p.value, source: p.source});
        })()""")
        result["steps"].append({"change": json.loads(changed) if str(changed).startswith("{") else changed})
        c = json.loads(changed) if str(changed).startswith("{") else {}
        shot("admin_params.png")
        change_ok = bool(c.get("dirty", 0) >= 1 and c.get("value") == 6 and c.get("source") == "db")

        # 3) 非法值走导入：整批拒绝且不落库
        bad = js("""(async()=>{
          const r = await fetch('/api/admin/import', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({values:{top_k: 999, rerank_top_n: 3}})
          }).then(x=>x.json());
          const cur = await fetch('/api/admin/params').then(x=>x.json());
          return JSON.stringify({ok: r.ok, rejected: (r.rejected||[]).length,
            topk: cur.params.find(x=>x.key==='top_k').value,
            rtn: cur.params.find(x=>x.key==='rerank_top_n').value});
        })()""")
        result["steps"].append({"import_bad": json.loads(bad)})
        ib = json.loads(bad)
        reject_ok = bool(ib["ok"] is False and ib["rejected"] >= 1 and ib["topk"] == 6 and ib["rtn"] == 4)

        # 4) 恢复默认
        reset = js("""(async()=>{
          await fetch('/api/admin/params/reset', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({keys:['top_k']})
          });
          const r = await fetch('/api/admin/params').then(x=>x.json());
          const p = r.params.find(x=>x.key==='top_k');
          // 恢复默认不能把 .env 里的密钥一起清空：那样服务当场失联，必须重启
          const s = r.params.find(x=>x.key==='llm_api_key');
          return JSON.stringify({value: p.value, source: p.source, secretSet: s.is_set,
                                 overridden: r.meta.overridden});
        })()""")
        result["steps"].append({"reset": json.loads(reset)})
        rs = json.loads(reset)
        reset_ok = bool(rs["value"] == 8 and rs["source"] in {"env", "default"}
                        and rs.get("secretSet") is True)

        # 5) 模型自测（真实调用）
        test = js("""(async()=>{
          const vm = window.__admin;
          vm.openTest();
          vm.test.maxTokens = 512;
          await vm.runTest();
          await new Promise(r => setTimeout(r, 300));
          return JSON.stringify({
            ok: vm.test.result && vm.test.result.ok,
            route: vm.test.result && vm.test.result.route,
            ms: vm.test.result && vm.test.result.ms,
            tokens: vm.test.result && vm.test.result.completion_tokens,
            preview: ((vm.test.result && vm.test.result.preview) || '').slice(0, 60),
          });
        })()""")
        result["steps"].append({"model_test": json.loads(test)})
        mt = json.loads(test)
        test_ok = bool(mt.get("ok") and (mt.get("tokens") or 0) > 0)
        time.sleep(0.5)
        shot("admin_test.png")

        result["checks"] = {
            "gate_401": gate_ok, "wrong_pwd": wrong_ok, "login": login_ok,
            "load": base_ok, "layout": lay_ok, "change_applied": change_ok,
            "reject_all_or_nothing": reject_ok, "reset": reset_ok, "model_test": test_ok,
            "params": b["params"], "rows": b["rows"],
        }
        result["ok"] = bool(gate_ok and wrong_ok and login_ok and base_ok and lay_ok
                            and change_ok and reject_ok and reset_ok and test_ok)
        return result
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2)[:4000])
