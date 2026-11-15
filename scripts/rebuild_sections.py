"""只重建章节树（sections 表），不重切分块、不重算向量。

真正的逻辑在 `app.pipeline.reparse`（启动自愈和后台接口共用同一份），
这里只是命令行入口。

用法：
    ./.venv/Scripts/python.exe scripts/rebuild_sections.py             # 只修可疑的
    ./.venv/Scripts/python.exe scripts/rebuild_sections.py --all       # 整库重建
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal  # noqa: E402
from app.parse.structure import PARSE_VERSION  # noqa: E402
from app.pipeline.reparse import heal_structures  # noqa: E402


def main() -> None:
    force = "--all" in sys.argv
    with SessionLocal() as db:
        result = heal_structures(db, force=force)
    print(f"解析器版本 {PARSE_VERSION}：{result}")


if __name__ == "__main__":
    main()
