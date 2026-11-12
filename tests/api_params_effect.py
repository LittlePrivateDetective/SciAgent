"""后台参数「改完不重启」是否真的改到了运行期。

与 ui_admin 的分工：
    ui_admin 验的是界面——口令门、改库、非法值整批拒绝、恢复默认；
    本脚本验的是更难证的一半：**改完之后，业务请求的行为是不是真的变了**。
    "接口回读是 6" 只能证明数据库里写了 6，证明不了检索/模型真的用了 6。

每条用例都按「改 → 打一次真实业务请求 → 看行为差异 → 还原」走，
还原放在 finally，中途失败也不会把服务留在奇怪状态（这正是 D-76 的教训）。

用法：
    ./.venv/Scripts/python.exe tests/api_params_effect.py
    ./.venv/Scripts/python.exe tests/api_params_effect.py --base http://127.0.0.1:8013

覆盖的四项，选的都是「有可观测行为差」的，纯配置项（如日志级别）不在此列：
    llm_mock            → 问答链路整体切到模拟文案（route 层真分叉）
    llm_thinking        → /api/chat 回参 meta.thinking 跟随全局设置
    keepalive_interval  → /api/keepalive 报出的间隔跟着变（后台线程读的是同一个值）
    max_upload_mb       → 上传超限立刻 413，且不会落盘
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "qa" / "evidence"

ADMIN_PWD = (os.environ.get("ADMIN_PASSWORD") or "").strip()
if not ADMIN_PWD:
    try:
        sys.path.insert(0, str(ROOT))
        from app.core.config import settings as _s  # noqa: E402

        ADMIN_PWD = (_s.admin_password or "").strip()
    except Exception:  # noqa: BLE001
        ADMIN_PWD = ""

TOUCHED = ["llm_mock", "llm_thinking", "keepalive_interval", "max_upload_mb"]


class Ctx:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=300, trust_env=False)
        self.rows: list[dict] = []
        self.before: dict[str, dict] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:28s} {detail[:110]}")
        return bool(ok)

    def params(self) -> dict:
        r = self.c.get("/api/admin/params")
        if r.status_code != 200:
            raise RuntimeError(f"读参数失败 {r.status_code}: {r.text[:200]}")
        body = r.json()
        return {p["key"]: p for p in body["params"]}

    def put(self, **values) -> dict:
        r = self.c.put("/api/admin/params", json={"values": values, "actor": "api_params_effect"})
        return {"status": r.status_code, "body": r.json()}

    def reset(self, keys: list[str]) -> dict:
        r = self.c.post("/api/admin/params/reset",
                        json={"keys": keys, "actor": "api_params_effect"})
        return {"status": r.status_code, "body": r.json()}


def pick_big_pdf(min_mb: float = 2.0) -> Path | None:
    cands = sorted((ROOT / "data" / "pdf").glob("*.pdf"), key=lambda p: -p.stat().st_size)
    for p in cands:
        if p.stat().st_size > min_mb * 1024 * 1024:
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8013")
    ap.add_argument("--skip-auth", action="store_true", help="服务端未配口令时跳过鉴权断言")
    args = ap.parse_args()

    ctx = Ctx(args.base)
    print(f"目标：{ctx.base}\n")
    print("【鉴权】")
    anon = httpx.Client(base_url=ctx.base, timeout=30, trust_env=False)
    unauth = anon.get("/api/admin/params")
    sess = anon.get("/api/admin/session").json()
    if args.skip_auth or not sess.get("enabled"):
        ctx.check("未登录读参数被拒", True, "服务端未配 ADMIN_PASSWORD，鉴权未启用（跳过）")
        ctx.check("登录", True, "未启用口令，直接可读写")
    else:
        if not ADMIN_PWD:
            print("  未拿到 ADMIN_PASSWORD（.env 或环境变量），无法验鉴权，退出")
            return 2
        ctx.check("未登录读参数被拒", unauth.status_code == 401,
                  f"GET /api/admin/params -> {unauth.status_code}")
        bad = anon.post("/api/admin/login", json={"password": "not-the-password"})
        ctx.check("错口令被拒", bad.status_code == 401, f"-> {bad.status_code}")
        ok = anon.post("/api/admin/login", json={"password": ADMIN_PWD})
        ctx.check("正确口令签发会话", ok.status_code == 200 and anon.get("/api/admin/params").status_code == 200,
                  f"-> {ok.status_code}，随后读参数 200")

    # 登录态复用 anon 的 cookie：本脚本所有写操作都走它
    ctx.c.cookies.update(anon.cookies)
    if ctx.c.get("/api/admin/params").status_code != 200:
        print("  未取得后台会话，后续用例无法进行")
        return 2

    ctx.before = ctx.params()
    print("  当前值：" + " / ".join(f"{k}={ctx.before[k]['value']}({ctx.before[k]['source']})"
                                 for k in TOUCHED))

    done = {"restored": False}

    def restore() -> bool:
        """把动过的四项写回原值/原来源。幂等——信号和 finally 可能都调它。"""
        if done["restored"]:
            return True
        print("\n【还原】")
        for key in TOUCHED:
            p = ctx.before.get(key)
            if not p:
                continue
            if p["source"] == "db":
                ctx.put(**{key: p["value"]})
            else:
                ctx.reset([key])
        after = ctx.params()
        fixed = all(after[k]["value"] == ctx.before[k]["value"] and after[k]["source"] == ctx.before[k]["source"]
                    for k in TOUCHED if k in after)
        ctx.check("参数已恢复原值与原来源", fixed,
                  " / ".join(f"{k}={after[k]['value']}({after[k]['source']})" for k in TOUCHED if k in after))
        done["restored"] = True
        return fixed

    # 被 kill / Ctrl-C 打断时也要还原：第一版没有这个守卫，脚本超时被 SIGTERM
    # 掐断，把 llm_mock=true 留在了库里——下一次问答全变成【模拟模式】，排查半天。
    # 与 D-76 同一个教训：测试必须自己负责把环境复原。
    def _on_signal(signum, _frame):
        print(f"\n收到信号 {signum}，先还原参数…")
        try:
            restore()
        except Exception as exc:  # noqa: BLE001 还原失败也要把原因说出来
            print(f"  还原失败：{type(exc).__name__}: {exc}")
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        # ---------------- 1. 强制模拟模式 ----------------
        print("\n【llm_mock：问答链路是否整体切到模拟】")
        r = ctx.put(llm_mock=True)
        ctx.check("保存 llm_mock", r["status"] == 200 and r["body"].get("ok"),
                  f"applied={len(r['body'].get('applied') or [])}")
        # 自测接口：route 必须变成 mock，且文案带【模拟模式】
        t = ctx.c.post("/api/admin/test-model", json={"prompt": "一句话说明什么是 RAG", "max_tokens": 64}).json()
        ctx.check("自测走 mock 路由", t.get("route") == "mock" and "模拟模式" in (t.get("preview") or ""),
                  f"route={t.get('route')} ms={t.get('ms')}")
        # 真正的业务链路：不重启，直接问一句
        chat = ctx.c.post("/api/chat", json={"question": "Transformer 的注意力复杂度是多少", "mode": "qa"}).json()
        ctx.check("问答链路同步切到 mock", "模拟模式" in (chat.get("answer") or ""),
                  f"answer={(chat.get('answer') or '')[:40]}")
        # 证据仍要来自真实检索：mock 只替模型，不该把检索也短路掉
        ctx.check("mock 下检索仍返回证据", len(chat.get("evidence") or []) >= 3,
                  f"evidence={len(chat.get('evidence') or [])}")

        # ---------------- 2. 思考开关 ----------------
        print("\n【llm_thinking：全局开关是否作用于每次问答】")
        ctx.put(llm_thinking=True)
        on = ctx.c.post("/api/chat", json={"question": "什么是混合检索", "mode": "qa"}).json()
        ctx.put(llm_thinking=False)
        off = ctx.c.post("/api/chat", json={"question": "什么是混合检索", "mode": "qa"}).json()
        ctx.check("meta.thinking 跟随全局 True", (on.get("meta") or {}).get("thinking") is True,
                  f"meta={on.get('meta')}")
        ctx.check("meta.thinking 跟随全局 False", (off.get("meta") or {}).get("thinking") is False,
                  f"meta={off.get('meta')}")
        ctx.put(llm_mock=False)  # 后面的真实调用要能出内容

        # ---------------- 3. 保活间隔 ----------------
        print("\n【keepalive_interval：后台线程读的是同一个值】")
        ctx.put(keepalive_interval=900)
        ka1 = ctx.c.get("/api/keepalive").json()
        ctx.put(keepalive_interval=1800)
        ka2 = ctx.c.get("/api/keepalive").json()
        # 字段名是 interval_s（秒），不是 interval——api_sweep 里那条只断言"有这个键"，
        # 这里要断言数值，所以必须用真实字段名
        ctx.check("保活状态报 900s", ka1.get("interval_s") == 900, f"interval_s={ka1.get('interval_s')}")
        ctx.check("改成 1800s 后跟着变", ka2.get("interval_s") == 1800, f"interval_s={ka2.get('interval_s')}")

        # ---------------- 4. 上传上限 ----------------
        print("\n【max_upload_mb：超限立刻拒绝且不落盘】")
        pdf = pick_big_pdf(2.0)
        if pdf is None:
            ctx.check("上传上限生效", True, "库内无 2MB 以上 PDF，跳过")
        else:
            n_before = ctx.c.get("/api/system/info").json()["counts"]["documents"]
            ctx.put(max_upload_mb=1)
            with pdf.open("rb") as fh:
                up = ctx.c.post("/api/documents/upload",
                                files={"file": (pdf.name, fh, "application/pdf")})
            n_after = ctx.c.get("/api/system/info").json()["counts"]["documents"]
            ctx.check("超限上传被拒 413", up.status_code == 413,
                      f"{pdf.name} {pdf.stat().st_size / 1048576:.1f}MB -> {up.status_code} {up.text[:60]}")
            ctx.check("被拒后库内数量不变", n_before == n_after,
                      f"{n_before} -> {n_after}")

        # ---------------- 5. 越界与整批拒绝 ----------------
        print("\n【校验：非法值不能留中间态】")
        temp_before = ctx.params()["llm_temperature"]
        # 同一批里故意混一个合法项（temperature=1.0）和一个越界项（9999MB）：
        # 合法项也不许写进去，否则就是"改 10 项成功 7 项"的老毛病
        bad = ctx.put(llm_temperature=1.0, max_upload_mb=9999)
        after_bad = ctx.params()["llm_temperature"]
        ctx.check("越界整批拒绝", bad["body"].get("ok") is False and len(bad["body"].get("rejected") or []) >= 1,
                  f"rejected={[x['key'] for x in bad['body'].get('rejected') or []]}")
        ctx.check("同批合法项也没被写进去",
                  after_bad["value"] == temp_before["value"] and after_bad["source"] == temp_before["source"],
                  f"temperature={after_bad['value']}({after_bad['source']})，拒绝前={temp_before['value']}({temp_before['source']})")

        # ---------------- 6. 审计 ----------------
        print("\n【审计】")
        au = ctx.c.get("/api/admin/audit?limit=20").json()
        acts = [a["action"] for a in au.get("items") or []]
        ctx.check("改动留痕", "params_update" in acts, f"最近动作={acts[:6]}")
    finally:
        # 还原：原本是后台覆盖的就写回原值，否则清掉覆盖行（回到 .env/默认）
        restore()

    total = len(ctx.rows)
    passed = sum(1 for r in ctx.rows if r["pass"])
    res = {"base": ctx.base, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "total": total, "pass": passed, "fail": total - passed, "rows": ctx.rows}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"params_effect_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{'=' * 66}\n共 {total} 项：PASS {passed} / FAIL {total - passed}")
    for r in ctx.rows:
        if not r["pass"]:
            print(f"  FAIL {r['name']} {r['detail'][:120]}")
    print(f"证据 -> {path}")
    return 0 if total == passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
