"""HTTP 接口层：文档、检索问答、图谱、系统自检。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.graph import run as run_agent
from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger
from app.db.models import Chunk, Citation, Document, Lesson, Section, Task
from app.db.session import get_db
from app.parse.ocr import ocr_available
from app.rag.citation_graph import build_graph as build_citation_graph
from app.rag.citation_graph import invalidate as invalidate_graph
from app.parse.structure import render_page_png
from app.pipeline.ingest import delete_document, ingest, rescan_library, save_upload
from app.rag.store import get_store

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
def remove_document(doc_id: str, db: Session = Depends(get_db)):
    if not delete_document(db, doc_id):
        raise HTTPException(404, "文档不存在")
    return {"ok": True}


@router.get("/documents/{doc_id}/sections")
def get_sections(doc_id: str, db: Session = Depends(get_db)):
    rows = list(
        db.scalars(select(Section).where(Section.doc_id == doc_id).order_by(Section.order_idx)).all()
    )
    return {"sections": [{"level": s.level, "title": s.title, "page": s.page} for s in rows]}


@router.get("/documents/{doc_id}/chunks")
def get_chunks(doc_id: str, page: int | None = None, db: Session = Depends(get_db)):
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
