"""SciAgent 后端入口。

启动顺序：建目录 → 初始化日志 → 建表 → 挂路由 → 托管前端静态文件。
访问 / 直接进 Web 界面，/docs 是自动生成的 API 文档（答辩时可现场演示）。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.admin import router as admin_router
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
app.include_router(admin_router)

WEB_DIR = BASE_DIR / "web"


@app.on_event("startup")
async def _startup() -> None:
    settings.ensure_dirs()
    init_db()
    # 后台改过的参数存在 app_settings 表里，启动时回灌——否则重启一次全部失效，
    # 而界面上还显示"已保存"，是最容易踩的坑
    try:
        from app.core.params import load_overrides
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            load_overrides(db)
    except Exception:  # noqa: BLE001 参数加载失败不能挡住服务启动
        log.warning("后台参数回灌失败，本次以 .env / 默认值启动", exc_info=True)
    store = get_store()
    info = {
        "llm_route": "见 /api/system/info",
        "vector_backend": store.name,
        "data_dir": str(settings.data_path),
        "official_pdfs": len(list(settings.pdf_dir.glob("*.pdf"))),
    }
    log.info("SciAgent 启动完成：%s", info)

    # 五分钟心跳：本地模型默认 5 分钟没请求就被 Ollama 卸出显存，下一次请求要重新
    # 加载（3B 约 1.5–4 秒），演示第一句会莫名卡住。开关与间隔在后台「服务与安全」组
    try:
        from app.core import keepalive

        keepalive.start()
    except Exception:  # noqa: BLE001 保活起不来不能挡住服务
        log.warning("保活循环启动失败", exc_info=True)


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        from app.core import keepalive

        await keepalive.stop()
    except Exception:  # noqa: BLE001
        pass


@app.get("/health")
def health():
    return {"ok": True, "service": "sciagent", "version": app.version}


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return {"message": "前端未构建，请访问 /docs 使用 API"}


@app.get("/admin")
def admin_page():
    """后台控制台。独立单页，不依赖主界面的构建链。"""
    page = WEB_DIR / "admin.html"
    if page.exists():
        return FileResponse(page)
    return {"message": "后台页面未找到"}


# 静态资源（app.js / style.css / admin.js 等）
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)
