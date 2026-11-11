"""后台控制台接口：参数中心 + 运行时概览 + 模型连通性自测 + 配置导入导出。

为什么参数不做成"改 .env 然后重启"：演示/答辩现场没法重启，而且不知道改了哪项
会让检索变差。这里每一项都带生效方式，改完能立刻在「自测」里验证。
"""

from __future__ import annotations

import json
import os
import platform
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import auth
from app.core import params as P
from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger
from app.db.models import AppSetting, AuditLog, Chunk, Document, Task
from app.db.session import get_db
from app.rag.store import get_store

log = get_logger(__name__)
def _keepalive_status() -> dict:
    """后台保活状态。延迟导入：避免 core 层反向依赖 api 层。"""
    from app.core import keepalive

    return keepalive.status()


router = APIRouter(prefix="/api/admin", tags=["admin"])

BOOT_AT = time.time()


class ParamsIn(BaseModel):
    values: dict = Field(default_factory=dict)
    actor: str = "admin"


class ResetIn(BaseModel):
    keys: list[str] = Field(default_factory=list)
    actor: str = "admin"


class TestModelIn(BaseModel):
    prompt: str = "用一句话说明什么是检索增强生成。"
    # 默认给足 512：本项目主模型带思维链，64 token 会被推理过程吃干净，
    # 返回空内容——那不是"模型不可用"，是预算不够，默认值不该制造假故障。
    max_tokens: int = Field(512, ge=8, le=2048)
    model: str = ""


# ---------------------------------------------------------------- 鉴权
class LoginIn(BaseModel):
    password: str = ""


@router.get("/session")
def session_state(request: Request):
    """前端启动时问一句：要不要登录、现在登没登。

    `enabled=false` 表示服务端没配 ADMIN_PASSWORD（本机开发），
    界面会明确写"未启用口令"，而不是假装已经安全了。
    """
    return {
        "ok": True,
        "enabled": auth.enabled(),
        "authenticated": (not auth.enabled()) or auth.valid(auth.token_of(request)),
    }


@router.post("/login")
def do_login(body: LoginIn, request: Request, response: Response):
    token, ttl = auth.login(request, body.password)
    response.set_cookie(
        auth.COOKIE, token, max_age=ttl, httponly=True, samesite="lax", path="/",
    )
    return {"ok": True, "expires_in": ttl}


@router.post("/logout")
def do_logout(request: Request, response: Response):
    auth.logout(auth.token_of(request))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/params")
def get_params(_=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """全部参数：定义 + 当前值 + 来源（后台 / .env / 默认）。密钥只回掩码。"""
    return {"ok": True, **P.describe(db)}


@router.put("/params")
def put_params(body: ParamsIn, _=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """保存参数。先全部校验，再统一落库；任何一项不合法整批拒绝，不留中间态。"""
    res = P.save(db, body.values, actor=body.actor or "admin")
    if not res["ok"]:
        return {"ok": False, "rejected": res["rejected"], "applied": [],
                "message": "有参数未通过校验，本次未保存任何一项"}
    if res["applied"]:
        keys = ", ".join(a["key"] for a in res["applied"])
        db.add(AuditLog(action="params_update", detail=f"更新 {keys}"[:900], actor=body.actor))
        db.commit()
    restart = [a["key"] for a in res["applied"] if a["scope"] in {"restart", "reindex"}]
    return {
        "ok": True, "applied": res["applied"], "rejected": [],
        "need_restart": [k for k in restart if P.BY_KEY[k].scope == "restart"],
        "need_reindex": [k for k in restart if P.BY_KEY[k].scope == "reindex"],
        **P.describe(db),
    }


@router.post("/params/reset")
def reset_params(body: ResetIn, _=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """恢复默认值（不删 .env，只清掉后台覆盖）。"""
    res = P.reset(db, body.keys or None)
    db.add(AuditLog(action="params_reset", actor=body.actor,
                    detail=("恢复默认：" + ",".join(res["reset"]))[:900]))
    db.commit()
    return {"ok": True, **res, **P.describe(db)}


@router.get("/overview")
def overview(_=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """运行时概览：语料规模、存储占用、模型与向量库状态、作业与审计。"""
    def dir_size(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    counts = {
        "documents": db.scalar(select(func.count(Document.id))) or 0,
        "chunks": db.scalar(select(func.count(Chunk.id))) or 0,
        "tasks": db.scalar(select(func.count(Task.id))) or 0,
        "overrides": db.scalar(select(func.count(AppSetting.key))) or 0,
    }
    char_sum = db.scalar(select(func.coalesce(func.sum(Chunk.char_len), 0))) or 0

    store = get_store()
    ollama = {"enabled": bool(settings.ollama_enabled), "url": settings.ollama_base_url,
              "model": settings.ollama_model, "reachable": None, "latency_ms": None}
    if settings.ollama_enabled:
        t0 = time.time()
        try:
            with httpx.Client(timeout=2.5) as c:
                r = c.get(settings.ollama_base_url.rstrip("/") + "/api/tags")
            ollama["reachable"] = r.status_code == 200
            ollama["latency_ms"] = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                ollama["models"] = [m.get("name") for m in (r.json().get("models") or [])][:12]
        except Exception as exc:  # noqa: BLE001 探测失败只如实标注，不抛给前端
            ollama["reachable"] = False
            ollama["error"] = str(exc)[:120]

    db_path = settings.db_path
    return {
        "ok": True,
        "counts": counts,
        "corpus": {"chars": int(char_sum),
                   "avg_chars_per_chunk": round(char_sum / counts["chunks"], 1) if counts["chunks"] else 0},
        "storage": {
            "db_mb": round(db_path.stat().st_size / 1048576, 1) if db_path.exists() else 0,
            "pdf_mb": round(dir_size(settings.pdf_dir) / 1048576, 1),
            "chroma_mb": round(dir_size(settings.chroma_path) / 1048576, 1),
            "data_dir": str(settings.data_path),
        },
        "llm": gateway.describe(),
        "ollama": ollama,
        "vector": {"backend": store.name, "chroma_dir": str(settings.chroma_path)},
        "embedding": {"provider": settings.emb_provider, "model": settings.emb_model,
                      "dim": settings.emb_dim},
        "rerank": {"provider": settings.rerank_provider, "model": settings.rerank_model,
                   "url_set": bool(settings.rerank_url)},
        "runtime": {
            "uptime_sec": int(time.time() - BOOT_AT),
            "started_at": datetime.fromtimestamp(BOOT_AT).strftime("%Y-%m-%d %H:%M:%S"),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "pid": os.getpid(),
            "rss_mb": _rss_mb(),
        },
        "keepalive": _keepalive_status(),
        "audit": [
            {"action": a.action, "detail": a.detail, "actor": a.actor,
             "at": a.created_at.strftime("%m-%d %H:%M:%S")}
            for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8)).all()
        ],
    }


def _rss_mb() -> float | None:
    """进程常驻内存。优先 psutil；没有就退回 Windows 任务列表 / /proc。"""
    try:
        import psutil  # type: ignore

        return round(psutil.Process(os.getpid()).memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        import resource  # type: ignore  # 仅 Unix

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


@router.post("/test-model")
def test_model(body: TestModelIn, _=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """真实打一次模型，回报耗时与首段输出——参数改完能立刻自证有没有生效。

    不使用假成功：mock 模式如实标注 route=mock，前端会提示"这是模拟回复"。
    """
    msgs = [
        {"role": "system", "content": "你是严谨的技术助手，回答不超过 40 字。"},
        {"role": "user", "content": body.prompt},
    ]
    t0 = time.time()
    try:
        res = gateway.chat(msgs, max_tokens=body.max_tokens, temperature=0.2,
                           model=body.model or None)
    except Exception as exc:  # noqa: BLE001 失败要给前端可读原因
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "route": gateway.route(), "ms": int((time.time() - t0) * 1000)}
    text = (res.text or "").strip()
    usage = res.usage
    out = {
        "ok": bool(text),
        "route": gateway.route(),
        "model": res.model,
        "ms": int((time.time() - t0) * 1000),
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "preview": text[:300],
        "mock": gateway.route() == "mock",
        "settings": {"model": settings.llm_model, "base_url": settings.llm_base_url,
                     "temperature": settings.llm_temperature,
                     "max_tokens": settings.llm_max_tokens, "timeout": settings.llm_timeout},
    }
    if not text:
        # 空回复最常见的两个原因：预算被思维链吃光、或模型名/网关对不上。
        # 这里把 token 用量摊开说，用户不用去翻日志猜。
        out["error"] = (
            f"模型返回空内容（prompt {usage.prompt_tokens} / completion {usage.completion_tokens} token）。"
            "主模型若带思维链，max_tokens 太小会把预算全用在推理上——把上限调大，"
            "或改用非推理模型（如 qwen-flash）。"
        )
    return out


@router.get("/export")
def export_config(_=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """导出后台覆盖项（不含密钥明文，密钥只导掩码占位）。"""
    rows = db.scalars(select(AppSetting)).all()
    data = {}
    for r in rows:
        if P.BY_KEY.get(r.key) and P.BY_KEY[r.key].kind == "secret":
            continue
        try:
            data[r.key] = json.loads(r.value)
        except json.JSONDecodeError:
            data[r.key] = r.value
    return {
        "ok": True,
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "values": data,
        "note": "密钥类参数不导出，迁移后需在目标环境重新填写",
    }


@router.post("/import")
def import_config(body: ParamsIn, _=Depends(auth.require_admin), db: Session = Depends(get_db)):
    """导入配置：与保存走同一套校验，非法项整批拒绝。"""
    values = {k: v for k, v in (body.values or {}).items() if k in P.BY_KEY}
    if not values:
        raise HTTPException(400, "没有可导入的参数")
    res = P.save(db, values, actor=body.actor or "admin")
    if not res["ok"]:
        return {"ok": False, "rejected": res["rejected"], "applied": []}
    db.add(AuditLog(action="params_import", actor=body.actor,
                    detail=f"导入 {len(res['applied'])} 项"))
    db.commit()
    return {"ok": True, "applied": res["applied"], **P.describe(db)}


@router.get("/audit")
def audit(limit: int = Query(20, ge=1, le=100), _=Depends(auth.require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {"ok": True, "items": [
        {"action": a.action, "doc_id": a.doc_id, "detail": a.detail, "actor": a.actor,
         "at": a.created_at.strftime("%Y-%m-%d %H:%M:%S")} for a in rows
    ]}
