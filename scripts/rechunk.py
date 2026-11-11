"""原地重建 chunk（保留 doc_id）。

为什么需要这个脚本：切块规则改了（表格块要把网格行吃进来，见 D-67），
但库里存的是按旧规则切好的块。用 ingest(replace_doc_id=...) 会**新建一个 doc_id**，
译文缓存、引用边、外部深链全部失效；而解析本身没变，只有切块变了。
所以这里只做"重新切块 + 重新向量化 + 替换该文档的 chunks"，文档行原样保留。

用法：
    ./.venv/Scripts/python.exe scripts/rechunk.py --tables-only --dry-run
    ./.venv/Scripts/python.exe scripts/rechunk.py --tables-only
    ./.venv/Scripts/python.exe scripts/rechunk.py --doc-id dce63a636cf64
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.core.logger import get_logger  # noqa: E402
from app.db.models import Chunk, Document  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.parse.structure import parse_pdf  # noqa: E402
from app.pipeline.ingest import _ocr_fill  # noqa: E402
from app.rag.chunker import build_chunks  # noqa: E402
from app.rag.embed import embed_texts, to_bytes  # noqa: E402
from app.rag.hybrid import invalidate_cache  # noqa: E402
from app.rag.store import get_store  # noqa: E402

log = get_logger("rechunk")


def target_docs(tables_only: bool) -> list[tuple[str, str, str]]:
    """返回 [(doc_id, filename, file_path)]。"""
    with SessionLocal() as db:
        q = select(Document.id, Document.filename, Document.file_path, Document.is_scanned)
        rows = db.execute(q.order_by(Document.filename)).all()
        # 扫描件跳过：重新解析等于重跑一次 VLM OCR，正文会变、耗时且花钱，
        # 而表格提取本来就不可靠（无文本层），收益不抵风险。
        rows = [r for r in rows if not r[3]]
        if tables_only:
            keep = set(db.scalars(select(Chunk.doc_id).where(Chunk.kind == "table")).all())
            rows = [r for r in rows if r[0] in keep]
        return [(r[0], r[1], r[2]) for r in rows]


def rechunk_one(doc_id: str, name: str, path: Path, dry: bool) -> dict:
    t0 = time.time()
    if not path.exists():
        return {"doc": name, "ok": False, "error": f"文件不存在：{path}"}

    old = None
    with SessionLocal() as db:
        rows = db.execute(
            select(Chunk.kind, Chunk.char_len).where(Chunk.doc_id == doc_id)
        ).all()
        old = {
            "chunks": len(rows),
            "tables": sum(1 for k, _ in rows if k == "table"),
            "table_chars": sum(c for k, c in rows if k == "table"),
        }

    parsed = parse_pdf(str(path))
    if parsed.is_scanned or parsed.char_count < 200:
        try:
            parsed = _ocr_fill(parsed, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 兜底失败 %s：%s", name, exc)

    chunks = build_chunks(parsed)
    if not chunks:
        return {"doc": name, "ok": False, "error": "切块为空"}

    new = {
        "chunks": len(chunks),
        "tables": sum(1 for c in chunks if c["kind"] == "table"),
        "table_chars": sum(c["char_len"] for c in chunks if c["kind"] == "table"),
    }
    if dry:
        return {"doc": name, "ok": True, "dry": True, "old": old, "new": new,
                "seconds": round(time.time() - t0, 1)}

    vectors = embed_texts([c["text"] for c in chunks])

    store_items: list[dict] = []
    with SessionLocal() as db:
        db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        rows: list[Chunk] = []
        for i, c in enumerate(chunks):
            row = Chunk(
                doc_id=doc_id,
                idx=i,
                page=c["page"],
                bbox=",".join(f"{v:g}" for v in c["bbox"]),
                text=c["text"],
                section_path=c.get("section_path", "")[:500],
                kind=c.get("kind", "text"),
                char_len=c["char_len"],
                embedding=to_bytes(vectors[i]),
            )
            db.add(row)
            rows.append(row)
        # Chroma 的 id 必须与 SQLite 主键一致，否则检索回来的 id 查不到正文
        db.flush()
        store_items = [
            {"id": r.id, "doc_id": doc_id, "page": r.page, "text": r.text,
             "embedding": vectors[i].tolist()}
            for i, r in enumerate(rows)
        ]
        db.commit()

    # 向量库是另一份副本：先按文档清旧，再写新，否则检索仍命中已删除的块
    try:
        store = get_store()
        store.delete_by_doc(doc_id)
        if getattr(store, "name", "") == "chroma":
            store.upsert(store_items)
    except Exception as exc:  # noqa: BLE001 向量库失败不影响 DB 侧（SQLite 始终是事实来源）
        log.warning("向量库同步失败 %s：%s", name, exc)

    invalidate_cache()
    return {"doc": name, "ok": True, "old": old, "new": new,
            "seconds": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables-only", action="store_true", help="只重建含表格块的文献")
    ap.add_argument("--doc-id", default="", help="只重建指定文档")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()

    docs = target_docs(args.tables_only)
    if args.doc_id:
        docs = [d for d in docs if d[0] == args.doc_id]
    if not docs:
        print("没有匹配的文档")
        return

    print(f"目标 {len(docs)} 篇{'（仅报告）' if args.dry_run else ''}")
    ok = 0
    tot_old = tot_new = 0
    for doc_id, name, path in docs:
        r = rechunk_one(doc_id, name, Path(path), args.dry_run)
        if r.get("ok"):
            ok += 1
            o, n = r["old"], r["new"]
            tot_old += o["table_chars"]
            tot_new += n["table_chars"]
            print(
                f"  ✓ {name[:42]:42s} 块 {o['chunks']:5d}→{n['chunks']:5d} "
                f"表块 {o['tables']:3d}→{n['tables']:3d} "
                f"表块字数 {o['table_chars']:6d}→{n['table_chars']:6d} {r['seconds']}s"
            )
        else:
            print(f"  ✗ {name[:42]:42s} {r.get('error')}")

    print(f"\n完成 {ok}/{len(docs)}　表格块总字数 {tot_old} → {tot_new}"
          f"（增长 {(tot_new / max(tot_old, 1) - 1) * 100:.0f}%）")


if __name__ == "__main__":
    main()
