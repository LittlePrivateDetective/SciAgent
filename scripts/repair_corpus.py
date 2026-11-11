"""QA 第三轮数据修补（一次性、幂等）：

1. 重算所有 Document.simhash —— simhash64 分词 bug 修复后旧指纹不兼容；
2. 给扫描件补建 Citation —— OCR 时参考区标题与条目粘在同一块，旧正则整段漏抽。

不重新解析 PDF（扫描件 OCR 要 2 分钟），扫描件的文本用已落库的 chunks 近似重建。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 直接 `python scripts/repair_corpus.py` 时，sys.path[0] 是 scripts/ 而不是项目根
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select

from app.core.logger import get_logger
from app.db.models import Chunk, Citation, Document
from app.db.session import session_scope
from app.parse.structure import Block, _fallback_ocr_references, parse_pdf
from app.rag.chunker import strip_carry
from app.rag.dedup import simhash64

log = get_logger(__name__)


def _chunks_text(db, doc_id: str) -> str:
    rows = list(db.scalars(select(Chunk).where(Chunk.doc_id == doc_id)
                           .order_by(Chunk.page, Chunk.idx)).all())
    prev, out = "", []
    for c in rows:
        clean = strip_carry(prev, c.text)
        out.append(clean)
        prev = c.text
    return "\n".join(out)


def run() -> None:
    db = session_scope()
    docs = list(db.scalars(select(Document)).all())
    stats = {"simhash_updated": 0, "scanned_refs": 0, "errors": []}
    for d in docs:
        try:
            if Path(d.file_path).exists() and not d.is_scanned:
                text = parse_pdf(d.file_path).body_text()
            else:
                text = _chunks_text(db, d.id)
            d.simhash = simhash64(text)
            stats["simhash_updated"] += 1

            if d.is_scanned:
                rows = list(db.scalars(select(Chunk).where(Chunk.doc_id == d.id)
                                       .order_by(Chunk.page, Chunk.idx)).all())
                blocks = [
                    Block(page=c.page, order=c.idx, col=0, text=c.text,
                          bbox=tuple(float(v) for v in c.bbox.split(",")))
                    for c in rows
                ]
                refs = _fallback_ocr_references(blocks)
                if refs:
                    db.execute(delete(Citation).where(Citation.src_doc_id == d.id))
                    for r in refs:
                        db.add(Citation(src_doc_id=d.id, dst_doc_id=None,
                                        raw_text=r["text"][:2000], ref_index=r["index"],
                                        matched_by="unmatched"))
                    stats["scanned_refs"] += len(refs)
                    log.info("扫描件 %s 补建参考文献 %d 条", d.filename, len(refs))
        except Exception as exc:  # noqa: BLE001 单篇失败不影响其它
            stats["errors"].append(f"{d.filename}: {type(exc).__name__}: {exc}")
            log.warning("修补 %s 失败：%s", d.filename, exc)
    db.commit()
    db.close()
    print(stats)
    # 图谱缓存里的引用匹配基于 Citation，重算一次
    from app.rag.citation_graph import invalidate
    invalidate()


if __name__ == "__main__":
    run()
