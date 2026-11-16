"""运行模式：cloud（默认）/ offline（纯本地）。

为什么单独放一个模块而不是塞进 settings：
  settings 是启动时从 .env 读的不可变快照，而"一键切换"要求运行中就能改，
  改完立刻影响下一次检索。这里用模块级状态 + app_settings 持久化，
  不动 pydantic 配置对象，也不会因为热改配置把别的字段带偏。

两种模式的实际差别只有三处：
  1. 查询向量化：cloud 调远程接口 / offline 用本机 bge-m3
  2. 向量检索：cloud 读 chunks.embedding / offline 读 chunks.embedding_local
  3. 重排：cloud 调 cross-encoder 接口 / offline 退回本地规则打分（不发网络请求）

生成那一段由 LLM_ROUTE 控制（走 gateway），与这里独立：离线模式下
请求会带 X-LLM-Route: local，或者把全局路由切成 local。
"""
from __future__ import annotations

import logging
import threading

from app.db.models import AppSetting
from app.db.session import SessionLocal

log = logging.getLogger(__name__)

CLOUD = "cloud"
OFFLINE = "offline"
_SETTING_KEY = "runtime_mode"

_lock = threading.Lock()
_mode: str = CLOUD


def get_mode() -> str:
    return _mode


def is_offline() -> bool:
    return _mode == OFFLINE


def set_mode(mode: str) -> str:
    """切换模式并落盘。非法值一律按 cloud 处理，避免把系统卡在一个不存在的档位。"""
    global _mode
    mode = OFFLINE if mode == OFFLINE else CLOUD
    with _lock:
        if mode == _mode:
            return _mode
        _mode = mode
    _persist(mode)
    log.info("运行模式切换为 %s", mode)
    return _mode


def load_mode() -> str:
    """启动时恢复上次的模式。读不到就保持 cloud。"""
    global _mode
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, _SETTING_KEY)
            if row and row.value in (CLOUD, OFFLINE):
                _mode = row.value
    except Exception:  # noqa: BLE001 首次启动表可能还没建
        log.debug("恢复运行模式失败，按 cloud 处理", exc_info=True)
    return _mode


def _persist(mode: str) -> None:
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, _SETTING_KEY)
            if row:
                row.value = mode
            else:
                db.add(AppSetting(key=_SETTING_KEY, value=mode))
            db.commit()
    except Exception:  # noqa: BLE001 落盘失败不该阻断本次切换
        log.warning("运行模式落盘失败", exc_info=True)
