"""构建云端发布目录 `deploy/`（只放运行期需要的东西）。

为什么单独建目录，而不是直接把项目根丢上去发布：
- 项目根有 `.venv`（704 MB）、`data/_qa_*`（830 MB）、`docs/qa/evidence`（564 MB），
  这些一上传就是几十分钟，而且平台只需要"能跑起来的最小集"；
- `.env` 里有真实 API Key，它必须跟着走，所以整个 `deploy/` 进 .gitignore，绝不进仓库。

用法：
    ./.venv/Scripts/python.exe scripts/build_deploy.py
产物：
    deploy/  约 230 MB（app + web + SQLite + 原始 PDF）

为什么带 PDF 不带 Chroma：
- 阅读器的高亮框要靠 PyMuPDF 从原始 PDF 现渲页图，PDF 不能省；
- Chroma 索引 394 MB，而 `chunks.embedding` 在 SQLite 里本来就有一份，
  `VECTOR_BACKEND=sqlite` 走内存暴力内积，2 万块约 80 MB 内存、首次检索 1–2 秒，
  之后走缓存 —— 用 80 MB 内存换 394 MB 上传体积，这笔换得过。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPT = ROOT / "deploy"

RUN_PY = '''"""云端启动入口：平台注入 PORT，必须绑 0.0.0.0。

本地开发不用它（直接 uvicorn app.main:app --port 8013）。
"""
import os
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parent
os.chdir(BASE)                      # data/ 全是相对路径，必须先切到 deploy 根
sys.path.insert(0, str(BASE))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
'''

# 云端不装 chromadb：VECTOR_BACKEND=sqlite 时不 import 它，
# 省下约 50 MB 安装体积和一两分钟安装时间。装不上也不影响启动。
DROP_DEPS = ("chromadb", "pytesseract", "Pillow")

# .env 里必须改掉的项（其余原样带过去，包括 Key）
OVERRIDES = {
    "VECTOR_BACKEND": "sqlite",
    "OLLAMA_ENABLED": "0",
    "LLM_ROUTE": "cloud",
    "DATA_DIR": "./data",
    "LOG_LEVEL": "INFO",
    "PORT": "8000",
}


def _checkpoint_db(path: Path) -> None:
    """把 WAL 合并回主库再拷贝。

    SQLite 默认 WAL 模式：新写入先落 .db-wal，主 .db 可能落后几个事务。
    直接 copy2 主库 = 拷到一个"少了最近改动"的旧快照，而且完全静默。
    """
    import sqlite3

    if not path.exists():
        return
    try:
        con = sqlite3.connect(str(path), timeout=10)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
        con.close()
    except Exception as exc:  # noqa: BLE001 数据库正被写也不能挡住打包
        print(f"  [warn] WAL checkpoint 失败（{exc}），将连 -wal 一起拷")


def _sync_sources(db_path: Path) -> None:
    """按库里的引用，把源文件补齐到包内，并把 file_path 改写成包内相对路径。

    这件事以前没人做，于是云端阅读器整页图打不开（前端只显示破图标），两个原因：
    1. 入库时写的是"那台机器"的绝对路径，实测 38 条里 10 条是
       D:\\projects\\sciagent\\data\\pdf\\...，换台机器就等于指到空气；
    2. 上传的 PDF 存在 data/uploads（文件名带 8 位哈希前缀），打包只拷了
       data/pdf —— cn_paper.pdf 这种就根本没进包，云端自然渲染不出来。

    运行时虽然还有按文件名兜底的 resolve_doc_file()，但那只是补丁；
    包里本来就该是能直接用的路径和齐全的文件。
    """
    import sqlite3

    if not db_path.exists():
        return
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = con.execute("SELECT id, filename, file_path FROM documents").fetchall()
        fixed = copied = missing = 0
        for doc_id, filename, file_path in rows:
            # file_path 的 basename 比 filename 更准（上传件带哈希前缀）
            base = Path((file_path or "").replace("\\", "/")).name or (filename or "")
            src = rel = None
            for sub, cand in (("pdf", ROOT / "data" / "pdf" / base),
                              ("uploads", ROOT / "data" / "uploads" / base)):
                if cand.is_file():
                    src, rel = cand, f"data/{sub}/{base}"
                    break
            if src is None and filename:
                cand = ROOT / "data" / "pdf" / filename
                if cand.is_file():
                    src, rel = cand, f"data/pdf/{filename}"
            if src is None:
                missing += 1
                print(f"  [warn] 源文件缺失，云端打不开：{filename}")
                continue
            dst = DEPT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
                copied += 1
            con.execute("UPDATE documents SET file_path = ? WHERE id = ?", (rel, doc_id))
            fixed += 1
        con.commit()
        print(f"  file_path 规范化：{fixed}/{len(rows)} 条 -> data/<子目录>/<文件名>"
              f"（另补拷 {copied} 个上传件，缺失 {missing}）")
    finally:
        con.close()


def copy_tree(src: Path, dst: Path, ignore=()) -> None:
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(*ignore, "__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )


def build_env(new_password: str) -> str:
    raw = (ROOT / ".env").read_text(encoding="utf-8")
    out, seen = [], set()
    for line in raw.splitlines():
        key = line.split("=", 1)[0].strip()
        if key in OVERRIDES and "=" in line:
            out.append(f"{key}={OVERRIDES[key]}")
            seen.add(key)
        elif key == "ADMIN_PASSWORD":
            out.append(f"ADMIN_PASSWORD={new_password}")
            seen.add(key)
        elif key == "DEMO_ACCESS_TOKEN":
            out.append("DEMO_ACCESS_TOKEN=")
            seen.add(key)
        else:
            out.append(line)
    for key, val in OVERRIDES.items():
        if key not in seen:
            out.append(f"{key}={val}")
    return "\n".join(out) + "\n"


def main() -> int:
    new_password = sys.argv[1] if len(sys.argv) > 1 else "".join(
        __import__("secrets").choice("abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789")
        for _ in range(16)
    )

    if DEPT.exists():
        shutil.rmtree(DEPT)
    DEPT.mkdir()

    copy_tree(ROOT / "app", DEPT / "app")
    copy_tree(ROOT / "web", DEPT / "web")
    (DEPT / "run.py").write_text(RUN_PY, encoding="utf-8")

    # 依赖：去掉云端用不到的三项
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in req if not any(ln.strip().lower().startswith(d.lower()) for d in DROP_DEPS)]
    (DEPT / "requirements.txt").write_text("\n".join(kept) + "\n", encoding="utf-8")

    # 数据：SQLite（含向量）+ 原始 PDF（阅读器要现渲页图）
    (DEPT / "data").mkdir()
    _checkpoint_db(ROOT / "data" / "sciagent.db")
    shutil.copy2(ROOT / "data" / "sciagent.db", DEPT / "data" / "sciagent.db")
    # 只拷主库，不带 -wal/-shm。
    # 上一版是连 WAL 一起拷的（怕丢最近的入库），但那是在 checkpoint 之后——
    # 主库已经是最新，再带一个半截 WAL 过去，云端打开就是
    # `database disk image is malformed`（实测踩到，表现是"列表能打开、点某篇 500"）。
    # checkpoint 失败时宁可报错，也不要把不一致的一对文件发出去。
    for suffix in ("-wal", "-shm"):
        side = DEPT / "data" / f"sciagent.db{suffix}"
        if side.exists():
            side.unlink()
    copy_tree(ROOT / "data" / "pdf", DEPT / "data" / "pdf")
    _sync_sources(DEPT / "data" / "sciagent.db")

    # seed：打包这一刻的干净快照，运行时若发现主库损坏会自动拿它顶上
    # （云端 data 目录是持久化的，部署不会覆盖主库，坏一次就一直是坏的）。
    seed_dir = DEPT / "data" / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEPT / "data" / "sciagent.db", seed_dir / "sciagent.db")
    print(f"  seed 快照：{(seed_dir / 'sciagent.db').stat().st_size / 1048576:.0f} MB")

    # 分词缓存：云端不重跑 jieba 分词（2 万块实测 12 秒 / 云沙箱 26 秒）。
    # 指纹按块 id 序列算，与 DB 一致；拷过去就能直接命中。
    tokens = ROOT / "data" / "bm25_tokens.txt"
    if tokens.exists():
        shutil.copy2(tokens, DEPT / "data" / "bm25_tokens.txt")
    for extra in ("samples",):
        src = ROOT / "data" / extra
        if src.exists():
            copy_tree(src, DEPT / "data" / extra)
    (DEPT / "data" / "logs").mkdir(exist_ok=True)

    (DEPT / ".env").write_text(build_env(new_password), encoding="utf-8")

    size = subprocess.run(["du", "-sh", str(DEPT)], capture_output=True, text=True).stdout.split()[0]
    parts = {
        p.name: subprocess.run(["du", "-sh", str(p)], capture_output=True, text=True).stdout.split()[0]
        for p in sorted(DEPT.iterdir())
    }
    print(f"deploy/ 已生成：{size}")
    for k, v in parts.items():
        print(f"  {k:18s} {v}")
    print(f"\n云端后台口令（只写进 deploy/.env，不进仓库）：{new_password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
