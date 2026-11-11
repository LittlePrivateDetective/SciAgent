"""入库管线：上传 → 解析（含扫描件 OCR 兜底）→ 去重 → 切块 → 向量化 → 落库。

所有环节都可单独重跑（幂等），失败会记录 status=failed 并保留错误信息，
方便在演示现场解释"为什么这篇没进库"。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import get_logger
from app.db.models import Chunk, Citation, Document, Section
from app.parse.ocr import ocr_page
from app.parse.structure import Block, ParsedDoc, build_section_paths, parse_pdf
from app.rag.chunker import build_chunks
from app.rag.dedup import content_hash, family_key, find_duplicates, simhash64
from app.rag.embed import embed_texts, to_bytes
from app.rag.hybrid import invalidate_cache
from app.rag.store import get_store

log = get_logger(__name__)

OCR_PAGE_LIMIT = 60  # 扫描件最多 OCR 多少页，防止误传 500 页扫描书把额度烧穿


def save_upload(data: bytes, filename: str) -> Path:
    """落盘到 data/uploads，文件名加随机前缀避免覆盖。"""
    safe = "".join(c for c in filename if c not in '\\/:*?"<>|')[:120]
    path = settings.upload_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(data)
    return path


def _ocr_fill(parsed: ParsedDoc, path: Path) -> ParsedDoc:
    """扫描件：逐页 OCR，把每页结果作为整页区块塞回 blocks。"""
    pages = min(parsed.page_count, OCR_PAGE_LIMIT)
    log.info("检测到扫描件，开始 OCR：共 %d 页（上限 %d）", parsed.page_count, OCR_PAGE_LIMIT)

    new_blocks: list[Block] = []
    got = 0
    for pno in range(1, pages + 1):
        text = ocr_page(str(path), pno)
        if not text:
            continue
        got += 1
        new_blocks.append(
            Block(
                page=pno,
                order=len(new_blocks),
                col=0,
                text=text,
                bbox=(0.0, 0.0, 600.0, 800.0),
                kind="text",
                size=10.0,
            )
        )

    if new_blocks:
        parsed.blocks = new_blocks
        parsed.char_count = sum(len(b.text) for b in new_blocks)
        # 扫描件通常拿不到可靠标题，用文件名兜底
        if not parsed.title:
            parsed.title = path.stem
        log.info("OCR 完成：%d/%d 页有文本", got, pages)
    else:
        log.warning("OCR 未取得任何文本，请检查 VLM 配置或 tesseract 安装")
    return parsed


def ingest(
    db: Session,
    path: Path,
    original_name: str,
    *,
    force: bool = False,
    replace_doc_id: str | None = None,
) -> dict:
    """执行完整入库。返回值直接给前端用。

    force=True        忽略重复检测强制入库
    replace_doc_id    覆盖指定的旧文档（用户在去重弹窗里选"覆盖"）
    """
    t0 = time.time()

    # 1) 解析
    try:
        parsed = parse_pdf(str(path))
    except Exception as exc:  # noqa: BLE001
        log.exception("解析失败：%s", original_name)
        return {"ok": False, "stage": "parse", "error": str(exc)}

    if parsed.is_scanned or parsed.char_count < 200:
        # 扫描件：没有文本层，走 OCR；注意别把 OCR 失败当成解析失败
        try:
            parsed = _ocr_fill(parsed, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 兜底失败：%s", exc)

    full_text = parsed.body_text()
    arxiv_id = parsed.arxiv_id
    title = parsed.title or Path(original_name).stem

    # 2) 去重（用户选"覆盖"时跳过检测）
    dup_report: list[dict] = []
    if not force and not replace_doc_id:
        dups = find_duplicates(db, text=full_text, arxiv_id=arxiv_id, title=title)
        if dups:
            return {
                "ok": False,
                "stage": "duplicate",
                "message": "库中已存在内容高度重合的文献",
                "duplicates": [
                    {
                        "doc_id": d.doc.id,
                        "filename": d.doc.filename,
                        "title": d.doc.title,
                        "version": d.doc.version,
                        "arxiv_id": d.doc.arxiv_id,
                        "reason": d.reason,
                        "similarity": d.similarity,
                        "suggestion": d.suggestion,
                    }
                    for d in dups[:5]
                ],
                "pending_file": str(path),
                "temp_name": original_name,
            }

    # 3) 覆盖旧文档（先删后建）
    if replace_doc_id:
        delete_document(db, replace_doc_id)

    # 4) 切块
    chunks = build_chunks(parsed)
    if not chunks:
        return {"ok": False, "stage": "chunk", "error": "未切出有效文本块，可能是空文件或加密 PDF"}

    # 5) 向量化（批量，失败自动降级 hash 向量）
    t_emb = time.time()
    vectors = embed_texts([c["text"] for c in chunks])
    emb_ms = int((time.time() - t_emb) * 1000)

    # 6) 落库
    version = 1
    fam = family_key(arxiv_id, title, full_text[:8000])
    if replace_doc_id:
        version = 2
    doc = Document(
        filename=original_name,
        title=title[:500],
        authors=json.dumps(parsed.authors, ensure_ascii=False),
        affiliations=json.dumps(parsed.affiliations, ensure_ascii=False),
        abstract=(parsed.abstract or "")[:8000],
        arxiv_id=arxiv_id,
        doi=parsed.doi,
        page_count=parsed.page_count,
        char_count=parsed.char_count,
        is_scanned=int(parsed.is_scanned),
        version=version,
        family_key=fam,
        content_hash=content_hash(full_text),
        simhash=simhash64(full_text),
        file_path=str(path),
        status="ready",
    )
    db.add(doc)
    db.flush()

    section_paths = build_section_paths(parsed.blocks)
    chunk_rows: list[Chunk] = []
    store_items: list[dict] = []

    for i, c in enumerate(chunks):
        row = Chunk(
            doc_id=doc.id,
            idx=i,
            page=c["page"],
            bbox=",".join(f"{v:g}" for v in c["bbox"]),
            text=c["text"],
            section_path=c.get("section_path", "")[:500],
            kind=c.get("kind", "text"),
            char_len=c["char_len"],
            embedding=to_bytes(vectors[i]),
        )
        chunk_rows.append(row)
        store_items.append(
            {
                "id": row.id if row.id else f"{doc.id}_{i}",
                "doc_id": doc.id,
                "page": c["page"],
                "text": c["text"],
                "embedding": vectors[i].tolist(),
            }
        )

    db.add_all(chunk_rows)

    for s in parsed.sections:
        db.add(
            Section(
                doc_id=doc.id,
                level=s["level"],
                title=s["title"],
                page=s["page"],
                order_idx=s["order"],
            )
        )

    # 参考文献先全存为外部节点，后续可一键匹配库内文献
    for r in parsed.references:
        db.add(
            Citation(
                src_doc_id=doc.id,
                dst_doc_id=None,
                raw_text=r["text"][:2000],
                ref_index=r["index"],
                matched_by="unmatched",
            )
        )

    db.commit()
    invalidate_cache()

    # Chroma 双写（如果用 Chroma 后端）
    try:
        store = get_store()
        if store.name == "chroma":
            store.upsert(store_items)
    except Exception as exc:  # noqa: BLE001
        log.warning("向量库双写失败（不影响检索）：%s", exc)

    elapsed = int((time.time() - t0) * 1000)
    log.info(
        "入库成功 %s：%d 块 / 向量 %d 维 / 耗时 %dms（其中向量化 %dms）",
        original_name, len(chunk_rows), vectors.shape[1], elapsed, emb_ms,
    )

    return {
        "ok": True,
        "doc": doc.to_dict(),
        "chunks": len(chunk_rows),
        "sections": len(parsed.sections),
        "references": len(parsed.references),
        "scanned": bool(parsed.is_scanned),
        "elapsed_ms": elapsed,
        "embedding_ms": emb_ms,
        "vector_backend": get_store().name,
    }


def delete_document(db: Session, doc_id: str) -> bool:
    doc = db.get(Document, doc_id)
    if not doc:
        return False
    db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    db.execute(delete(Section).where(Section.doc_id == doc_id))
    db.execute(delete(Citation).where(Citation.src_doc_id == doc_id))
    db.delete(doc)
    db.commit()
    invalidate_cache()
    log.info("已删除文档 %s", doc_id)
    return True


def reembed_all(batch_size: int = 48) -> dict:
    """换 embedding 模型后，重算全库向量（不重新解析 PDF，几秒钟搞定）。

    为什么需要：解析 39 篇 1986 页要几分钟，但换向量模型只跟文本有关。
    分开做，答辩现场可以演示"把兜底 hash 向量切成真实语义向量，检索质量立刻变化"。
    """
    from sqlalchemy import update

    from app.db.session import SessionLocal

    t0 = time.time()
    with SessionLocal() as db:
        rows = db.execute(select(Chunk.id, Chunk.doc_id, Chunk.page, Chunk.text)).all()
    if not rows:
        return {"ok": True, "total": 0, "message": "库中没有可重算的文本块"}

    store = get_store()
    try:
        store.reset()  # 旧向量与新模型不兼容，整体重建
    except Exception as exc:  # noqa: BLE001
        log.warning("重置向量库失败（继续写入）：%s", exc)

    dim = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        vecs = embed_texts([r.text for r in batch])
        dim = int(vecs.shape[1])
        with SessionLocal() as db:
            for r, v in zip(batch, vecs):
                db.execute(update(Chunk).where(Chunk.id == r.id).values(embedding=to_bytes(v)))
            db.commit()
        if store.name == "chroma":
            try:
                store.upsert(
                    [
                        {
                            "id": r.id,
                            "doc_id": r.doc_id,
                            "page": r.page,
                            "text": r.text,
                            "embedding": v.tolist(),
                        }
                        for r, v in zip(batch, vecs)
                    ]
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Chroma 写入失败（SQLite 已写入）：%s", exc)

    invalidate_cache()
    elapsed = int((time.time() - t0) * 1000)
    log.info("全库向量重算完成：%d 块 / %d 维 / %dms", len(rows), dim, elapsed)
    return {
        "ok": True,
        "total": len(rows),
        "dim": dim,
        "provider": settings.emb_provider,
        "model": settings.emb_model if settings.emb_provider == "api" else settings.emb_local_model,
        "elapsed_ms": elapsed,
        "vector_backend": store.name,
    }


def rescan_library() -> dict:
    """把 data/pdf 下的官方测试集批量入库（首次启动/演示前一键灌库）。"""
    from app.db.session import SessionLocal

    pdfs = sorted(settings.pdf_dir.glob("*.pdf"))
    stats = {"total": len(pdfs), "ok": 0, "dup": 0, "failed": 0, "details": []}
    for p in pdfs:
        with SessionLocal() as db:
            exist = db.scalar(select(Document).where(Document.filename == p.name))
            if exist:
                stats["details"].append({"file": p.name, "status": "exists"})
                continue
            r = ingest(db, p, p.name)
            if r.get("ok"):
                stats["ok"] += 1
                stats["details"].append({"file": p.name, "status": "ok", "chunks": r["chunks"]})
            elif r.get("stage") == "duplicate":
                stats["dup"] += 1
                stats["details"].append({"file": p.name, "status": "duplicate"})
            else:
                stats["failed"] += 1
                stats["details"].append({"file": p.name, "status": "failed", "error": r.get("error")})
    return stats
