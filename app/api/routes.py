"""HTTP 接口层：文档、检索问答、图谱、系统自检。"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.graph import run as run_agent
from app.core.config import resolve_doc_file, settings
from app.core.llm import gateway, want_thinking
from app.core.logger import get_logger
from app.db.models import (
    AuditLog,
    Chunk,
    Citation,
    Document,
    Lesson,
    Section,
    Task,
    Translation,
    TrashEntry,
)
from app.db.session import get_db
from app.parse.ocr import ocr_available
from app.rag.focus import annotate_focus
from app.rag.bilingual import (
    build_segments,
    cached_ratio,
    en_status,
    export_zh,
    job_status,
    start_en_job,
    start_job,
    translate_pages,
    translate_selection,
)
from app.rag.citation_graph import build_graph as build_citation_graph
from app.rag.citation_graph import invalidate as invalidate_graph
from app.parse.structure import render_page_png
from app.pipeline.ingest import delete_document, ingest, rescan_library, restore_document, save_upload
from app.rag.anchors import attach_anchors
from app.rag.anchors import verify as verify_answer
from app.rag.hybrid import search_with_rerank
from app.rag.store import get_store
from app.research.future_work import collect_future_work
from app.research.reproducibility import collect_reproducibility
from app.research import discover as discover_mod
from app.research.writer import generate as generate_outline
from app.tools.chart import convert as convert_chart
from app.tools.external import TOOL_SCHEMAS
from app.tools.external import decide as tool_decide
from app.tools.external import invoke as tool_invoke
from app.tools.topology import generate as generate_topology

log = get_logger(__name__)
router = APIRouter(prefix="/api")

# 去重拦截后暂存的文件（pending_id -> 路径），等用户选完再入库
PENDING: dict[str, dict] = {}


# ---------------- 系统 ----------------
@router.get("/system/info")
def system_info(db: Session = Depends(get_db)):
    return {
        "ok": True,
        "llm": gateway.describe(),
        "vector_backend": get_store().name,
        "ocr": ocr_available(),
        "embedding": {"provider": settings.emb_provider, "model": settings.emb_model, "dim": settings.emb_dim},
        "rerank": {
            "provider": settings.rerank_provider,
            "model": settings.rerank_model,
            "enabled": bool(
                (settings.rerank_api_key or settings.emb_api_key or settings.llm_api_key)
                and settings.rerank_url
                and settings.rerank_provider.lower() != "none"
            ),
        },
        "counts": {
            "documents": db.scalar(select(func.count(Document.id))) or 0,
            "chunks": db.scalar(select(func.count(Chunk.id))) or 0,
            "tasks": db.scalar(select(func.count(Task.id))) or 0,
            "lessons": db.scalar(select(func.count(Lesson.id))) or 0,
        },
        # 对照阅读的翻译模型与缓存进度（前端据此决定是否提示"整篇翻译"）
        "translation": {"model": settings.trans_model, "batch": settings.trans_batch},
        # 前端「快速 / 深思」按钮的初始状态跟着这里走
        "thinking": bool(settings.llm_thinking),
        "keepalive": _keepalive_status(),
        "official_pdfs": len(list(settings.pdf_dir.glob("*.pdf"))),
    }


def _keepalive_status() -> dict:
    from app.core import keepalive

    return keepalive.status()


@router.get("/keepalive")
def keepalive_info():
    """后台保活状态：最近一次心跳、耗时、累计次数、最近一次错误。"""
    return {"ok": True, **_keepalive_status()}


@router.post("/keepalive/beat")
def keepalive_beat():
    """手动补一次心跳。

    外部 cron / Windows 计划任务保活时打这个接口即可（见 scripts/keepalive.sh）。
    30 秒内重复调用返回上次结果，不会被刷着烧 token。
    """
    from app.core import keepalive

    return {"ok": True, **keepalive.beat(manual=True)}


@router.get("/warm")
def warm():
    """预热：让第一个真实提问不再承担冷启动成本（逻辑见 app/core/warmup.py）。"""
    from app.core.warmup import warm_once

    return warm_once(tag="http")


# ---------------- 文档 ----------------
@router.post("/system/reembed")
def system_reembed():
    """换 embedding 模型后重算全库向量（不重新解析 PDF）。"""
    from app.pipeline.ingest import reembed_all

    return reembed_all()


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    force: bool = Form(False),
    replace_doc_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF")

    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB 限制")

    path = save_upload(data, file.filename)
    result = ingest(db, path, file.filename, force=force, replace_doc_id=replace_doc_id)

    if not result.get("ok") and result.get("stage") == "duplicate":
        pending_id = uuid.uuid4().hex[:10]
        PENDING[pending_id] = {"path": path, "name": file.filename}
        result["pending_id"] = pending_id
        return JSONResponse(result, status_code=200)

    return result


class DupResolve(BaseModel):
    pending_id: str
    action: str = Field(..., description="keep_new=用新文件覆盖 / keep_old=保留库中版本 / both=两份都留")
    target_doc_id: str | None = None


@router.post("/documents/resolve")
def resolve_duplicate(body: DupResolve, db: Session = Depends(get_db)):
    item = PENDING.get(body.pending_id)
    if not item:
        raise HTTPException(404, "待处理的上传已过期，请重新上传")

    path = Path(item["path"])
    if body.action == "keep_old":
        PENDING.pop(body.pending_id, None)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "action": "keep_old", "message": "已保留库中版本，新文件未入库"}

    replace_id = body.target_doc_id if body.action == "keep_new" else None
    result = ingest(db, path, item["name"], force=True, replace_doc_id=replace_id)
    PENDING.pop(body.pending_id, None)
    return result


@router.get("/documents/{doc_id}/diff")
def document_diff(doc_id: str, other_id: str, db: Session = Depends(get_db)):
    """版本差异：以 other_id 为旧版、doc_id 为新版，返回章节/句子级差异。"""
    from app.rag.version_diff import diff_docs

    for did in (doc_id, other_id):
        if not db.get(Document, did):
            raise HTTPException(404, f"文档不存在：{did}")
    return diff_docs(db, other_id, doc_id)


# 必须注册在 /documents/{doc_id} 之前，否则 "trash" 会被当成 doc_id 吃掉
@router.get("/documents/trash")
def list_trash(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(TrashEntry).order_by(TrashEntry.deleted_at.desc())).all())
    return {"items": [
        {"doc_id": r.doc_id, "filename": r.filename, "title": r.title,
         "chunk_count": r.chunk_count, "page_count": r.page_count,
         "actor": r.actor, "reason": r.reason,
         "deleted_at": r.deleted_at.isoformat(), "restorable": Path(r.file_path).exists()}
        for r in rows
    ]}


class BatchDeleteIn(BaseModel):
    ids: list[str] = []
    reason: str = "batch_delete"


@router.post("/documents/batch-delete")
def batch_remove_documents(body: BatchDeleteIn, db: Session = Depends(get_db)):
    """批量删除 → 逐篇进回收站（可恢复）+ 审计，返回逐篇结果。

    为什么不复用前端循环调单删接口：38 篇会变成 38 次网络往返 + 38 次独立事务，
    中途失败还会留下"删了一半"的界面——用户不知道到底哪几篇没了。
    收口到一个接口，失败项单独列出来，前端能如实显示"删了 N 篇、M 篇失败"。

    删除语义与单篇完全一致（走 delete_document：留快照、写审计、清 chunk、
    失效检索/图谱缓存），所以不会出现"批量删的恢复不了"这种两套行为。
    """
    ids = [x for x in dict.fromkeys(body.ids or []) if x]   # 去重且保序
    if not ids:
        raise HTTPException(400, "没有选择任何文献")
    if len(ids) > 300:
        raise HTTPException(400, "一次最多删除 300 篇，请分批操作")

    done: list[str] = []
    failed: list[dict] = []
    for doc_id in ids:
        try:
            if delete_document(db, doc_id, actor="ui", reason=body.reason or "batch_delete"):
                done.append(doc_id)
            else:
                failed.append({"id": doc_id, "error": "文档不存在"})
        except Exception as exc:  # noqa: BLE001 单篇失败不拖垮整批
            db.rollback()
            log.warning("批量删除：%s 失败 %s", doc_id, exc)
            failed.append({"id": doc_id, "error": f"{type(exc).__name__}: {exc}"[:200]})

    return {
        "ok": True,
        "deleted": len(done),
        "ids": done,
        "failed": failed,
        "message": f"已移入回收站 {len(done)} 篇"
                   + (f"，{len(failed)} 篇失败" if failed else "")
                   + "（左栏「回收」可恢复）",
    }


@router.get("/documents")
def list_documents(db: Session = Depends(get_db)):
    docs = list(db.scalars(select(Document).order_by(Document.created_at.desc())).all())
    out = []
    for d in docs:
        item = d.to_dict()
        item["chunks"] = db.scalar(select(func.count(Chunk.id)).where(Chunk.doc_id == d.id)) or 0
        out.append(item)
    return {"documents": out}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc.to_dict()


@router.delete("/documents/{doc_id}")
def remove_document(doc_id: str, request: Request, actor: str = "ui", reason: str = "",
                    db: Session = Depends(get_db)):
    """删除 → 回收站（可恢复）+ 审计。源文件永远保留在磁盘。"""
    if not delete_document(db, doc_id, actor=actor, reason=reason or "api_delete"):
        raise HTTPException(404, "文档不存在")
    return {"ok": True, "trash": True, "message": "已移入回收站，可用 /api/documents/{id}/restore 恢复"}


@router.get("/documents/{doc_id}/sections")
def get_sections(doc_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    rows = list(
        db.scalars(select(Section).where(Section.doc_id == doc_id).order_by(Section.order_idx)).all()
    )
    return {"sections": [{"level": s.level, "title": s.title, "page": s.page} for s in rows]}


@router.get("/documents/{doc_id}/chunks")
def get_chunks(doc_id: str, page: int | None = None, db: Session = Depends(get_db)):
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    stmt = select(Chunk).where(Chunk.doc_id == doc_id)
    if page:
        stmt = stmt.where(Chunk.page == page)
    rows = list(db.scalars(stmt.order_by(Chunk.idx)).all())
    return {
        "chunks": [
            {
                "id": c.id,
                "page": c.page,
                "bbox": [float(v) for v in c.bbox.split(",")],
                "text": c.text,
                "kind": c.kind,
                "section_path": c.section_path,
            }
            for c in rows
        ]
    }


@router.get("/documents/{doc_id}/page/{page}.png")
def get_page_image(doc_id: str, page: int, dpi: int = Query(130, ge=72, le=220), db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    try:
        src = resolve_doc_file(doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("页图：路径解析失败 doc=%s file_path=%r: %s",
                    doc_id, getattr(doc, "file_path", None), exc)
        raise HTTPException(404, f"源文件路径解析失败：{exc}") from exc
    if not doc or src is None:
        log.warning("页图：源文件找不到 doc=%s file_path=%r", doc_id, getattr(doc, "file_path", None))
        raise HTTPException(404, "文档或源文件不存在")
    try:
        png = render_page_png(str(src), page, dpi=dpi)
    except Exception as exc:  # noqa: BLE001
        log.warning("页图渲染失败 doc=%s page=%s src=%s: %s", doc_id, page, src, exc)
        raise HTTPException(400, f"渲染失败：{exc}") from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/documents/{doc_id}/pages")
def get_page_meta(doc_id: str, db: Session = Depends(get_db)):
    """返回每页的原始尺寸（pt），前端据此把 bbox 换算成百分比，做高亮覆盖层。"""
    import pymupdf

    doc = db.get(Document, doc_id)
    try:
        src = resolve_doc_file(doc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"源文件路径解析失败：{exc}") from exc
    if not doc or src is None:
        raise HTTPException(404, "文档不存在")
    out = []
    with pymupdf.open(str(src)) as pdf:
        for i, page in enumerate(pdf, start=1):
            out.append({"page": i, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2)})
    return {"pages": out}


@router.get("/documents/{doc_id}/page/{page}/text")
def get_page_text(doc_id: str, page: int, db: Session = Depends(get_db)):
    """页内词级文本层：让页图上的原文可被选中、角标可被点击。

    阅读器主视图是 PNG 页图（排版 100% 还原），代价是正文没有任何可交互的文本。
    官方 1.3.2 要求「点正文里的 [11] 跳到 References」、1.3.3.1 要求「划词翻译」，
    两者都必须有文本层。这里按词返回 bbox，前端用**透明文字**绝对定位覆盖在页图上：
    视觉仍是原图，但文字可选、可点。

    用紧凑数组而非对象（一页约 500 词，对象序列化会白白多出几十 KB）：
    [text, x, y, w, h]
    """
    import pymupdf

    doc = db.get(Document, doc_id)
    try:
        src = resolve_doc_file(doc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"源文件路径解析失败：{exc}") from exc
    if not doc or src is None:
        raise HTTPException(404, "文档或源文件不存在")
    try:
        with pymupdf.open(str(src)) as pdf:
            if not 1 <= page <= len(pdf):
                raise HTTPException(404, f"页码超出范围（共 {len(pdf)} 页）")
            pg = pdf[page - 1]
            words = [
                [w[4], round(w[0], 1), round(w[1], 1), round(w[2] - w[0], 1), round(w[3] - w[1], 1)]
                for w in pg.get_text("words")
                if w[4].strip()
            ]
            width, height = round(pg.rect.width, 2), round(pg.rect.height, 2)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"文本层提取失败：{exc}") from exc

    # 扫描件没有文本层：如实返回空列表，让前端提示「该页为扫描图，划词翻译不可用」，
    # 而不是假装成功再让用户对着一片死区发呆。
    return {
        "page": page,
        "width": width,
        "height": height,
        "has_text_layer": bool(words),
        "words": words,
    }


@router.post("/documents/{doc_id}/page/{page}/translate")
def translate_page_text(doc_id: str, page: int, db: Session = Depends(get_db)):
    """扫描件整页翻译：页面上选不出字，就只能由服务端把该页文本整体送去译。

    与 `/translate` 的分工：`/translate` 收前端提交的任意片段（划词用）；
    这里前端手里根本没有文本——扫描件没有词级文本层，选取不到任何字符，
    所以文本由服务端从库内 chunk 按段落顺序取。参考文献条目跳过：
    保留原文更利于与页图逐条核对，也少占输出预算。
    """
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    rows = list(
        db.scalars(
            select(Chunk)
            .where(Chunk.doc_id == doc_id, Chunk.page == page, Chunk.kind != "reference")
            .order_by(Chunk.idx)
        ).all()
    )
    rows = [c for c in rows if c.text and c.text.strip()]
    if not rows:
        return {"ok": False, "error": f"第 {page} 页没有可翻译的文本（可能是纯图表页）", "zh": ""}

    # 逐段落库翻译，而不是把整页拼成一坨塞给模型：
    # ① 一页常有 4000+ 字符，单次调用的输出预算装不下，末尾会被截；
    # ② 分段后与「对照」视图共用 translations 缓存，那边译过这边直接秒出；
    # ③ 并发分批由 translate_pages 负责，1 页 3 路并行约 5–8 秒。
    translate_pages(db, doc_id, page, page)
    zh_map = {
        t.chunk_id: t.zh
        for t in db.scalars(
            select(Translation).where(Translation.chunk_id.in_([c.id for c in rows]))
        ).all()
    }
    zh_list = [zh_map[c.id].strip() for c in rows if zh_map.get(c.id)]
    if not zh_list:
        return {"ok": False, "error": "翻译服务暂时不可用，请稍后重试", "zh": "", "page": page}
    return {
        "ok": True,
        "page": page,
        "segs": len(rows),
        "translated": len(zh_list),
        "missing": len(rows) - len(zh_list),
        "en": "\n\n".join(c.text.strip() for c in rows),
        "zh": "\n\n".join(zh_list),
    }


@router.get("/documents/{doc_id}/references")
def get_reference_entries(doc_id: str, db: Session = Depends(get_db)):
    """References 区逐条索引：编号 → 页码 + bbox + 原文。

    和 /refs 的区别：/refs 返回的是**引用边**（谁引了谁，供图谱用），
    这里返回的是**参考文献列表本身**，供「点正文 [11] 跳到第 11 条」使用。
    编号直接从条目文本开头的 [n] 解析——这是 LaTeX 排版的事实格式，
    比依赖 Citation 表的 ref_index 更可靠（后者在匹配失败时会缺号）。
    """
    import re as _re

    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    rows = list(
        db.scalars(
            select(Chunk)
            .where(Chunk.doc_id == doc_id, Chunk.kind == "reference")
            .order_by(Chunk.page, Chunk.idx)
        ).all()
    )
    pat = _re.compile(r"^\s*\[(\d{1,3})\]")
    entries: dict[int, dict] = {}
    for c in rows:
        m = pat.match(c.text or "")
        if not m:
            continue
        idx = int(m.group(1))
        # 同号重复时保留首条：正文角标指向的永远是第一次出现的那条
        if idx in entries:
            continue
        entries[idx] = {
            "index": idx,
            "page": c.page,
            # bbox 在库里是 "x0,y0,x1,y1" 字符串，必须在这里转成数组再返回：
            # 前端覆盖层按 [x0,y0,x1,y1] 解构，直接给字符串会逐字符解构，
            # 算出一个 1% 宽、0 高的畸形高亮框（实测踩到过）。
            "bbox": [float(v) for v in str(c.bbox).split(",")],
            "text": c.text,
            "chunk_id": c.id,
        }
    return {"total": len(entries), "entries": [entries[k] for k in sorted(entries)]}


# ---------------- References → 正文反向跳转（官方 1.3.2 第 2 点后半） ----------------
_RE_CITE_GROUP = re.compile(r"\[(\d{1,3}(?:\s*[,;\-–—]\s*\d{1,3})*)\]")


def _cite_indices(group: str) -> set[int]:
    """把 `1` / `1, 2` / `4-6` / `7–9` 展开成编号集合。

    学术论文这三种写法混着用，只认 `[n]` 会漏掉大半引用。
    含 0 的组整组丢弃：正文里的 `[0, 1]` 是数学区间，不是文献编号，
    否则查第 1 条会把所有区间都算成引用（实测论文里这种写法不少）。
    """
    out: set[int] = set()
    for part in re.split(r"\s*[,;]\s*", group):
        m = re.match(r"^(\d{1,3})\s*[-–—]\s*(\d{1,3})$", part.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b <= 300:
                out.update(range(a, b + 1))
            continue
        if part.strip().isdigit():
            out.add(int(part))
    return set() if 0 in out else out


_ABBR_TAIL = re.compile(r"(?:e\.g|i\.e|et al|fig|eq|tab|sec|ref|vs|no|cf|approx|resp)\.$", re.I)


def _hit_sentence(text: str, s: int, e: int) -> tuple[str, str]:
    """取 `[n]` 命中处的整句与一小段上下文。

    句子边界是"`. ? !` 后跟空白"，不能只找 `". "`：PDF 抽出的文本经常是
    `benchmarks.\\nMistral ...`，句号后面是换行，只认空格会把两段并成一句
    （实测返回的句子包含了上一句的 `[20]`，跳回去高亮就偏了）。
    常见缩写（e.g. / et al. / Fig.）的句点不算边界。
    """
    lo = 0
    for m in re.finditer(r"[.?!](?=\s)", text[:s]):
        if _ABBR_TAIL.search(text[: m.end()]):
            continue
        lo = m.end()
    hi = len(text)
    for m in re.finditer(r"[.?!](?=\s)", text[e:]):
        hi = e + m.end()
        break
    sent = " ".join(text[lo:hi].split())
    ctx = " ".join(text[max(0, s - 70) : min(len(text), e + 70)].split())
    return sent[:320], ctx[:220]


def _bbox_list(raw) -> list[float]:
    try:
        vals = [float(v) for v in str(raw).split(",")]
    except (TypeError, ValueError):
        return []
    return vals if len(vals) == 4 else []


@router.get("/documents/{doc_id}/references/{index}/cites")
def reference_cites(doc_id: str, index: int, limit: int = Query(12, ge=1, le=50),
                    db: Session = Depends(get_db)):
    """第 n 条参考文献被正文哪些地方引用（`[11]` → 跳回原文）。

    与「正文角标 → References」互为反方向；官方要求「还可以从 References
    快速跳回原来的位置」。同一段里多次引用只返回一条，用 `count` 记次数，
    否则一段里连引 3 次会在列表里刷出 3 行一模一样的条目。
    """
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    rows = db.scalars(
        select(Chunk)
        .where(Chunk.doc_id == doc_id, Chunk.kind != "reference")
        .order_by(Chunk.page, Chunk.idx)
    ).all()

    hits: list[dict] = []
    total_count = 0
    for c in rows:
        text = c.text or ""
        if not text or "[" not in text:
            continue
        first = None
        matched = 0
        for m in _RE_CITE_GROUP.finditer(text):
            if index in _cite_indices(m.group(1)):
                matched += 1
                if first is None:
                    first = m
        if first is None:
            continue
        total_count += matched
        sent, ctx = _hit_sentence(text, first.start(), first.end())
        hits.append({
            "page": c.page,
            "bbox": _bbox_list(c.bbox),
            "chunk_id": c.id,
            "section": c.section_path or "",
            "sentence": sent,
            "context": ctx,
            "count": matched,
        })
        if len(hits) >= limit:
            break

    return {"ok": True, "index": index, "total": len(hits),
            "occurrences": total_count, "hits": hits}


@router.get("/documents/{doc_id}/refs")
def get_references(doc_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    rows = list(
        db.scalars(
            select(Citation).where(Citation.src_doc_id == doc_id).order_by(Citation.ref_index)
        ).all()
    )
    return {
        "references": [
            {
                "index": r.ref_index,
                "text": r.raw_text,
                "matched_doc_id": r.dst_doc_id,
            }
            for r in rows
        ]
    }


# ---------------- 中英对照阅读 ----------------
@router.get("/documents/{doc_id}/bilingual")
def get_bilingual(
    doc_id: str,
    page: int = Query(1, ge=1),
    pages: int = Query(1, ge=1, le=8),
    translate: int = Query(1, ge=0, le=1),
    db: Session = Depends(get_db),
):
    """取 [page, page+pages) 区间的英中对照段落。

    默认 translate=1：缺译文的段落当场翻译后返回（一页约 1–2 次模型调用）。
    前端滚动时只预取下一页，所以响应时间可控在 2–3 秒内。
    """
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")

    from_page, to_page = page, min(page + pages - 1, max(doc.page_count, 1))
    segs = build_segments(db, doc_id, from_page, to_page)
    translated = 0
    if translate and any(not s["zh"] for s in segs):
        translated = translate_pages(db, doc_id, from_page, to_page)
        if translated:
            segs = build_segments(db, doc_id, from_page, to_page)

    return {
        "doc_id": doc_id,
        "page_from": from_page,
        "page_to": to_page,
        "page_count": doc.page_count,
        "translated": translated,
        "cached": cached_ratio(db, doc_id),
        "segments": segs,
    }


class BilingualTranslateIn(BaseModel):
    """整篇/按页翻译：这一个是"文档级"的，不要和下面"任意片段"那个混用。"""
    page_from: int = 1
    page_to: int = 1
    all: bool = False


@router.post("/documents/{doc_id}/bilingual/translate")
def post_translate(doc_id: str, body: BilingualTranslateIn, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")

    if body.all:
        return {"ok": True, "job": start_job(doc_id), "message": "整篇翻译已在后台开始"}

    n = translate_pages(db, doc_id, max(body.page_from, 1), min(body.page_to, doc.page_count or 1))
    return {"ok": True, "translated": n, "cached": cached_ratio(db, doc_id)}


@router.get("/documents/{doc_id}/bilingual/status")
def get_translate_status(doc_id: str, db: Session = Depends(get_db)):
    return {"job": job_status(doc_id), "cached": cached_ratio(db, doc_id)}


class TextTranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    to: str = ""   # "" = 按源语言自动判方向；"zh" / "en" 强制方向


@router.post("/translate")
def translate_arbitrary(body: TextTranslateIn):
    """划词 / 悬停翻译：任意片段即译即返，不落库。

    与整篇翻译分开做，是因为整篇走后台任务 + translations 表缓存（按页、可续传），
    而划词是"用户手指还按着"的交互，必须同步返回，且选区内容不值得缓存。

    `to` 留空即自动判方向：英→中（官方 1.3.3.1）、中→英（官方 1.6.4）共用这一条通道。
    """
    target = body.to if body.to in {"zh", "en"} else ""
    return translate_selection(body.text, target)


# ---------------- 整篇中译英（官方 1.6.4） ----------------
@router.post("/documents/{doc_id}/translate-en")
def start_translate_en(doc_id: str, db: Session = Depends(get_db)):
    """启动整篇中译英：后台线程跑，前端轮询进度。

    整篇中文论文动辄两三万字，同步接口一定超时；这里按 ~1200 字分批、3 路并发，
    产出 Markdown 落到 `data/exports/{doc_id}_en.md`，同时把正文留在内存供预览。
    """
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return start_en_job(db, doc_id)


@router.get("/documents/{doc_id}/translate-en")
def get_translate_en(doc_id: str, db: Session = Depends(get_db)):
    if not db.get(Document, doc_id):
        raise HTTPException(404, "文档不存在")
    return en_status(db, doc_id)


@router.get("/documents/{doc_id}/translate-en.md")
def download_translate_en(doc_id: str):
    """下载英文版 Markdown。走文件而不是重新生成：重复点不该再烧一次模型额度。"""
    path = Path(settings.db_path).parent / "exports" / f"{doc_id}_en.md"
    if not path.exists():
        raise HTTPException(404, "还没有生成英文版，请先点「译英」")
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{doc_id}_en.md"'},
    )


# ---------------- 整篇中文译本导出（官方 1.3.3.2） ----------------
@router.get("/documents/{doc_id}/translate-zh")
def get_translate_zh(doc_id: str, preview: int = Query(6000, ge=0, le=60000),
                     db: Session = Depends(get_db)):
    """中文译本预览：从已缓存的译文拼 Markdown，不调用模型。

    `preview` 控制返回多少字符，默认 6000——整本书的译本可能有几十万字符，
    一次性塞给浏览器没有意义，下载接口拿全文即可。
    """
    out = export_zh(db, doc_id)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "导出失败")
    md = out.get("markdown") or ""
    return {
        **{k: v for k, v in out.items() if k != "markdown"},
        "preview": md[:preview] if preview else "",
        "truncated": bool(preview) and len(md) > preview,
    }


@router.get("/documents/{doc_id}/translate-zh.md")
def download_translate_zh(doc_id: str, db: Session = Depends(get_db)):
    """下载中文译本 Markdown。

    每次请求都重新拼一遍：拼接是纯本地操作（毫秒级），而库里的译文随时在增加，
    落成固定文件反而会出现"昨天导出的比今天少"这种没人说得清的差异。
    """
    out = export_zh(db, doc_id)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "导出失败")
    if not out.get("translated"):
        raise HTTPException(400, "这篇还没有任何译文，请先在「对照」视图阅读或点「整篇翻译」")
    return Response(
        content=out["markdown"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{doc_id}_zh.md"'},
    )


# ---------------- 问答 ----------------
class ChatIn(BaseModel):
    question: str
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = "qa"
    # None=跟随后台全局设置；True=深思（开思维链）；False=快速
    thinking: bool | None = None


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db)):
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if not body.doc_ids:
        # 未指定文献 → 全库检索。这里保持 None 而不是铺开成 id 列表：
        # 检索层靠"没给范围"来判定能不能用分词/BM25 缓存，铺开之后每次都重算一遍，
        # 单次问答要多等 14 秒（实测 15.3s → 0.9s）。
        if not db.scalar(select(func.count()).select_from(Document)):
            return {
                "answer": "文献库还是空的。请先上传 PDF，或用「一键导入官方测试集」把 data/pdf 下的 39 篇论文入库。",
                "evidence": [],
                "verify": {},
            }
        body.doc_ids = None
    res = run_agent(body.question.strip(), body.doc_ids, mode=body.mode,
                    thinking=body.thinking)
    # 把"这次实际用的是哪种模式"回给前端，回答上方才标得出「深思 · 3.4s」
    res["meta"] = {"thinking": want_thinking(body.thinking), "model": settings.llm_model}
    # 句级定位：chunk 往往是一整段，直接把 bbox 画成大矩形既挡字也看不出重点，
    # 这里按问题与答案挑出真正作答的句子，前端据此画成贴行的小框。
    annotate_focus(res.get("evidence") or [], body.question.strip(), res.get("answer") or "")
    return res


@router.get("/tasks")
def list_tasks(limit: int = 20, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)).all())
    return {
        "tasks": [
            {
                "id": t.id,
                "goal": t.goal,
                "mode": t.mode,
                "status": t.status,
                "steps": t.steps_count,
                "latency_ms": t.latency_ms,
                "created_at": t.created_at.strftime("%m-%d %H:%M:%S"),
            }
            for t in rows
        ]
    }


# ---------------- 图谱 ----------------
@router.get("/graph")
def citation_graph(db: Session = Depends(get_db), refresh: bool = Query(False, description="true=跳缓存重算")):
    """引用图谱 + 图算法（PageRank / 介数中心性 / 社区发现），识别基石与桥接论文。"""
    return build_citation_graph(db, force=refresh)


class SurveyRequest(BaseModel):
    cluster_id: int | None = Field(None, description="只综述某个聚类；不传则综述全库")
    max_papers: int = Field(8, ge=3, le=20)
    focus: str = Field("", description="可选：希望侧重的角度，如'训练效率'")


SURVEY_PROMPT = """你是学术综述作者。下面是文献库中按引用影响力筛出的论文（含摘要）。

{papers}

请写一篇中文迷你综述，严格遵循：
1. 四个小节：**领域脉络**、**主要技术路线**、**关键分歧**、**开放问题**。
2. 每个结论句末尾必须标注来源编号，如 [1][3]；编号只能来自上面列表。
3. 不要编造列表之外的方法、数据或结论；列表没提的就不要写。
4. 总长 350-450 字，四节都要写完整并收尾，不要写"本文将综述……"这类套话，直接给内容。
{focus_line}"""


@router.post("/graph/survey")
def graph_survey(req: SurveyRequest, db: Session = Depends(get_db)):
    """基于图谱影响力排序，自动生成带引用的领域综述（P1 功能 8）。"""
    g = build_citation_graph(db)
    nodes = g.get("nodes") or []
    if not nodes:
        raise HTTPException(status_code=400, detail="文献库为空，先导入文献")

    pool = [n for n in nodes if req.cluster_id is None or n["cluster"] == req.cluster_id]
    if not pool:
        raise HTTPException(status_code=400, detail=f"聚类 {req.cluster_id} 不存在")

    picked = sorted(pool, key=lambda n: (n.get("in_degree", 0), n.get("pagerank", 0)), reverse=True)[
        : req.max_papers
    ]
    ids = [n["id"] for n in picked]
    docs = {d.id: d for d in db.scalars(select(Document).where(Document.id.in_(ids))).all()}

    lines = []
    for i, n in enumerate(picked, start=1):
        d = docs.get(n["id"])
        if not d:
            continue
        lines.append(
            f"[{i}] {d.title or d.filename}（{d.year or '年份未知'}，"
            f"被库内引用 {n.get('in_degree', 0)} 次）\n摘要：{(d.abstract or '无摘要')[:300]}"
        )
    papers_block = "\n\n".join(lines)

    focus_line = f"5. 侧重角度：{req.focus}\n" if req.focus else ""
    messages = [
        {"role": "system", "content": "你是严谨的学术综述作者，只依据给定材料写作。"},
        {"role": "user", "content": SURVEY_PROMPT.format(papers=papers_block, focus_line=focus_line)},
    ]

    # 推理模型会把预算先花在思维链上，2000 tokens 在大提示词下经常不够，
    # 表现为"接口 200 但正文为空"。这里按 4000 → 8000 递增重试。
    text = ""
    last_err = ""
    try:
        for budget in (8000, 12000):
            res = gateway.chat(messages, temperature=0.3, max_tokens=budget)
            text = (res.text or "").strip()
            if text:
                break
            last_err = f"模型返回空内容（预算 {budget} tokens 已被推理耗尽）"
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"综述生成失败：{exc}") from exc

    if not text:
        raise HTTPException(status_code=502, detail=f"综述生成失败：{last_err}，请调小 max_papers 后重试")

    return {
        "ok": True,
        "survey": text,
        "cluster_id": req.cluster_id,
        "papers": [
            {
                "index": i,
                "doc_id": n["id"],
                "title": (docs.get(n["id"]).title if docs.get(n["id"]) else n["label"]) or n["label"],
                "year": docs.get(n["id"]).year if docs.get(n["id"]) else n.get("year"),
                "in_degree": n.get("in_degree", 0),
                "pagerank": n.get("pagerank", 0),
            }
            for i, n in enumerate(picked, start=1)
        ],
        "stats": g.get("stats"),
    }


# ---------------- 批量导入 ----------------
@router.post("/library/rescan")
def rescan(background: BackgroundTasks, sync: bool = Query(False, description="true=同步执行，便于命令行调试")):
    """把 data/pdf 里的官方测试集批量入库。数据量大时走后台任务。"""
    if sync:
        return rescan_library()

    def job() -> None:
        try:
            stats = rescan_library()
            log.info("批量导入完成：%s", json.dumps(stats, ensure_ascii=False)[:500])
        except Exception:  # noqa: BLE001
            log.exception("批量导入失败")

    background.add_task(job)
    return {"ok": True, "message": "已在后台导入 data/pdf，进度见服务日志", "total": len(list(settings.pdf_dir.glob("*.pdf")))}


# ==================================================================== 回收站 / 审计（P0 数据安全）
@router.post("/documents/{doc_id}/restore")
def restore_doc(doc_id: str, actor: str = "ui", db: Session = Depends(get_db)):
    return restore_document(db, doc_id, actor=actor)


@router.get("/audit")
def list_audit(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all())
    return {"items": [
        {"action": r.action, "doc_id": r.doc_id, "detail": r.detail,
         "actor": r.actor, "created_at": r.created_at.isoformat()} for r in rows
    ]}


# ==================================================================== 外部工具链 / Function Calling
@router.get("/tools/health")
def tools_health():
    """列出工具与其真实可达性；不实际联网，避免健康检查被外网拖慢。"""
    return {"ok": True, "tools": [t["function"]["name"] for t in TOOL_SCHEMAS],
            "endpoints": {"arxiv": "export.arxiv.org", "github": "api.github.com",
                          "crossref": "api.crossref.org"},
            "policy": "重试 → 外部 API → 本地库降级（degraded 标记）"}


@router.get("/tools/catalog")
def tools_catalog():
    return {"tools": TOOL_SCHEMAS}


class ToolIn(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    simulate: str = ""       # QA 用：timeout / fail
    retries: int = 2
    timeout: float = 8.0


@router.post("/tools/invoke")
def tools_invoke(body: ToolIn):
    # 直接返回 ToolResult：它自带 ok/degraded/source/attempts。
    # 早期写成 {"ok": True, **result} 会被 result.ok 覆盖，反而把
    # "降级成功"错误地暴露成 HTTP 层 ok=True / 工具层 ok=False 的歧义结构。
    return tool_invoke(body.tool, body.args, simulate=body.simulate,
                       retries=body.retries, timeout=body.timeout)


class ToolAgentIn(BaseModel):
    question: str


@router.post("/tools/agent")
def tools_agent(body: ToolAgentIn):
    """让 LLM 按 OpenAI tools 协议决定调哪个工具，再真实执行。"""
    return {"ok": True, **tool_decide(body.question)}


# ==================================================================== CSV → 学术图表
class ChartIn(BaseModel):
    csv: str = Field(max_length=200_000)
    format: str = "mermaid"
    title: str = Field(default="实验结果", max_length=120)
    kind: str = "bar"           # bar / line / radar / table
    caption: bool = True        # 是否顺带生成 Figure Caption


@router.post("/tools/csv2chart")
def csv2chart(body: ChartIn):
    """CSV → 表格 / 柱状 / 折线 / 雷达的代码（Mermaid·TikZ·Matplotlib·LaTeX）+ 图注（官方 1.6.3）。"""
    try:
        return {"ok": True, **convert_chart(
            body.csv, body.format, body.title, kind=body.kind, caption=body.caption)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ==================================================================== 科研增强
@router.get("/research/future-work")
def future_work(limit: int = Query(60, ge=1, le=200), db: Session = Depends(get_db)):
    return collect_future_work(db, max_items=limit)


@router.get("/research/reproducibility")
def reproducibility(max_docs: int = Query(8, ge=1, le=20), online: bool = True,
                    db: Session = Depends(get_db)):
    return collect_reproducibility(db, max_docs=max_docs, online=online)


class WriteIn(BaseModel):
    idea: str = Field(min_length=2, max_length=1200)
    doc_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(6, ge=1, le=10)


@router.post("/write/outline")
def write_outline(body: WriteIn, db: Session = Depends(get_db)):
    """写作 Copilot：idea → 论文框架 + 只来自库内真实文献的 References（官方 1.6.1）。"""
    return generate_outline(db, body.idea, body.doc_ids or None, top_k=body.top_k)


class TopologyIn(BaseModel):
    idea: str = Field(min_length=2, max_length=1200)
    outline: str = Field(default="", max_length=6000)


@router.post("/write/topology")
def write_topology(body: TopologyIn):
    """idea（或已有大纲）→ 架构拓扑图，一次产出 Mermaid / DOT / TikZ / Matplotlib（官方 1.6.2）。"""
    return generate_topology(body.idea, body.outline)


# ==================================================================== Token 级流式 + 思考过程
def _intent_of(q: str, mode: str) -> str:
    """粗略判断问题类型，只用于「规划」那一步的展示。

    不追求分类准确——它的作用是把"我按什么思路在检索"说清楚，
    比一句固定的"任务规划：拆解问题与检索目标"更有信息量，也经得起追问。
    """
    if mode == "compare":
        return "跨文献对比"
    if re.search(r"多少|几[个块倍张层]|BLEU|参数量|FLOPs|占比|比例|分数|准确率|复杂度|大小", q):
        return "数值抽取"
    if re.search(r"区别|不同|关系|为什么|原理|机制|怎么实现|如何", q):
        return "概念辨析"
    if re.search(r"第\s*\d+\s*作者|是谁|哪个机构|哪一年|哪里", q):
        return "事实定位"
    return "资料检索"


class StreamIn(BaseModel):
    question: str
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = "qa"
    thinking: bool | None = None


@router.post("/chat/stream")
def chat_stream(body: StreamIn, db: Session = Depends(get_db)):
    """SSE：先推真实的检索/校验思考事件，再逐 token 推答案。

    与 /api/chat 不冲突：这里不套 JSON 包裹，模型每产出一个 delta 就发一个
    token 事件；结束后用同一套 attach_anchors + verify 做引用校验，
    把 evidence / verify 放在 done 事件里（流式也要能溯源）。
    """
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    # None = 全库（与上面的 /chat 同理：给 None 才能命中分词缓存）
    doc_ids = body.doc_ids or None
    scope_n = len(body.doc_ids) if body.doc_ids else (
        db.scalar(select(func.count()).select_from(Document)) or 0)

    def gen():
        def emit(event: str, **data) -> str:
            return "data: " + json.dumps({"event": event, **data}, ensure_ascii=False) + "\n\n"

        # 快速=trans_model（非推理模型，最快）；深思=主模型 + 开思维链。
        # 这个接口原本固定用 trans_model，等于流式和非流式跑的不是一个模型；
        # 加开关后两条路径才对得上。
        deep = want_thinking(body.thinking)
        model = settings.llm_model if deep else settings.trans_model
        reason_chars = 0
        reason_buf: list[str] = []
        reason_sent = 0

        try:
            # 每一步都带**这一步真实发生了什么的数字**，而不是一句固定的流程说明：
            # 固定文案看两遍就会被认出是装饰，带命中数/耗时/字数才像真的在干活。
            yield emit("thinking", stage="plan", state="done",
                       text=f"识别意图：{_intent_of(q, body.mode)} · 检索范围 {scope_n} 篇文献",
                       scope=scope_n)
            yield emit("thinking", stage="retrieve", state="run",
                       text="检索中：BM25 + 稠密向量 + RRF 融合 → 重排")
            t = time.time()
            hits = search_with_rerank(q, doc_ids, top_k=8)
            evidence = [h.to_dict() for h in hits]
            ms = int((time.time() - t) * 1000)
            doc_names = {e.get("doc_name") for e in evidence}
            pages = {(e.get("doc_name"), e.get("page")) for e in evidence}
            yield emit("thinking", stage="retrieve", state="done",
                       text=f"命中 {len(evidence)} 条证据 · 覆盖 {len(doc_names)} 篇 / {len(pages)} 页 · {ms} ms",
                       hits=len(evidence), docs=len(doc_names), pages=len(pages), latency_ms=ms)
            if not evidence:
                yield emit("error", text="文献库中没有检索到相关内容")
                yield emit("done", answer="没有检索到相关证据。", evidence=[], verify={})
                return

            block = "\n".join(
                f"[{i}] 来源：{e['doc_name']} 第{e['page']}页 | {e['text'][:900]}"
                for i, e in enumerate(evidence, start=1)
            )
            chars = sum(len(e.get("text") or "") for e in evidence)
            yield emit("thinking", stage="read", state="done",
                       text=f"通读 {len(evidence)} 条证据（{chars:,} 字），按句定位可引用原句",
                       chars=chars)
            msgs = [
                {"role": "system", "content": "你是严谨的科研写作助手。只依据给定证据回答，"
                                              "每个论断后带 [编号]；数字原样抄写；"
                                              "找不到就直说文献未提及。不要 JSON、不要前言。"},
                {"role": "user", "content": f"【证据】\n{block}\n\n【问题】\n{q}\n\n"
                                            f"要求：第一句给结论，200 字以内，正文带 [编号] 引用。"},
            ]
            if deep:
                yield emit("thinking", stage="depth", state="run",
                           text=f"深思模式：{model} 先推理再作答，推理内容实时可见")
            buf: list[str] = []
            t_reason = time.time()
            for kind, piece in gateway.chat_stream(msgs, model=model, thinking=body.thinking):
                if kind == "reason":
                    reason_chars += len(piece)
                    # 推理原文分段推给前端：一次 120 字左右，既看得到"在想什么"，
                    # 又不会把每一两个字符都当成一次网络往返。总量封顶 6000 字，
                    # 超长推理只记账不再推（避免前端渲染拖垮流）。
                    reason_buf.append(piece)
                    if sum(len(x) for x in reason_buf) >= 120:
                        chunk = "".join(reason_buf)
                        reason_buf = []
                        if reason_sent < 6000:
                            reason_sent += len(chunk)
                            yield emit("reason", text=chunk, chars=reason_chars)
                    continue
                if reason_buf:
                    if reason_sent < 6000:
                        chunk = "".join(reason_buf)
                        reason_sent += len(chunk)
                        yield emit("reason", text=chunk, chars=reason_chars)
                    reason_buf = []
                if reason_chars and not buf:
                    yield emit("thinking", stage="depth_done", state="done",
                               text=f"推理完成（{reason_chars} 字），开始作答",
                               latency_ms=int((time.time() - t_reason) * 1000))
                buf.append(piece)
                yield emit("token", text=piece)
            answer = "".join(buf).strip()
            rendered = attach_anchors(answer, evidence)
            annotate_focus(evidence, q, answer)
            v = verify_answer(rendered, evidence, q)
            yield emit("thinking", stage="verify", state="done",
                       text=(f"引用校验 {v.get('valid')}/{v.get('total')} 命中"
                             + (f" · {v.get('invalid')} 处未核实" if v.get("invalid") else "")),
                       rate=v.get("rate"))
            yield emit("done", answer=rendered, raw_answer=answer,
                       evidence=evidence, verify=v,
                       meta={"thinking": deep, "model": model,
                             "reasoning_chars": reason_chars})
        except Exception as exc:  # noqa: BLE001 流内异常也要给前端可渲染的事件，不能断流
            log.exception("流式回答失败")
            yield emit("error", text=f"生成中断：{type(exc).__name__}: {str(exc)[:160]}")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------- 1.4 论文自动下载与推荐
class DiscoverImportIn(BaseModel):
    arxiv_id: str = ""
    title: str = ""
    force: bool = False


class DiscoverRefImportIn(BaseModel):
    arxiv_ids: list[str] = Field(default_factory=list)
    titles: dict[str, str] = Field(default_factory=dict)


class DiscoverFeedbackIn(BaseModel):
    arxiv_id: str = ""
    action: str = "like"
    reason: str = ""


def _mark_in_library(db: Session, items: list[dict]) -> list[dict]:
    """标出「库里已有」：用户最怕的是点了下载才发现重复。

    必须走 library_index 归一化：arXiv 返回 `1706.03762`，库里存 `1706.03762v1`，
    直接比字符串会得出"库里没有"的错误结论（实测就是这样漏标的）。
    """
    idx = discover_mod.library_index(db)
    for it in items:
        d = idx.get(discover_mod._base_id(it.get("arxiv_id") or ""))
        it["in_library"] = bool(d)
        it["doc_id"] = d.id if d else ""
    return items


@router.get("/discover/search")
def discover_search(q: str = Query(..., min_length=2), max: int = Query(8, ge=1, le=20),
                    db: Session = Depends(get_db)):
    """按标题/关键词在 arXiv 检索候选论文。

    纯标题走 `ti:`，多词短语走 `all:"..."`（词组匹配），都不中再退化到 `all:词`。
    检索失败时如实返回空 + error，不拿本地库冒充 arXiv 结果。
    """
    t0 = time.time()
    raw = re.sub(r"[^A-Za-z0-9\s\-]", " ", " ".join((q or "").split()))[:160].strip()
    if not raw:
        raise HTTPException(400, "查询词需要包含英文标题或关键词")
    items = discover_mod.search_arxiv(f'all:"{raw}"', max)
    if not items:
        items = discover_mod.search_arxiv(f"ti:{raw}", max)
    if not items:
        items = discover_mod.search_arxiv(f"all:{raw}", max)
    _mark_in_library(db, items)
    return {"ok": True, "q": q, "items": items, "source": "arxiv.org",
            "ms": int((time.time() - t0) * 1000)}


@router.post("/discover/import")
def discover_import(body: DiscoverImportIn, db: Session = Depends(get_db)):
    """下载一篇并走完整入库（解析 → 去重 → 切块 → 向量化 → 章节树 → 引用图）。"""
    return discover_mod.import_paper(db, body.arxiv_id, body.title, force=body.force)


@router.get("/discover/references")
def discover_references(doc_id: str = Query(...), limit: int = Query(8, ge=1, le=15),
                        min_cites: int = Query(2, ge=0), db: Session = Depends(get_db)):
    """库内论文的参考文献候选，按正文被引次数排序（被引多的才值得补）。"""
    return discover_mod.reference_candidates(db, doc_id, limit=limit, min_cites=min_cites)


@router.post("/discover/references/import")
def discover_import_refs(body: DiscoverRefImportIn, background: BackgroundTasks,
                         db: Session = Depends(get_db)):
    """批量补全参考文献：分钟级操作，立刻返回 job_id，前端轮询进度。"""
    ids = [a for a in body.arxiv_ids if a]
    if not ids:
        raise HTTPException(400, "没有选择要下载的条目")
    if len(ids) > 20:
        raise HTTPException(400, "一次最多补全 20 篇，请分批")
    job_id = discover_mod.create_job(ids, body.titles)
    background.add_task(discover_mod.run_job, job_id)
    return {"ok": True, "job_id": job_id, "total": len(ids)}


@router.get("/discover/jobs/{job_id}")
def discover_job(job_id: str):
    st = discover_mod.job_status(job_id)
    # 注意不要写 {"ok": ..., **st}：st 里的 ok 是"成功篇数"，展开后会覆盖掉布尔值，
    # 前端拿到 ok:0 会以为任务失败（实测踩到）。用 found 表达"任务是否存在"。
    return {"found": st.get("state") != "missing", **st}


@router.get("/discover/recommend")
def discover_recommend(limit: int = Query(6, ge=1, le=12), days: int = Query(180, ge=7, le=3650),
                       doc_id: str = Query("", description="以某篇文献为种子（可选）"),
                       db: Session = Depends(get_db)):
    """按库内兴趣找近期 arXiv 新论文，附中文推荐理由与 GitHub 代码标记。"""
    return discover_mod.recommend(db, limit=limit, days=days, doc_id=doc_id)


@router.post("/discover/feedback")
def discover_feedback(body: DiscoverFeedbackIn, db: Session = Depends(get_db)):
    """赞 / 踩 / 点开：下一次推荐里 dislike 直接过滤、like 参与加权。"""
    return discover_mod.feedback(db, body.arxiv_id, body.action, body.reason)


@router.get("/discover/feedback")
def discover_feedback_stats(db: Session = Depends(get_db)):
    return discover_mod.feedback_stats(db)


@router.get("/discover/watch")
def discover_watch_status():
    """论文定期追踪的状态 + 最近一次扫出来的新论文（官方 1.4.2）。

    返回的是"上次扫描的结果快照"，不等同于实时推荐：实时推荐走
    `/discover/recommend`（会现场查 arXiv，慢）。这里是给界面挂在
    「发现」页上做背景提示用的，打开页面不该触发一次新的外网请求。
    """
    from app.research import watcher

    return watcher.status()


@router.post("/discover/watch/run")
def discover_watch_run():
    """立刻扫一次。带 60 秒冷却，避免连点把 arXiv 与模型都刷爆。"""
    from app.research import watcher

    return watcher.run_once(manual=True)
