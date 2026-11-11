"""后台口令鉴权：把 `/admin` 与 `/api/admin/*` 挡住。

为什么不是 HTTP Basic：Basic 会把口令以 base64 往返，且浏览器在每次请求都带上；
这里用一次性会话令牌 + HttpOnly Cookie，令牌只存在服务端内存里，能单独失效。

为什么不做用户体系：本项目是单机单用户定位，一个口令足够；引入用户表反而是
多出来的攻击面和运维负担。需要多人时，口令换成 `demo_access_token` 走网关即可。

三条硬规则：
1. 口令只从环境变量读（`ADMIN_PASSWORD`），不进数据库、不回显、不写日志。
2. 会话令牌随机生成，服务重启即全部失效——重启后重新登录，是预期行为。
3. 登录失败按 IP 限速，避免在线爆破；限速窗口内只返回 429，不提示"口令错了几次"。
"""

from __future__ import annotations

import hmac
import secrets
import time

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

COOKIE = "sciagent_admin"
SESSION_SECONDS = 12 * 3600     # 12 小时：够开一天会，过期要重新输
MAX_FAILS = 8                   # 同一 IP 连续失败上限
FAIL_WINDOW = 600               # 统计窗口（秒）

_sessions: dict[str, float] = {}          # token -> 过期时间戳
_fails: dict[str, tuple[int, float]] = {}  # ip -> (次数, 窗口起点)


def enabled() -> bool:
    return bool((settings.admin_password or "").strip())


def _client_ip(request: Request) -> str:
    # 反代场景取 X-Forwarded-For 第一段；直连时退回 socket 地址
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many(ip: str) -> bool:
    n, start = _fails.get(ip, (0, 0.0))
    if time.time() - start > FAIL_WINDOW:
        _fails.pop(ip, None)
        return False
    return n >= MAX_FAILS


def _mark_fail(ip: str) -> None:
    n, start = _fails.get(ip, (0, time.time()))
    if time.time() - start > FAIL_WINDOW:
        n, start = 0, time.time()
    _fails[ip] = (n + 1, start)


def login(request: Request, password: str) -> tuple[str, int]:
    """校验口令并签发会话，返回 (token, 有效期秒数)。失败抛 HTTPException。"""
    if not enabled():
        raise HTTPException(400, "未设置 ADMIN_PASSWORD，后台鉴权未启用")
    ip = _client_ip(request)
    if _too_many(ip):
        raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
    if not hmac.compare_digest((password or "").strip(), settings.admin_password.strip()):
        _mark_fail(ip)
        log.warning("后台登录失败：ip=%s", ip)
        raise HTTPException(401, "口令不正确")

    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_SECONDS
    _fails.pop(ip, None)
    log.info("后台登录成功：ip=%s", ip)
    return token, SESSION_SECONDS


def logout(token: str) -> None:
    _sessions.pop(token or "", None)


def valid(token: str) -> bool:
    exp = _sessions.get(token or "")
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def token_of(request: Request) -> str:
    return request.cookies.get(COOKIE, "")


def require_admin(request: Request) -> None:
    """FastAPI 依赖：未登录一律 401。

    口令为空时不拦截——本地开发/离线演示默认就是这个状态，
    但界面上会明确显示"未启用口令"，避免误以为已经有人管着。
    """
    if not enabled():
        return
    if valid(token_of(request)):
        return
    raise HTTPException(401, "需要登录后台")
