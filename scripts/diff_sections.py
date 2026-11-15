"""章节树回归：把"库里的旧章节"和"用当前解析器重算的章节"逐篇对比。

用途：改了 structure.py 之后，确认修正只落在目标文档上，没有把别的文档改坏。
输出：每篇的条数变化 + 被删/被增的标题。
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.parse.structure import parse_pdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sciagent.db"
PDF_DIR = ROOT / "data" / "pdf"


def norm(t: str) -> str:
    return " ".join((t or "").split()).strip().lower()


def main() -> None:
    logging.disable(logging.INFO)
    con = sqlite3.connect(DB)
    docs = con.execute("select id, filename from documents order by filename").fetchall()
    verbose = "-v" in sys.argv
    for doc_id, fname in docs:
        path = PDF_DIR / fname
        if not path.exists():
            print(f"!! 缺文件 {fname}")
            continue
        old = con.execute(
            "select level, title from sections where doc_id=? order by order_idx", (doc_id,)
        ).fetchall()
        try:
            parsed = parse_pdf(str(path))
        except Exception as exc:  # noqa: BLE001
            print(f"!! 解析失败 {fname}: {exc}")
            continue
        new = [(s["level"], s["title"]) for s in parsed.sections]
        old_map = {}
        for lv, t in old:
            old_map.setdefault(norm(t), []).append(lv)
        new_map = {}
        for lv, t in new:
            new_map.setdefault(norm(t), []).append(lv)
        removed = [t for t in old_map if t not in new_map]
        added = [t for t in new_map if t not in old_map]
        level_chg = []
        for t in old_map:
            if t in new_map and old_map[t][0] != new_map[t][0]:
                level_chg.append((t, old_map[t][0], new_map[t][0]))
        if not (removed or added or level_chg):
            print(f"== 无变化 {fname}  ({len(old)})")
            continue
        print(f"== {fname}  旧 {len(old)} → 新 {len(new)}")
        for t in removed[:12]:
            print(f"   - 删 L{old_map[t][0]}: {t[:70]}")
        if len(removed) > 12:
            print(f"   - 删 … 共 {len(removed)} 条")
        for t in added[:12]:
            print(f"   + 增 L{new_map[t][0]}: {t[:70]}")
        if len(added) > 12:
            print(f"   + 增 … 共 {len(added)} 条")
        for t, o, n in level_chg[:10]:
            print(f"   ~ 层级 L{o}→L{n}: {t[:70]}")
        if verbose:
            for i, (lv, t) in enumerate(new):
                print(f"      {i:3d} L{lv} {t[:76]}")


if __name__ == "__main__":
    main()
