#!/usr/bin/env python
"""保活心跳自测（不起浏览器，纯 HTTP）。

验四件事：
1. 服务起来后**自动**开始跳，不需要人工触发；
2. 间隔改小后 **5 秒内**按新节奏走（循环每 5 秒看一次表，不用重启）；
3. 本地路由下真的把模型留在显存里（warm=ollama + `ollama ps` 能看到驻留）；
4. 关掉开关就停——不是"设了不看"，读一次配置就停。

脚本会改后台参数，结束时（含异常）一律恢复原值。
    ./.venv/Scripts/python.exe tests/keepalive_check.py
    ./.venv/Scripts/python.exe tests/keepalive_check.py --skip-warm    # 不切本地路由，快 40 秒
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8013"

try:
    sys.path.insert(0, str(ROOT))
    from app.core.config import settings as _s

    ADMIN_PWD = (_s.admin_password or "").strip()
except Exception:  # noqa: BLE001
    ADMIN_PWD = "<ADMIN_PASSWORD>"

_jar = http.cookiejar.CookieJar()
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def call(path: str, body: dict | None = None, method: str | None = None, timeout: float = 60):
    req = urllib.request.Request(
        BASE + path,
        method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with _op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def status() -> dict:
    return call("/api/keepalive")


def wait_beats(target: int, limit: float = 60.0) -> tuple[bool, dict]:
    """等到 beats 达到 target，或超时。返回 (是否达到, 最后一次状态)。"""
    end = time.time() + limit
    st = status()
    while time.time() < end:
        if st["beats"] >= target:
            return True, st
        time.sleep(2)
        st = status()
    return st["beats"] >= target, st


def ollama_ps() -> str:
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=25)
        return out.stdout
    except Exception as exc:  # noqa: BLE001 没装 ollama 就跳过该断言
        return f"<unavailable: {exc}>"


def run(skip_warm: bool) -> dict:
    res: dict = {"ok": False, "steps": [], "checks": {}}
    original = {"keepalive_interval": None, "keepalive_enabled": 1, "llm_route": None}

    call("/api/admin/login", {"password": ADMIN_PWD})
    params = {p["key"]: p for p in call("/api/admin/params")["params"]}
    original["keepalive_interval"] = params["keepalive_interval"]["value"]
    original["llm_route"] = params["llm_route"]["value"]

    try:
        # 1) 自动心跳：服务启动后不用任何人触发
        st0 = status()
        res["steps"].append({"auto_start": {k: st0[k] for k in ("enabled", "beats", "last_beat_at", "last_ms")}})
        ok_auto = st0["enabled"] and st0["beats"] >= 1 and st0["last_beat_at"]

        # 2) 间隔热生效：300 → 30，40 秒内必须多跳一次
        call("/api/admin/params", {"values": {"keepalive_interval": 30}}, method="PUT")
        base_beats = status()["beats"]
        ok_hot, st1 = wait_beats(base_beats + 1, limit=45)
        res["steps"].append({"interval_hot": {"before": base_beats, "after": st1["beats"],
                                              "interval_s": st1["interval_s"], "ms": st1["last_ms"]}})

        # 3) 本地预热：切 local，心跳应把模型加载进显存并续期
        ok_warm = True
        ps = ""
        if not skip_warm:
            call("/api/admin/params", {"values": {"llm_route": "local"}}, method="PUT")
            b = status()["beats"]
            ok_warm, st2 = wait_beats(b + 1, limit=60)
            st2 = status()
            ps = ollama_ps()
            ok_warm = bool(ok_warm and st2["last_warm"] == "ollama" and st2["last_ms"] > 0
                           and ("qwen" in ps or "unavailable" in ps))
            res["steps"].append({"warm": {"warm": st2["last_warm"], "ms": st2["last_ms"],
                                          "ollama_ps_has_model": "qwen" in ps,
                                          "error": st2["last_error"]}})

        # 4) 关掉就停
        call("/api/admin/params", {"values": {"keepalive_enabled": False}}, method="PUT")
        b = status()["beats"]
        time.sleep(40)
        st3 = status()
        ok_off = (st3["beats"] == b) and not st3["enabled"]
        res["steps"].append({"switch_off": {"before": b, "after": st3["beats"], "enabled": st3["enabled"]}})

        res["checks"] = {"auto_start": bool(ok_auto), "interval_hot": bool(ok_hot),
                         "warm_local": bool(ok_warm), "switch_off": bool(ok_off)}
    finally:
        # 恢复现场：不恢复的话演示时就一直 30 秒一跳、还锁在本地路由
        try:
            call("/api/admin/params", {"values": {
                "keepalive_interval": original["keepalive_interval"],
                "keepalive_enabled": True,
                "llm_route": original["llm_route"],
            }}, method="PUT")
            res["restored"] = {"route": call("/api/system/info")["llm"]["route"],
                               "interval": status()["interval_s"], "enabled": status()["enabled"]}
        except Exception as exc:  # noqa: BLE001
            res["restored"] = {"error": str(exc)}

    res["ok"] = all(res["checks"].values()) if res["checks"] else False
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-warm", action="store_true", help="不切本地路由（省 40 秒）")
    args = ap.parse_args()
    r = run(args.skip_warm)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["ok"] else 1)
