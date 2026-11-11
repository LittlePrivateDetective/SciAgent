"""HTTP 接口层：文档、检索问答、图谱、系统自检。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.graph import run as run_agent
from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger
from app.db.models import AuditLog, Chunk, Citation, Document, Lesson, Section, Task, TrashEntry
from app.db.session import get_db
from app.parse.ocr import ocr_available
from app.rag.bilingual import (
    build_segments,
    cached_ratio,
    job_status,
    start_job,
    translate_pages,
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
from app.tools.chart import convert as convert_chart
from app.tools.external import TOOL_SCHEMAS
from app.tools.external import decide as tool_decide
from app.tools.external import invoke as tool_invoke

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
        "official_pdfs": len(list(settings.pdf_dir.glob("*.pdf"))),
    }


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
    if not doc or not Path(doc.file_path).exists():
        raise HTTPException(404, "文档或源文件不存在")
    try:
        png = render_page_png(doc.file_path, page, dpi=dpi)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"渲染失败：{exc}") from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/documents/{doc_id}/pages")
def get_page_meta(doc_id: str, db: Session = Depends(get_db)):
    """返回每页的原始尺寸（pt），前端据此把 bbox 换算成百分比，做高亮覆盖层。"""
    import pymupdf

    doc = db.get(Document, doc_id)
    if not doc or not Path(doc.file_path).exists():
        raise HTTPException(404, "文档不存在")
    out = []
    with pymupdf.open(doc.file_path) as pdf:
        for i, page in enumerate(pdf, start=1):
            out.append({"page": i, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2)})
    return {"pages": out}


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


class TranslateIn(BaseModel):
    page_from: int = 1
    page_to: int = 1
    all: bool = False


@router.post("/documents/{doc_id}/bilingual/translate")
def post_translate(doc_id: str, body: TranslateIn, db: Session = Depends(get_db)):
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


# ---------------- 问答 ----------------
class ChatIn(BaseModel):
    question: str
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = "qa"


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db)):
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if not body.doc_ids:
        # 未指定文献 → 全库检索
        body.doc_ids = [d.id for d in db.scalars(select(Document)).all()]
    if not body.doc_ids:
        return {
            "answer": "文献库还是空的。请先上传 PDF，或用「一键导入官方测试集」把 data/pdf 下的 39 篇论文入库。",
            "evidence": [],
            "verify": {},
        }
    return run_agent(body.question.strip(), body.doc_ids, mode=body.mode)


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
    csv: str
    format: str = "mermaid"
    title: str = "实验结果"


@router.post("/tools/csv2chart")
def csv2chart(body: ChartIn):
    try:
        return {"ok": True, **convert_chart(body.csv, body.format, body.title)}
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


# ==================================================================== Token 级流式 + 思考过程
class StreamIn(BaseModel):
    question: str
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = "qa"


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
    doc_ids = body.doc_ids or [d.id for d in db.scalars(select(Document)).all()]

    def gen():
        def emit(event: str, **data) -> str:
            return "data: " + json.dumps({"event": event, **data}, ensure_ascii=False) + "\n\n"

        try:
            yield emit("thinking", stage="plan", text="任务规划：拆解问题与检索目标")
            t = time.time()
            hits = search_with_rerank(q, doc_ids, top_k=8)
            evidence = [h.to_dict() for h in hits]
            yield emit("thinking", stage="retrieve",
                       text=f"BM25 + 稠密向量 + RRF + 重排，命中 {len(evidence)} 条证据",
                       hits=len(evidence), latency_ms=int((time.time() - t) * 1000))
            if not evidence:
                yield emit("error", text="文献库中没有检索到相关内容")
                yield emit("done", answer="没有检索到相关证据。", evidence=[], verify={})
                return

            block = "\n".join(
                f"[{i}] 来源：{e['doc_name']} 第{e['page']}页 | {e['text'][:900]}"
                for i, e in enumerate(evidence, start=1)
            )
            yield emit("thinking", stage="read", text="通读证据，定位可引用的原句")
            msgs = [
                {"role": "system", "content": "你是严谨的科研写作助手。只依据给定证据回答，"
                                              "每个论断后带 [编号]；数字原样抄写；"
                                              "找不到就直说文献未提及。不要 JSON、不要前言。"},
                {"role": "user", "content": f"【证据】\n{block}\n\n【问题】\n{q}\n\n"
                                            f"要求：第一句给结论，200 字以内，正文带 [编号] 引用。"},
            ]
            buf: list[str] = []
            for piece in gateway.chat_stream(msgs, model=settings.trans_model):
                buf.append(piece)
                yield emit("token", text=piece)
            answer = "".join(buf).strip()
            rendered = attach_anchors(answer, evidence)
            v = verify_answer(rendered, evidence, q)
            yield emit("thinking", stage="verify",
                       text=f"引用校验 {v.get('valid')}/{v.get('total')} 命中",
                       rate=v.get("rate"))
            yield emit("done", answer=rendered, raw_answer=answer,
                       evidence=evidence, verify=v)
        except Exception as exc:  # noqa: BLE001 流内异常也要给前端可渲染的事件，不能断流
            log.exception("流式回答失败")
            yield emit("error", text=f"生成中断：{type(exc).__name__}: {str(exc)[:160]}")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
