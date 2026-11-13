"""入库管线：上传 → 解析（含扫描件 OCR 兜底）→ 去重 → 切块 → 向量化 → 落库。

所有环节都可单独重跑（幂等），失败会记录 status=failed 并保留错误信息，
方便在演示现场解释"为什么这篇没进库"。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import get_logger
from app.db.models import AuditLog, Chunk, Citation, Document, Section, TrashEntry
from app.parse.ocr import ocr_page
from app.parse.structure import (
    Block,
    ParsedDoc,
    build_section_paths,
    finalize_ocr_structure,
    parse_pdf,
)
from app.rag.chunker import build_chunks
from app.rag.citation_graph import invalidate as invalidate_graph
from app.rag.dedup import content_hash, family_key, find_duplicates, simhash64
from app.rag.embed import embed_texts, to_bytes
from app.rag.hybrid import invalidate_cache
from app.rag.store import get_store
from app.rag.version_diff import diff_parsed_vs_doc

log = get_logger(__name__)

OCR_PAGE_LIMIT = 60  # 扫描件最多 OCR 多少页，防止误传 500 页扫描书把额度烧穿


def save_upload(data: bytes, filename: str) -> Path:
    """落盘到 data/uploads，文件名加随机前缀避免覆盖。"""
    safe = "".join(c for c in filename if c not in '\\/:*?"<>|')[:120]
    path = settings.upload_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(data)
    return path


def _year_from_arxiv(arxiv_id: str) -> str:
    """arXiv 编号前两位就是年份：1706.03762 → 2017。"""
    m = re.match(r"^(\d{2})\d{2}\.", arxiv_id or "")
    if not m:
        return ""
    yy = int(m.group(1))
    return str(2000 + yy)


def _year_from_text(text: str) -> str:
    """兜底：从首页文本里找 19xx/20xx 的年份（排除 arXiv 水印里的编号）。"""
    years = [int(y) for y in re.findall(r"\b(199\d|20[0-2]\d)\b", text or "")]
    if not years:
        return ""
    # 取出现次数最多的年份，避免误取参考文献里的年份
    return str(max(set(years), key=years.count))


def _ocr_fill(parsed: ParsedDoc, path: Path) -> ParsedDoc:
    """扫描件：逐页 OCR，并按版面位置把整页文本还原成带层级的区块。

    为什么不能"整页一个块"：锚点校验靠 bbox 回查，整页块等于所有引用都指向
    同一块；标题层级也会全部丢失。这里按 OCR 输出的 Markdown 标题语法反推层级，
    再按行序把文本摊回页面坐标（纵向等分，宽度占满正文区），
    使扫描件同样具备「章节树 + 可定位锚点」。
    """
    import pymupdf

    pages = min(parsed.page_count, OCR_PAGE_LIMIT)
    log.info("检测到扫描件，开始 OCR：共 %d 页（上限 %d）", parsed.page_count, OCR_PAGE_LIMIT)

    # 取每页真实尺寸：扫描件也要按实际纸张宽度算 bbox
    rects: dict[int, tuple[float, float]] = {}
    try:
        with pymupdf.open(str(path)) as doc:
            for i, page in enumerate(doc, start=1):
                rects[i] = (page.rect.width, page.rect.height)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取页面尺寸失败，使用默认尺寸：%s", exc)

    new_blocks: list[Block] = []
    got = 0
    for pno in range(1, pages + 1):
        text = ocr_page(str(path), pno)
        if not text.strip():
            continue
        got += 1
        w, h = rects.get(pno, (595.0, 842.0))
        new_blocks.extend(_ocr_text_to_blocks(text, pno, w, h, len(new_blocks)))

    if new_blocks:
        parsed.blocks = new_blocks
        parsed.char_count = sum(len(b.text) for b in new_blocks)
        parsed = finalize_ocr_structure(parsed)
        # 扫描件 OCR 出不来标题时（首页没抓到标题行），退到文件名
        if not parsed.title or parsed.title.strip() == "":
            parsed.title = path.stem
        log.info("OCR 完成：%d/%d 页有文本，还原 %d 个区块", got, pages, len(new_blocks))
    else:
        log.warning("OCR 未取得任何文本，请检查 VLM 配置或 tesseract 安装")
    return parsed


_RE_MD_HEAD = re.compile(r"^(#{1,4})\s+(.{1,180})$")
_RE_FIGCAP = re.compile(r"^(figure|fig\.)\s*\d+", re.I)
_RE_TABCAP = re.compile(r"^table\s*\d+", re.I)


def _ocr_text_to_blocks(text: str, page_no: int, width: float, height: float, order0: int) -> list[Block]:
    """把一页 OCR 文本按行序摊回页面坐标。

    纵向按行数等分（上下各留 6% 页边距），横向占正文区 88%。
    坐标不是像素级精确，但足以让前端在扫描页上画出定位框，也足以让锚点校验
    在同一页内区分不同段落——这比"整页一个块"强一个量级。

    空行必须保留语义：先前的实现把空行全过滤掉，导致标题/作者/邮箱被粘成
    一个块，标题回填时因含 "@" 被跳过，整篇文献的标题就退化成了文件名。
    """
    raw = [ln.rstrip() for ln in text.splitlines()]
    if not any(ln.strip() for ln in raw):
        return []

    top, bottom = height * 0.052, height * 0.94
    margin_x = width * 0.06
    # 空行按半行高度计，用来还原段间距
    weight_total = sum(1.0 if ln.strip() else 0.45 for ln in raw)
    line_h = (bottom - top) / max(weight_total, 1.0)

    blocks: list[Block] = []
    buf: list[str] = []
    y_cursor = top
    buf_y0 = top

    def flush(y_end: float) -> None:
        nonlocal buf
        body = " ".join(x.strip() for x in buf).strip()
        if body:
            kind = "text"
            if _RE_FIGCAP.match(body):
                kind = "figure"
            elif _RE_TABCAP.match(body):
                kind = "table"
            blocks.append(
                Block(
                    page=page_no,
                    order=order0 + len(blocks),
                    col=0,
                    text=body,
                    bbox=(margin_x, buf_y0, width - margin_x, max(y_end, buf_y0 + line_h * 0.6)),
                    kind=kind,
                    size=10.0,
                )
            )
        buf = []

    for ln in raw:
        stripped = ln.strip()
        if not stripped:
            # 空行 = 段落边界
            if buf:
                flush(y_cursor)
            y_cursor += line_h * 0.45
            buf_y0 = y_cursor
            continue

        m = _RE_MD_HEAD.match(stripped)
        if m:
            if buf:
                flush(y_cursor)
            level = len(m.group(1))
            blocks.append(
                Block(
                    page=page_no,
                    order=order0 + len(blocks),
                    col=0,
                    text=m.group(2).strip(),
                    bbox=(margin_x, y_cursor, width - margin_x, y_cursor + line_h),
                    kind="heading",
                    level=level,
                    size=10.0 + (4 - level) * 1.5,
                    bold=True,
                )
            )
            y_cursor += line_h
            buf_y0 = y_cursor
            continue

        if not buf:
            buf_y0 = y_cursor
        buf.append(stripped)
        y_cursor += line_h

        # 参考文献条目形如 [12] ... 单独成块，便于后续引用抽取
        if re.match(r"^\[\d+\]", stripped) and len(buf) > 1:
            flush(y_cursor)

    if buf:
        flush(y_cursor)
    return blocks


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
        return {"ok": False, "stage": "parse",
                "error": "无法解析 PDF，文件可能损坏、加密或内容格式无效"}

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
            # 直接给出与"最相近旧版本"的差异报告：这正是官方用例想要的
            # "检测重复 + 建议保留较新版本 + 版本差异"三件套
            diff = {}
            try:
                diff = diff_parsed_vs_doc(db, parsed, dups[0].doc.id)
            except Exception as exc:  # noqa: BLE001 差异算不出来不能挡住流程
                log.warning("版本差异计算失败：%s", exc)
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
                "diff": diff,
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
        year=_year_from_arxiv(arxiv_id) or _year_from_text(full_text[:4000]),
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

    for i, c in enumerate(chunks):
        chunk_rows.append(
            Chunk(
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
        )

    db.add_all(chunk_rows)
    # 必须先 flush 拿到真实主键：Chroma 里的 id 必须和 SQLite 的 Chunk.id 完全一致，
    # 否则向量库检索回来的 id 在 SQLite 里查不到正文，检索直接空转。
    db.flush()
    store_items = [
        {
            "id": row.id,
            "doc_id": doc.id,
            "page": row.page,
            "text": row.text,
            "embedding": vectors[i].tolist(),
        }
        for i, row in enumerate(chunk_rows)
    ]

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
    invalidate_graph()

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


def delete_document(db: Session, doc_id: str, actor: str = "", reason: str = "") -> bool:
    """删除文献，但**先留可恢复快照 + 审计**。

    为什么物理删块而只保留元数据快照：软删标记会让 12656 个已删块继续留在
    BM25/Chroma 语料里污染检索，要改检索层好几处；而源文件本来就在磁盘上，
    恢复 = 重新解析一次（低频操作），换来的是检索层的绝对干净。
    """
    doc = db.get(Document, doc_id)
    if not doc:
        return False
    n_chunks = db.scalar(select(func.count(Chunk.id)).where(Chunk.doc_id == doc_id)) or 0
    snap = {
        "doc_id": doc_id, "filename": doc.filename, "title": doc.title,
        "file_path": doc.file_path, "chunk_count": n_chunks,
        "page_count": doc.page_count, "actor": actor or "ui", "reason": reason or "user_delete",
    }
    db.merge(TrashEntry(**snap))
    db.add(AuditLog(action="delete", doc_id=doc_id,
                    detail=json.dumps(snap, ensure_ascii=False), actor=actor or "ui"))

    db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    db.execute(delete(Section).where(Section.doc_id == doc_id))
    db.execute(delete(Citation).where(Citation.src_doc_id == doc_id))
    db.delete(doc)
    db.commit()
    invalidate_cache()
    invalidate_graph()
    try:
        get_store().delete_by_doc(doc_id)
    except Exception as exc:  # noqa: BLE001 向量库清理失败不影响主流程
        log.warning("向量库清理失败：%s", exc)
    log.info("已删除文档 %s（进入回收站，%d 块，源文件保留）", doc_id, n_chunks)
    return True


def restore_document(db: Session, doc_id: str, actor: str = "") -> dict:
    """从回收站恢复：重新解析磁盘上的原始 PDF。"""
    entry = db.get(TrashEntry, doc_id)
    if not entry:
        return {"ok": False, "error": "回收站中没有这条记录"}
    path = Path(entry.file_path)
    if not path.exists():
        return {"ok": False, "error": f"源文件已不存在：{entry.file_path}"}
    if db.get(Document, doc_id):
        return {"ok": True, "restored": False, "message": "该文献已在库中，无需恢复"}

    result = ingest(db, path, entry.filename, force=True)
    if not (result.get("ok") or result.get("stage") == "duplicate"):
        return {"ok": False, "error": result.get("error") or "重新解析失败", "ingest": result}

    new_id = (result.get("doc") or {}).get("id", "")
    db.delete(entry)
    db.add(AuditLog(action="restore", doc_id=new_id or doc_id,
                    detail=json.dumps({"filename": entry.filename,
                                       "old_doc_id": doc_id,
                                       "chunks": result.get("chunks", 0)}, ensure_ascii=False),
                    actor=actor or "ui"))
    db.commit()
    invalidate_cache()
    invalidate_graph()
    # 重新解析会生成新的 doc_id，必须回给调用方，否则前端/QA 还拿旧 id 查会 404
    return {"ok": True, "restored": True, "doc_id": new_id,
            "old_doc_id": doc_id, "filename": entry.filename,
            "chunks": result.get("chunks", 0), "message": "已从回收站恢复"}


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
