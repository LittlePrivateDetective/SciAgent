"""论文追踪：定期扫描 arXiv，把「值得看的新论文」攒起来（官方 1.4.2）。

为什么单独一个调度器，而不是并进 keepalive：
- 保活是"保命"，失败要重试、间隔固定；追踪是"干活"，一次要调 arXiv + 逐篇生成
  中文推荐理由，耗时几十秒甚至几分钟，失败也不该影响服务可用。两者的失败语义不同，
  混在一起会让保活日志被追踪的超时刷屏。
- 但调度骨架刻意照抄 keepalive：每 5 秒看表、改参数 5 秒内生效、任务丢线程里跑。
  同一套模式重复两次，比抽一个"通用调度框架"更好读。

数据落哪里：只放内存 + `app_settings` 里一条"上次扫描时间"。
不建新表——追踪结果是"推荐候选"，随时可以重算，没必要为它加迁移。
重启后第一次循环会立刻补扫一次（`last_run_at` 为空 → 立即执行）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

TICK = 5.0            # 看表间隔，与 keepalive 一致：改参数 5 秒内生效
MANUAL_GAP = 60.0     # 手动触发的冷却，防止连点把 arXiv 与模型都刷爆
MAX_ITEMS = 8

_state: dict = {
    "started_at": None,
    "last_run_at": None,
    "last_ms": None,
    "runs": 0,
    "fails": 0,
    "last_count": 0,
    "last_error": "",
    "items": [],          # 最近一次的推荐结果（只留展示需要的字段）
}
_lock = threading.Lock()
_task: asyncio.Task | None = None


def _paper_brief(it: dict) -> dict:
    """只留界面要显示的字段，避免把整段 arXiv 摘要塞进内存。"""
    return {
        "arxiv_id": it.get("arxiv_id", ""),
        "title": (it.get("title") or "")[:200],
        "published": it.get("published", ""),
        "reason": (it.get("reason") or "")[:300],
        "relevance": it.get("relevance", 0),
        "github": it.get("github", ""),
        "in_library": bool(it.get("in_library")),
    }


def run_once(manual: bool = False) -> dict:
    """跑一次追踪。同步函数——循环和 HTTP 接口都直接调它。"""
    now = time.time()
    with _lock:
        last = _state["last_run_at"]
        if manual and last and now - last < MANUAL_GAP:
            return {"ok": False, "error": f"距上次扫描不足 {int(MANUAL_GAP)} 秒，请稍后再试"}

    t0 = time.time()
    err = ""
    items: list[dict] = []
    note = ""
    try:
        from app.db.session import SessionLocal
        from app.research import discover

        with SessionLocal() as db:
            r = discover.recommend(db, limit=MAX_ITEMS, days=int(settings.discover_watch_days))
        if r.get("ok"):
            items = [_paper_brief(it) for it in (r.get("items") or [])]
            note = r.get("note", "")
        else:
            err = r.get("error") or "推荐失败"
    except Exception as exc:  # noqa: BLE001 追踪失败不能影响服务
        err = str(exc)[:300]

    elapsed = int((time.time() - t0) * 1000)
    with _lock:
        _state["last_run_at"] = time.time()
        _state["last_ms"] = elapsed
        _state["runs"] += 1
        if err:
            _state["fails"] += 1
            _state["last_error"] = err
        else:
            _state["last_error"] = ""
            _state["last_count"] = len(items)
            if items or note:
                _state["items"] = items
        snap = _snapshot_locked()

    if err:
        log.warning("论文追踪失败：%s", err)
    else:
        log.info("论文追踪完成：%d 篇新论文，耗时 %dms%s", len(items), elapsed,
                 f"（{note}）" if note else "")
    return {"ok": not err, "error": err, "note": note, "items": items, "status": snap}


def _snapshot_locked() -> dict:
    s = _state
    nxt = None
    if s["last_run_at"]:
        nxt = datetime.fromtimestamp(s["last_run_at"] + max(300, int(settings.discover_watch_interval)))
    return {
        "enabled": bool(settings.discover_watch_enabled),
        "interval_s": int(settings.discover_watch_interval),
        "days": int(settings.discover_watch_days),
        "started_at": s["started_at"],
        "last_run_at": (datetime.fromtimestamp(s["last_run_at"]).isoformat(timespec="seconds")
                        if s["last_run_at"] else None),
        "next_run_at": nxt.isoformat(timespec="seconds") if nxt else None,
        "last_ms": s["last_ms"],
        "runs": s["runs"],
        "fails": s["fails"],
        "last_count": s["last_count"],
        "last_error": s["last_error"],
        "items": list(s["items"]),
    }


def status() -> dict:
    with _lock:
        return _snapshot_locked()


async def _loop() -> None:
    with _lock:
        _state["started_at"] = datetime.now().isoformat(timespec="seconds")
    log.info("论文追踪循环已启动：间隔 %ss", settings.discover_watch_interval)

    last = 0.0
    while True:
        await asyncio.sleep(TICK)
        if not settings.discover_watch_enabled:
            last = 0.0            # 关掉再打开时立刻补一次
            continue
        interval = max(300, int(settings.discover_watch_interval))
        if time.time() - last < interval:
            continue
        last = time.time()
        try:
            # 一次扫描会调 arXiv + 逐篇写推荐理由，几十秒起步，必须丢线程
            await asyncio.to_thread(run_once, False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 循环本身绝不能退出
            log.warning("论文追踪循环异常，下一轮继续", exc_info=True)


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("追踪任务退出时异常", exc_info=True)
    _task = None
