"""把已译页面用新提示词 + 加强模型重译一遍。

背景：本轮改了 `_SYS` 第 3 条（普通名词不再加英文括号注释），并新增图注格式兜底。
库里已缓存的 193 段仍带旧风格，两种风格混在同一篇里会显得不一致，
所以把"已经译过的页"整页重来一遍——只重译这些页，不动其他 33 篇（它们本来就没有译文）。

用法：.venv/Scripts/python.exe scripts/retranslate_cached.py [--dry]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402

from app.db.models import Translation  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.rag.bilingual import translate_pages  # noqa: E402


def main() -> None:
    dry = "--dry" in sys.argv
    db = session_scope()
    try:
        rows = db.execute(
            select(Translation.doc_id, Translation.page)
            .where(Translation.model != "verbatim")
            .distinct()
        ).all()
        from app.rag.bilingual import detect_lang
        from app.db.models import Document, Chunk

        # 中文文献跳过：这些页是"中译英"测试留下的副产品，重译只会把中文原样吐回来
        zh_docs = {
            d for (d,) in db.execute(select(Document.id)).all()
            if detect_lang((db.scalars(select(Chunk.text).where(Chunk.doc_id == d).limit(1)).first() or "")) == "zh"
        }
        pages = sorted({(d, p) for d, p in rows if d not in zh_docs})
        print(f"待重译 {len(pages)} 页：")
        for d, p in pages:
            print(f"  {d} p{p}")
        if dry:
            return
        by_doc: dict[str, list[int]] = {}
        for d, p in pages:
            by_doc.setdefault(d, []).append(p)
        t0 = time.time()
        total = 0
        for doc_id, ps in by_doc.items():
            db.execute(
                delete(Translation).where(
                    Translation.doc_id == doc_id,
                    Translation.page.in_(ps),
                    Translation.model != "verbatim",
                )
            )
            db.commit()
            for p in ps:
                n = translate_pages(db, doc_id, p, p, hq=True)
                total += n
                print(f"  {doc_id} p{p} → {n} 段（{time.time() - t0:.0f}s）", flush=True)
        print(f"完成：{total} 段，用时 {time.time() - t0:.0f}s")
    finally:
        db.close()


if __name__ == "__main__":
    main()
