"""章节结构自愈：用**当前**解析器重建已入库文档的章节树。

为什么需要这一层（而不是重新入库）：云端 data 目录是持久化的，换代码不换数据。
解析器修好之后（中文期刊的小节标题、参考文献区、作者名单误判……），
线上老库里的 sections 还是旧结果——用户点开阅读器看到的仍是乱码一样的章节树，
"代码修好了但界面没变"，这是最容易白干的一类问题。

为什么只重建 sections + chunks.section_path：
- 章节树完全由"块分类"决定，与切块/向量无关；
- 重新入库会把两万多个分块重新向量化（几分钟 + 真金白银的接口费）；
- 分块的章节归属可以只靠"页码 + 坐标"重算：分块内的每个块都落在同一节里，
  按与分块包围盒最近的区块就能取回正确的 section_path。
"""
from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import resolve_doc_file
from app.core.logger import get_logger
from app.db.models import AppSetting, Chunk, Document, Section
from app.parse.structure import PARSE_VERSION, build_section_paths, parse_pdf

log = get_logger(__name__)

VERSION_KEY = "parse_version"
# 分块与区块的坐标匹配容差（pt²）：分块是从区块切出来的，正常误差在几 pt 以内，
# 超过这个距离说明页码对不上（比如跨页合并块），宁可不动也不写错。
_MAX_DIST2 = 300.0 ** 2


def _looks_broken(titles: list[str]) -> bool:
    """章节树"看起来就是坏的"的三条判据（用于只修坏的那几篇）。

    1. 出现单字/两字片段（"林"、"威"、"言"）——典型的中文标题被拆碎；
    2. 标题里带全角逗号/括号——是正文句子或公式碎片，不是标题；
    3. 标题数异常多（>200），或一条都没有。
    """
    if not titles:
        return True
    if len(titles) > 200:
        return True
    for t in titles:
        s = t.strip()
        body = s.lstrip("0123456789. ").strip()
        if len(body) <= 1:
            return True
        if any(c in s for c in "，、；（）("):
            return True
    return False


def _suspect_docs(db: Session) -> list[Document]:
    """挑出章节树可疑的文档；全库一条章节都没有时说明是老库，返回全部。"""
    docs = list(db.scalars(select(Document)).all())
    per_doc: dict[str, list[str]] = {}
    for sid, doc_id, title in db.execute(select(Section.id, Section.doc_id, Section.title)):
        per_doc.setdefault(doc_id, []).append(title)
    if not per_doc:
        return docs
    return [d for d in docs if _looks_broken(per_doc.get(d.id, []))]


def _remap_chunk_paths(db: Session, doc: Document, parsed) -> int:
    """按坐标把每个分块对回新章节树里的路径。"""
    paths = build_section_paths(parsed.blocks)
    by_page: dict[int, list] = {}
    for b in parsed.blocks:
        by_page.setdefault(b.page, []).append(b)

    rows = list(db.scalars(select(Chunk).where(Chunk.doc_id == doc.id)).all())
    changed = 0
    for c in rows:
        try:
            x0, y0, x1, y1 = (float(v) for v in (c.bbox or "").split(","))
        except (TypeError, ValueError):
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best, best_d = None, _MAX_DIST2
        for b in by_page.get(c.page, ()):
            bx, by = (b.bbox[0] + b.bbox[2]) / 2, (b.bbox[1] + b.bbox[3]) / 2
            d = (bx - cx) ** 2 + (by - cy) ** 2
            if d < best_d:
                best, best_d = b, d
        if best is None:
            continue
        path = (paths.get(best.order, "") or "")[:500]
        if path != (c.section_path or ""):
            c.section_path = path
            changed += 1
    return changed


def reparse_document(db: Session, doc: Document, *, remap_chunks: bool = True) -> dict:
    """单篇重建：重解析 PDF → 重写 sections → 重算分块的 section_path。"""
    path = resolve_doc_file(doc)
    if path is None:
        return {"ok": False, "doc_id": doc.id, "reason": "源文件缺失"}
    try:
        parsed = parse_pdf(str(path))
    except Exception as exc:  # noqa: BLE001 单篇失败不能拖垮整批
        log.warning("重建章节树失败（%s）：%s", doc.filename, exc)
        return {"ok": False, "doc_id": doc.id, "reason": str(exc)}

    old = db.scalar(
        select(Section.id).where(Section.doc_id == doc.id).limit(1)
    )
    db.execute(delete(Section).where(Section.doc_id == doc.id))
    for s in parsed.sections:
        db.add(Section(doc_id=doc.id, level=s["level"], title=s["title"],
                       page=s["page"], order_idx=s["order"]))
    changed = _remap_chunk_paths(db, doc, parsed) if remap_chunks else 0
    db.commit()
    log.info("章节树重建 %s：%d 条（原 %s）/ 分块路径更新 %d 条",
             doc.filename, len(parsed.sections), "有" if old else "无", changed)
    return {"ok": True, "doc_id": doc.id, "filename": doc.filename,
            "sections": len(parsed.sections), "chunks_updated": changed}


def _read_version(db: Session) -> str:
    row = db.get(AppSetting, VERSION_KEY)
    if row is None:
        return ""
    try:
        return str(json.loads(row.value))
    except (TypeError, ValueError):
        return ""


def heal_structures(db: Session, *, force: bool = False, only_broken: bool = False) -> dict:
    """按解析器版本号决定要不要重建；返回统计，供日志和接口使用。

    force=True 忽略版本号强制重建。only_broken=True 只修"一眼就是坏的"（单字章节、
    正文句子当标题），用于文献量很大、想先快速修一批的场合。

    默认**整库重建**而不是只修可疑的：解析器一变，输出就可能整篇变样，
    但"变样"未必"看起来是坏的"——实测那三篇中文期刊修完只剩 6 个一级标题，
    形态上完全正常，实际整篇的小节都丢了，靠启发式根本挑不出来。
    """
    current = _read_version(db)
    if not force and current == PARSE_VERSION:
        return {"ok": True, "skipped": True, "version": current, "repaired": 0}

    docs = _suspect_docs(db) if only_broken else list(db.scalars(select(Document)).all())
    repaired = failed = 0
    for doc in docs:
        r = reparse_document(db, doc)
        if r.get("ok"):
            repaired += 1
        else:
            failed += 1
    row = db.get(AppSetting, VERSION_KEY)
    if row is None:
        db.add(AppSetting(key=VERSION_KEY, value=json.dumps(PARSE_VERSION), actor="system"))
    else:
        row.value = json.dumps(PARSE_VERSION)
    db.commit()
    log.info("章节结构自愈：版本 %s → %s，重建 %d 篇，失败 %d 篇",
             current or "（无）", PARSE_VERSION, repaired, failed)
    return {"ok": True, "skipped": False, "version": PARSE_VERSION,
            "candidates": len(docs), "repaired": repaired, "failed": failed}
