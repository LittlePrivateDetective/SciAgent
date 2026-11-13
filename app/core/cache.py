"""进程内的短时缓存：问答结果复用。

为什么需要：演示与评审的场景里，同一个问题经常被连着问两三遍（换个人点一次、
刷新一次页面）。每次重跑一遍完整链路要 3–16 秒，还要付一次模型调用费；
而同一个问题在同一份语料上的答案在几分钟内不会变。

为什么不用 Redis：这是个单进程部署的作品，多一个外部依赖就多一个"评审机器上
起不来"的理由。进程内 LRU 够用，进程重启自然失效，也不会留下"改了参数但缓存
还是旧的"那种幽灵问题。

三条硬约束（不遵守就是 bug 制造机）：
1. **key 里必须带模型与检索参数**——否则在后台把 trans_model 从 qwen-flash 换成
   qwen3.8-max，提问仍返回旧模型的答案，"改参数不生效"这种问题最难查。
2. **key 里必须带库规模**——上传新文献后，同一个问题本该命中新证据，
   只按问题做 key 会让新文献"怎么都搜不到"。
3. **mock 模式不缓存**——测试用例（api_params_effect）靠"改参数→行为变"来验证，
   mock 下缓存会把断言全搞坏。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from app.core.logger import get_logger

log = get_logger(__name__)

MAX_ITEMS = 200


class TTLCache:
    """带容量上限的 TTL 字典。get 命中会刷新 LRU 次序（但不续期）。"""

    def __init__(self, ttl: int = 600, max_items: int = MAX_ITEMS) -> None:
        self.ttl = max(0, int(ttl))
        self.max_items = max(1, int(max_items))
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if not item:
                self.misses += 1
                return None
            ts, val = item
            if time.time() - ts > self.ttl:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return val

    def put(self, key: str, val: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.time(), val)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data.clear()
            self.hits = self.misses = 0
        return n

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "items": len(self._data),
                "ttl_s": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }


def make_key(**parts: Any) -> str:
    """稳定 key：字典按键排序，避免同一组参数因顺序不同算出两个 key。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# 问答缓存。构造时 ttl=0（等价关闭），真正生效的 TTL 每次调用前从后台参数同步——
# 后台把 ANSWER_CACHE_TTL 从 600 改成 0，下一问就必须真的不用缓存，
# 而不是等进程重启（否则"改参数不生效"又会变成一次排查）。
answer_cache = TTLCache(ttl=0)


def answer_ttl() -> int:
    """把后台参数同步到缓存实例上；归零时顺手清空，避免关闭后还留着旧数据。"""
    from app.core.config import settings

    ttl = int(getattr(settings, "answer_cache_ttl", 0) or 0)
    if answer_cache.ttl != ttl:
        answer_cache.ttl = ttl
        if ttl <= 0:
            answer_cache.clear()
    return ttl
