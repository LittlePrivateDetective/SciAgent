"""SciAgent 后端入口：解析 / 检索 / 溯源问答 的最小闭环。"""
from __future__ import annotations

import os
import tempfile

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from app.parse_pdf import merge_blocks, parse_pdf

app = FastAPI(title="SciAgent", version="0.1.0")

STORE: dict[str, list[dict]] = {}


@app.get("/health")
def health():
    return {"ok": True, "docs": len(STORE), "model": os.getenv("LLM_MODEL", "unset")}


@app.post("/parse")
async def parse(file: UploadFile = File(...)):
    """上传 PDF → 返回带页码与 bbox 的检索单元。"""
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "only pdf"}, status_code=400)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as fh:
        fh.write(await file.read())
        tmp = fh.name

    try:
        chunks = merge_blocks(parse_pdf(tmp))
    finally:
        os.unlink(tmp)

    doc_id = file.filename
    STORE[doc_id] = chunks
    return {
        "doc_id": doc_id,
        "chunks": len(chunks),
        "preview": chunks[:3],
    }


@app.get("/docs/{doc_id}/chunks")
def get_chunks(doc_id: str):
    return STORE.get(doc_id, [])
