"""SciAgent 后端入口。

启动顺序：建目录 → 初始化日志 → 建表 → 挂路由 → 托管前端静态文件。
访问 / 直接进 Web 界面，/docs 是自动生成的 API 文档（答辩时可现场演示）。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import BASE_DIR, settings
from app.core.logger import get_logger, setup_logging
from app.db.session import init_db
from app.rag.store import get_store

setup_logging()
log = get_logger(__name__)

app = FastAPI(
    title="SciAgent · 科研智能体",
    description="双栏感知 PDF 解析 + 锚点可验证 RAG + 多 Agent 编排",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

WEB_DIR = BASE_DIR / "web"


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    init_db()
    store = get_store()
    info = {
        "llm_route": "见 /api/system/info",
        "vector_backend": store.name,
        "data_dir": str(settings.data_path),
        "official_pdfs": len(list(settings.pdf_dir.glob("*.pdf"))),
    }
    log.info("SciAgent 启动完成：%s", info)


@app.get("/health")
def health():
    return {"ok": True, "service": "sciagent", "version": app.version}


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return {"message": "前端未构建，请访问 /docs 使用 API"}


# 静态资源（app.js / style.css / vendor）
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)
