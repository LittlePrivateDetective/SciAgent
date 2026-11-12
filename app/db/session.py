"""数据库会话管理：SQLite + WAL，单文件可交付。"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.core.config import settings
from app.core.logger import get_logger
from app.db.models import Base

log = get_logger(__name__)

engine: Engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
    """WAL 提升并发读性能；外键约束必须手动打开。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def _quick_check(path: Path) -> tuple[bool, str]:
    """SQLite 自带的完整性快检。返回 (是否 ok, 说明)。

    用 quick_check 而不是 integrity_check：前者跳过昂贵的索引比对，秒级返回，
    对"能不能继续用"这个判断足够。
    """
    import sqlite3

    if not path.exists():
        return False, "文件不存在"
    try:
        con = sqlite3.connect(str(path), timeout=10)
        try:
            rows = [r[0] for r in con.execute("PRAGMA quick_check").fetchall()]
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    bad = [r for r in rows if str(r).lower() != "ok"]
    return (not bad), ("; ".join(bad[:3]) if bad else "ok")


def ensure_db_healthy() -> dict:
    """启动自检：库坏了就用随包附带的 seed 顶上。

    云端实测踩到过 `database disk image is malformed`：进程重启/文件被覆盖的
    瞬间有写入没落盘，主库就坏了，而它的表现是"列表能打开、点进某篇就 500"，
    从外部完全看不出是数据库的问题。
    这里在第一次连接之前把库检查一遍，坏了就换 seed —— 宁可用打包时那份确定
    可用的快照（库里是 38 篇预置语料），也不要带着一个坏库启动。
    """
    import shutil
    import time as _t

    db_path = settings.db_path
    ok, why = _quick_check(db_path)
    if ok:
        return {"ok": True, "action": "none", "detail": "quick_check ok"}

    log.error("数据库自检失败（%s），尝试恢复：%s", why, db_path)
    stamp = _t.strftime("%Y%m%d_%H%M%S")

    # 先把坏库连同 WAL 侧文件挪走：留着它们，新库会被一起带坏
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            try:
                src.replace(Path(f"{db_path}.corrupt-{stamp}{suffix}"))
            except OSError:
                try:
                    src.unlink()
                except OSError:
                    pass

    # 1) 先试抢救：损坏不严重时 VACUUM INTO 能榨出一份干净库，
    #    这样云端用户后来上传的论文还在（seed 里只有打包那一刻的 38 篇）
    for src in (Path(f"{db_path}.corrupt-{stamp}"),):
        if not src.exists():
            continue
        tmp = Path(str(db_path) + ".recover")
        try:
            import sqlite3

            if tmp.exists():
                tmp.unlink()
            con = sqlite3.connect(str(src), timeout=10)
            try:
                con.execute(f"VACUUM INTO '{tmp.as_posix()}'")
            finally:
                con.close()
            ok2, why2 = _quick_check(tmp)
            if ok2:
                shutil.move(str(tmp), str(db_path))
                log.warning("已用 VACUUM 抢救出可用库（%s）", db_path)
                return {"ok": True, "action": "vacuum", "detail": why}
            tmp.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("VACUUM 抢救失败：%s", exc)
            Path(str(db_path) + ".recover").unlink(missing_ok=True)

    # 2) 兜底：用随包附带的 seed
    seed = settings.data_path / "seed" / "sciagent.db"
    if seed.exists():
        shutil.copy2(seed, db_path)
        ok3, why3 = _quick_check(db_path)
        if ok3:
            log.warning("已用 seed 恢复数据库：%s", db_path)
            return {"ok": True, "action": "seed", "detail": why}
        log.error("seed 也通不过自检（%s）", why3)
        return {"ok": False, "action": "seed-failed", "detail": why3}

    log.error("没有可用 seed，将按空库启动（原有数据需重新入库）")
    return {"ok": False, "action": "none", "detail": why}


def init_db() -> None:
    ensure_db_healthy()
    Base.metadata.create_all(engine)
    log.info("数据库就绪：%s", settings.db_path)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_scope() -> Session:
    """脚本/后台任务用：调用方负责 commit/close。"""
    return SessionLocal()
