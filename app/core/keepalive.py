"""五分钟心跳保活（cron 式后台循环）。

保的不是"进程别睡"——服务进程在 Windows/Linux 上不会因为空闲被杀。真正会冷掉的是两样：

1. **本地模型会被卸载**。Ollama 默认 5 分钟没有请求就把模型从显存里卸掉，下一次
   请求要重新加载权重：qwen2.5:3b 实测冷启动 1.5–4 秒，7B 更久。演示时第一句话
   突然卡 4 秒，看起来就像"系统很慢"。心跳带 ``keep_alive`` 让模型常驻。
2. **云端连接池与 DNS 会凉**。空闲久了 TLS 会话被回收，首个请求要重新握手，
   多花 100–300ms；这在"首字延迟"这个指标上很显眼。

云端默认**不**预热：每 5 分钟一次 1-token 调用，一天 288 次，会持续计费。云端模型
是共享的，本来就没有"卸载"这回事，收益只有连接池那一层。需要时在后台把
``keepalive_warm_cloud`` 打开。

循环用"每 5 秒看一次表"而不是 ``sleep(interval)``：后台把间隔从 300 改成 60，
5 秒内就按新节奏走，不用重启进程。
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

TICK = 5.0                      # 看表间隔：改参数后 5 秒内生效
MANUAL_GAP = 30.0               # 手动触发的最小间隔，防止被刷着烧 token
KEEP_ALIVE = "30m"              # 给 Ollama 的驻留时长：留 6 倍余量，心跳漏几次也不掉

_state: dict = {
    "started_at": None,
    "last_beat_at": None,
    "last_ms": None,
    "beats": 0,
    "fails": 0,
    "last_warm": "none",        # ollama / cloud / skip / fail
    "last_error": "",
    "last_log_at": 0.0,
}
_lock = threading.Lock()
_task: asyncio.Task | None = None


# ---------------------------------------------------------------- 预热
def _warm_ollama(timeout: float) -> str:
    """让本地模型留在显存里。num_predict=1：只要它被加载，不要它真的说话。"""
    resp = httpx.post(
        f"{settings.ollama_base_url.rstrip('/')}/api/generate",
        json={
            "model": settings.ollama_model,
            "prompt": "ping",
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": 1, "temperature": 0},
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:160]}")
    return "ollama"


def _warm_cloud(timeout: float) -> str:
    """只建连 + 1 token。不经过 gateway：它每次都会写 INFO 日志，心跳会刷屏。"""
    key = settings.llm_api_key or settings.emb_api_key
    if not key:
        return "skip"
    resp = httpx.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"云端 HTTP {resp.status_code}: {resp.text[:160]}")
    return "cloud"


# ---------------------------------------------------------------- 一次心跳
def _db_ping() -> None:
    """一条 SELECT 1：顺带把连接池里闲置的 SQLite 连接重新握上。"""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        db.execute(text("SELECT 1"))


def beat(manual: bool = False) -> dict:
    """跑一次心跳。同步函数——异步循环和 HTTP 接口都直接调它。"""
    now = time.time()
    with _lock:
        last = _state["last_beat_at"]
        if manual and last and now - last < MANUAL_GAP:
            return {**_snapshot_locked(), "throttled": True}

    t0 = time.perf_counter()
    warm = "skip"
    err = ""
    try:
        _db_ping()
    except Exception as exc:  # noqa: BLE001 心跳不能把服务带崩
        err = f"db: {exc}"

    route = settings.llm_route.lower()
    # auto 的实际走向与 gateway 保持一致：有 key 用云端，否则本地
    if route == "auto":
        route = "cloud" if settings.llm_api_key else ("local" if settings.ollama_enabled else "mock")

    if not err:
        try:
            if route == "local" and settings.ollama_enabled and settings.keepalive_warm_model:
                warm = _warm_ollama(timeout=25.0)
            elif route == "cloud" and settings.keepalive_warm_cloud:
                warm = _warm_cloud(timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            warm = "fail"
            err = str(exc)[:300]

    elapsed = int((time.perf_counter() - t0) * 1000)
    with _lock:
        _state["last_beat_at"] = time.time()
        _state["last_ms"] = elapsed
        _state["beats"] += 1
        _state["last_warm"] = warm
        _state["last_error"] = err
        if err:
            _state["fails"] += 1
        snap = _snapshot_locked()
        # 成功时只在 DEBUG 落一条；失败要看得见，但同一条错别每 5 分钟刷一次
        noisy = err and (time.time() - _state["last_log_at"] > 600)
        if noisy:
            _state["last_log_at"] = time.time()

    if err:
        if noisy:
            log.warning("保活失败（累计 %d 次）：%s", snap["fails"], err)
    else:
        log.debug("保活 OK：%dms warm=%s", elapsed, warm)
    return snap


def _snapshot_locked() -> dict:
    s = _state
    nxt = None
    if s["last_beat_at"]:
        nxt = datetime.fromtimestamp(s["last_beat_at"] + max(30, settings.keepalive_interval))
    return {
        "enabled": bool(settings.keepalive_enabled),
        "interval_s": int(settings.keepalive_interval),
        "warm_model": bool(settings.keepalive_warm_model),
        "warm_cloud": bool(settings.keepalive_warm_cloud),
        "started_at": s["started_at"],
        "last_beat_at": (datetime.fromtimestamp(s["last_beat_at"]).isoformat(timespec="seconds")
                         if s["last_beat_at"] else None),
        "next_beat_at": nxt.isoformat(timespec="seconds") if nxt else None,
        "last_ms": s["last_ms"],
        "beats": s["beats"],
        "fails": s["fails"],
        "last_warm": s["last_warm"],
        "last_error": s["last_error"],
    }


def status() -> dict:
    with _lock:
        return _snapshot_locked()


# ---------------------------------------------------------------- 循环
async def _loop() -> None:
    with _lock:
        _state["started_at"] = datetime.now().isoformat(timespec="seconds")
    log.info("保活循环已启动：间隔 %ss", settings.keepalive_interval)

    last = 0.0
    while True:
        await asyncio.sleep(TICK)
        if not settings.keepalive_enabled:
            last = 0.0            # 关掉再打开时立刻补一次，不等满一个周期
            continue
        interval = max(30, int(settings.keepalive_interval))
        if time.time() - last < interval:
            continue
        last = time.time()
        try:
            # 心跳里可能有 25 秒的模型加载，别把事件循环卡住——丢到线程里跑
            await asyncio.to_thread(beat)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 循环本身绝不能退出
            log.warning("保活循环异常，下一轮继续", exc_info=True)


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
        except Exception:  # noqa: BLE001 停机时任何异常都不该挡住退出
            log.debug("保活任务退出时异常", exc_info=True)
    _task = None
