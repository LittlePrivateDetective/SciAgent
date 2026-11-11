"""重建引用表：解析器修好后，已有文献的 Citation 行是旧结果，需要刷新。

不重新切块、不重新向量化 —— 只重跑解析、覆盖 Citation 行，几秒钟完成。
用法：python scripts/rebuild_citations.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select

from app.db.models import Citation, Document
from app.db.session import SessionLocal
from app.parse.structure import parse_pdf
from app.pipeline.ingest import _year_from_arxiv, _year_from_text
from app.rag.citation_graph import invalidate as invalidate_graph

ok = fail = 0
total_refs = 0

with SessionLocal() as db:
    docs = list(db.scalars(select(Document)).all())
    print(f"待刷新 {len(docs)} 篇", flush=True)

    for i, d in enumerate(docs, 1):
        path = Path(d.file_path or "")
        if not path.exists():
            print(f"  [{i}/{len(docs)}] 跳过（文件不存在）{d.filename}", flush=True)
            fail += 1
            continue
        try:
            parsed = parse_pdf(str(path))
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(docs)}] 解析失败 {d.filename}: {exc}", flush=True)
            fail += 1
            continue

        # 扫描件没有文本层，解析出来是空的；这类文档跳过，避免把已有引用抹掉
        if parsed.is_scanned and not parsed.references:
            print(f"  [{i}/{len(docs)}] 跳过（扫描件）{d.filename}", flush=True)
            continue

        db.execute(delete(Citation).where(Citation.src_doc_id == d.id))
        for r in parsed.references:
            db.add(
                Citation(
                    src_doc_id=d.id,
                    dst_doc_id=None,
                    raw_text=r["text"][:2000],
                    ref_index=r["index"],
                    matched_by="unmatched",
                )
            )
        if not d.year:
            d.year = _year_from_arxiv(d.arxiv_id or "") or _year_from_text((parsed.abstract or "")[:2000])
        db.flush()
        total_refs += len(parsed.references)
        ok += 1
        print(f"  [{i}/{len(docs)}] {d.filename[:44]:44s} refs={len(parsed.references)}", flush=True)

    db.commit()

invalidate_graph()
print(f"\n完成：{ok} 篇刷新成功 / {fail} 篇失败 / 共 {total_refs} 条参考文献", flush=True)
