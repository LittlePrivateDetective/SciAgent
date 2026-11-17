"""冷启动预热：把"页面打开"和"第一次提问"之间的等待提前付掉。

云平台沙箱空闲会休眠，唤醒后第一次请求实测要 37.3 秒（热态 15 秒）。
echoMirror 那边踩过同一个坑，解法是「页面一打开就静默打一次 warm」。

三个动作各自计时、各自降级——预热失败绝不能影响正常问答：
1) 检索：把 SQLite 里的 2 万条向量读进内存（约 80 MB，首次 1–2 秒）；这一步最值钱，
   不预热的话第一个问题要在用户面前加载整张矩阵；
2) 向量化：打一次 embedding 接口，建立连接复用；
3) 生成：发一条 1-token 的请求，把 LLM 链路焐热。
"""
from __future__ import annotations

import threading
import time

from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger

log = get_logger(__name__)

# 图谱预热互斥：保活每 5 分钟来一次，启动还要来一次，不挡并发重复建图
_GRAPH_LOCK = threading.Lock()


def _warm_graph(tag: str) -> None:
    """后台建一次引用图谱。

    为什么必须异步：39 篇论文的 PageRank + 介数中心性 + 社区发现实测 30 秒以上，
    而反向代理的读请求超时只有 20 秒——首屏点开图谱会拿到 504（线上实测）。
    提前在后台建好，用户点的时候就是内存里那份。
    """
    if not _GRAPH_LOCK.acquire(blocking=False):
        return
    try:
        from app.db.session import SessionLocal
        from app.rag.citation_graph import build_graph

        t0 = time.time()
        db = SessionLocal()
        try:
            data = build_graph(db)
        finally:
            db.close()
        log.info("预热[%s]：图谱 %d 节点 / %d 边，%d ms", tag,
                 len(data.get("nodes", [])), len(data.get("edges", [])),
                 int((time.time() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001
        log.warning("预热[%s]：图谱失败 %s", tag, str(exc)[:120])
    finally:
        _GRAPH_LOCK.release()


def warm_graph_async(tag: str = "warm") -> None:
    threading.Thread(target=_warm_graph, args=(tag,), daemon=True,
                     name="warm-graph").start()


# ---------------- 未来工作方向预计算 ----------------
# 这条链路要算 146 条句向量 + 39 篇的块矩阵，线上沙箱实测 **43 秒**（本地 8 秒）。
# 接口是读请求，反代 20 秒就切，等于用户第一次点必然等到超时。和图画谱一样，
# 把这份计算挪到后台线程，保活每 5 分钟续一次缓存，用户点开时就是内存里那份。
_FW_LOCK = threading.Lock()


def _warm_future_work(tag: str) -> None:
    if not _FW_LOCK.acquire(blocking=False):
        return
    try:
        from app.db.session import SessionLocal
        from app.research.future_work import collect_future_work

        t0 = time.time()
        db = SessionLocal()
        try:
            out = collect_future_work(db)
        finally:
            db.close()
        stats = out.get("stats", {})
        log.info("预热[%s]：未来工作 %d 条（命中 %d），%d ms", tag,
                 stats.get("returned", 0), stats.get("possibly_addressed", 0),
                 int((time.time() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001
        log.warning("预热[%s]：未来工作失败 %s", tag, str(exc)[:120])
    finally:
        _FW_LOCK.release()


def warm_future_work_async(tag: str = "warm") -> None:
    threading.Thread(target=_warm_future_work, args=(tag,), daemon=True,
                     name="warm-future-work").start()


def warm_once(tag: str = "manual") -> dict:
    out: dict = {"ok": True, "route": settings.llm_route, "tag": tag}
    t0 = time.time()

    try:
        from app.rag.hybrid import search, token_cache_status

        # 先记录缓存状态：冷启动变慢时，是"缓存没带上去"还是"带了但指纹不符"，
        # 一看就知道，不用去翻沙箱文件系统
        out["tokens"] = token_cache_status()
        hits = search("预热查询", None, top_k=3)
        out["retrieval"] = {"ok": bool(hits), "hits": len(hits), "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        out["retrieval"] = {"ok": False, "error": str(exc)[:120]}

    t1 = time.time()
    try:
        gateway.chat(
            [{"role": "user", "content": "只回复一个字：好"}],
            max_tokens=1,
            retries=0,
            thinking=False,
        )
        out["llm"] = {"ok": True, "ms": int((time.time() - t1) * 1000)}
    except Exception as exc:  # noqa: BLE001
        out["llm"] = {"ok": False, "error": str(exc)[:120]}

    out["total_ms"] = int((time.time() - t0) * 1000)
    log.info("预热[%s]：检索 %s / 生成 %s / 合计 %d ms", tag, out["retrieval"].get("ok"), out["llm"].get("ok"), out["total_ms"])

    # 图谱与未来工作都不参与上面的耗时统计：它们在后台线程里建，命中缓存时几乎不花时间
    warm_graph_async(tag)
    warm_future_work_async(tag)
    return out
