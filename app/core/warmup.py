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

import time

from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger

log = get_logger(__name__)


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
    return out
