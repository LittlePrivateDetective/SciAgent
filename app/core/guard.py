"""访问防护：限流 + 上传并发闸门 + 演示令牌。

为什么在作品里做这件事：公网上挂着演示地址，任何人都能点。一次评审期间被打爆
的后果不是"慢"，而是**额度被刷光、后面来的人打不开**——这比任何功能缺陷都致命。
而如果防护加得太重，第一个评审就会被 429 挡在门外。所以这里的原则是：

- **默认宽松到"正常使用永远碰不到"**：每人每分钟 120 次、问答类 30 次。
  一个人连续点，一分钟最多点出十几次；只有脚本才会碰到天花板。
- **上传并发压到 1**：解析是 CPU 密集的，两个人同时传两个 500 页 PDF 会把
  整机卡到问答也超时。排队比并发体验更好（至少每个人都有进度条）。
- **令牌三档**：off（不校验）/ warn（缺令牌只在响应头标记）/ enforce（401）。
  默认 off，因为它会挡住"拿到链接就能用"的演示路径；要收紧时在后台一键切。

限流按"IP + 桶"计，桶分三类：ask（问答，额度最小）、write（上传/写操作）、
read（其余 GET）。分开计数而不是一个总数，是因为"翻页 50 次"和"问 50 个问题"
的成本差两个数量级。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

# 不计入限流的路径：健康检查要被探针高频调用，静态资源不算"使用"，
# 后台自己有口令鉴权，文档页不会产生模型费用。
SKIP_PREFIX = ("/health", "/static/", "/admin", "/docs", "/openapi.json", "/redoc", "/favicon")

ASK_PATHS = ("/api/chat", "/api/chat/stream", "/api/tools/agent", "/api/write/", "/api/documents/")

WINDOW = 60.0

# 本机回环地址：开发、回归测试、健康检查都从这里来。
# 不豁免的话，一次 qa_suite（39 项、每项多次请求）就会把自己打成 429 ——
# 实测就是这样：后半程 9 条用例全红，而它们上一次全是 PASS。
# 公网部署时客户端 IP 来自 X-Forwarded-For（真实访客），不受这条影响；
# 若反向代理不透传 XFF，则所有请求都会被认成 127.0.0.1，此时应在后台
# 把「豁免本机」关掉（那时限流本来也无从区分访客）。
LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}


class Bucket:
    """滑动窗口计数。用 deque 而不是"固定窗口计数器"，避免整点前后
    两倍额度（59 秒打满、61 秒再打满）这种经典漏洞。"""

    __slots__ = ("hits",)

    def __init__(self) -> None:
        self.hits: deque[float] = deque()

    def allow(self, limit: int, now: float) -> bool:
        if limit <= 0:
            return True
        while self.hits and now - self.hits[0] > WINDOW:
            self.hits.popleft()
        if len(self.hits) >= limit:
            return False
        self.hits.append(now)
        return True


class Guard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], Bucket] = {}
        self._uploads = threading.BoundedSemaphore(max(1, int(settings.upload_concurrency)))
        self._last_gc = 0.0
        self._blocked = 0

    # ---------- 限流 ----------
    def check(self, ip: str, path: str) -> tuple[bool, str]:
        now = time.time()
        if settings.rate_limit_local and ip in LOCAL_IPS:
            return True, "local"
        kind = "ask" if path.startswith(ASK_PATHS) else "read"
        limit = (settings.rate_limit_ask_per_min if kind == "ask"
                 else settings.rate_limit_per_min)
        key = (ip, kind)
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = Bucket()
        ok = b.allow(int(limit), now)
        if not ok:
            with self._lock:
                self._blocked += 1
        self._gc(now)
        return ok, kind

    def _gc(self, now: float) -> None:
        """每 5 分钟清掉空桶。不清的话，公网扫描器换 IP 刷一遍，
        字典会留下几万个空 deque，内存缓慢上涨。"""
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        with self._lock:
            dead = [k for k, b in self._buckets.items()
                    if not b.hits or now - b.hits[-1] > WINDOW * 3]
            for k in dead:
                self._buckets.pop(k, None)

    # ---------- 上传闸门 ----------
    def acquire_upload(self, timeout: float = 30.0) -> bool:
        return self._uploads.acquire(timeout=timeout)

    def release_upload(self) -> None:
        try:
            self._uploads.release()
        except ValueError:
            pass

    def stats(self) -> dict:
        with self._lock:
            return {
                "buckets": len(self._buckets),
                "blocked": self._blocked,
                "rate_limit_per_min": int(settings.rate_limit_per_min),
                "rate_limit_local": bool(settings.rate_limit_local),
                "rate_limit_ask_per_min": int(settings.rate_limit_ask_per_min),
                "upload_concurrency": int(settings.upload_concurrency),
                "token_mode": (settings.guard_mode or "off").lower(),
                "token_set": bool(settings.demo_access_token),
            }


guard = Guard()


def client_ip(request: Request) -> str:
    """反向代理后面必须看 X-Forwarded-For，否则所有请求都来自同一个 IP，
    限流会把"张三打满额度"变成"全站被限"。取第一个（最靠近客户端的那个）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "?")[:64]


def _token_ok(request: Request) -> bool:
    want = (settings.demo_access_token or "").strip()
    if not want:
        return True
    got = (request.headers.get("x-demo-token")
           or request.query_params.get("token") or "")
    if not got:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    return got == want


class GuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CORS 预检不带业务意图，算进限流会让"前端先 OPTIONS 再 POST"
        # 的每一次调用吃掉两个名额。
        if request.method == "OPTIONS" or path.startswith(SKIP_PREFIX):
            return await call_next(request)

        ok, kind = guard.check(client_ip(request), path)
        if not ok:
            log.warning("限流命中：%s %s kind=%s", client_ip(request), path, kind)
            return JSONResponse(
                {"detail": f"请求太频繁（{kind} 类每分钟有上限）。请稍等十几秒再试。"},
                status_code=429,
                headers={"Retry-After": "20"},
            )

        mode = (settings.guard_mode or "off").lower()
        if mode in ("enforce", "warn") and not _token_ok(request):
            if mode == "enforce":
                return JSONResponse(
                    {"detail": "演示地址需要访问令牌。请使用带 ?token=… 的完整链接。"},
                    status_code=401,
                )
            resp = await call_next(request)
            resp.headers["X-Demo-Token-Missing"] = "1"
            return resp

        return await call_next(request)
