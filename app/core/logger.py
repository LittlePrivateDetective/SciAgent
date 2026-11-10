"""统一日志：控制台 + 文件双输出，UTF-8，带轮转。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import settings

_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s"


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    try:
        fh = RotatingFileHandler(
            settings.log_dir / "sciagent.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)
    except OSError:
        # 日志文件写不了不能拖垮服务
        pass

    # 压掉第三方噪音
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
