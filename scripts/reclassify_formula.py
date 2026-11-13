"""把"公式碎片"从正文块里挑出来，重标成 formula。

为什么需要一次性迁移：判据改在 `app/parse/structure.py`，但它只在**重新解析**
时生效；库里已有的 38 篇文献不会自动变。而这两千多个块正是翻译、检索、引用
溯源的实际数据源——不迁移，改动等于没发生。

判据与解析层共用 `looks_formula_text`（纯文本函数），不做二次实现。

安全边界：
- 只改 `kind='text'` 的块，不动 table/figure/reference/heading；
- 只改长度 ≤200 的块（长段落里即使夹着公式，整段仍应作为正文参与检索）；
- 默认 dry-run，`--apply` 才写库；写库前先备份 db 文件。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.parse.structure import looks_formula_text  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只看）")
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    db = ROOT / "data" / "sciagent.db"
    con = sqlite3.connect(db)
    rows = con.execute(
        "select c.id, c.doc_id, c.page, c.kind, c.text, d.title "
        "from chunks c join documents d on d.id = c.doc_id "
        "where c.kind = 'text' and length(c.text) between 20 and 200"
    ).fetchall()

    cand = [(cid, did, page, text, title) for cid, did, page, kind, text, title in rows
            if looks_formula_text(text or "")]
    print(f"正文块 {len(rows)} 条，命中公式判据 {len(cand)} 条")
    for cid, did, page, text, title in cand[: args.show]:
        print(f"  p{page:<3} [{(title or '')[:26]:26s}] {text[:96]!r}")

    if not args.apply:
        print("\n（dry-run，未写库。加 --apply 执行）")
        return

    backup = db.with_name(f"sciagent.before-formula-{time.strftime('%Y%m%d-%H%M%S')}.db")
    shutil.copy2(db, backup)
    print(f"已备份：{backup.name}")

    con.executemany("update chunks set kind='formula' where id=?",
                    [(c[0],) for c in cand])
    con.commit()
    left = con.execute("select kind, count(*) from chunks group by kind").fetchall()
    print("改后分布：", left)


if __name__ == "__main__":
    main()
