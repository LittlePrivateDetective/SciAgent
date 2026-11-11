#!/usr/bin/env python
"""外部保活：给运行中的 SciAgent 补一次心跳。

服务自己已经内置了 5 分钟循环（app/core/keepalive.py），**正常情况下不需要本脚本**。
它适用于两种场景：

1. 服务跑在另一台机器 / 容器里，本机只想用系统 cron 兜底；
2. 想用 cron 而不是进程内定时器来管节奏（服务重启后 cron 仍然照跑）。

用法：
    python scripts/keepalive.py                     # 打一次，退出码 0/1
    python scripts/keepalive.py --loop 300          # 常驻，每 300 秒一次
    python scripts/keepalive.py --base http://1.2.3.4:8013

系统 cron（Linux / macOS）：
    */5 * * * * cd /path/to/sciagent && .venv/bin/python scripts/keepalive.py >> /tmp/scia.log 2>&1

Windows 计划任务（每 5 分钟）：
    schtasks /Create /SC MINUTE /MO 5 /TN SciAgentKeepAlive /F ^
      /TR "<repo>\\.venv\\Scripts\\python.exe <repo>\\scripts\\keepalive.py"

删除：schtasks /Delete /TN SciAgentKeepAlive /F
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8013"


def beat(base: str, timeout: float = 40.0) -> tuple[bool, dict]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/keepalive/beat",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, {"error": f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'ignore')}"}
    except Exception as exc:  # noqa: BLE001 网络问题一律算失败，交给 cron 下一轮
        return False, {"error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser(description="SciAgent 保活心跳")
    ap.add_argument("--base", default=DEFAULT_BASE, help="服务地址，默认 http://127.0.0.1:8013")
    ap.add_argument("--loop", type=int, default=0, metavar="SEC", help="常驻循环间隔（秒），0 表示只打一次")
    args = ap.parse_args()

    fails = 0
    while True:
        ok, data = beat(args.base)
        stamp = time.strftime("%H:%M:%S")
        if ok:
            fails = 0
            print(f"[{stamp}] ok beats={data.get('beats')} last={data.get('last_ms')}ms "
                  f"warm={data.get('last_warm')} next={data.get('next_beat_at')}")
        else:
            fails += 1
            print(f"[{stamp}] FAIL({fails}) {data.get('error')}", file=sys.stderr)

        if args.loop <= 0:
            return 0 if ok else 1
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    raise SystemExit(main())
