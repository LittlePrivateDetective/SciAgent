"""论文的代码复现性对比：从正文抽 GitHub 仓库 → 查真实仓库状态 → 打分。

"有没有开源代码"只是第一步，科研真正关心的是**还能不能跑起来**：
star 数（社区验证）、最后 push（是否烂尾）、许可证（能不能合法复用）、
是否 archived（是否已归档）。外部 API 不通时如实降级，不编造 star。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document
from app.tools.external import github_repo

_RE_GH = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", re.I)


def _grade(info: dict | None) -> tuple[str, list[str]]:
    if not info:
        return "no_public_code", ["正文未检出 GitHub 仓库链接"]
    reasons = []
    score = 0
    stars = info.get("stars") or 0
    if stars >= 100:
        score += 2
    elif stars >= 10:
        score += 1
    reasons.append(f"star {stars}")
    if info.get("license"):
        score += 1
        reasons.append(f"许可证 {info['license']}")
    else:
        reasons.append("无明确许可证")
    pushed = info.get("pushed_at") or ""
    if pushed:
        try:
            dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).days
            reasons.append(f"最后更新 {age} 天前")
            if age < 730:
                score += 1
        except ValueError:
            pass
    if info.get("archived"):
        score -= 2
        reasons.append("仓库已归档")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return level, reasons


def collect_reproducibility(db: Session, max_docs: int = 8, online: bool = True) -> dict:
    docs = list(db.scalars(select(Document)).all())
    found: list[dict] = []
    for d in docs:
        chunks = db.scalars(
            select(Chunk).where(Chunk.doc_id == d.id).order_by(Chunk.idx).limit(400)
        ).all()
        repos: list[str] = []
        for c in chunks:
            for m in _RE_GH.finditer(c.text or ""):
                r = m.group(1).rstrip(".)/,;")
                if r not in repos:
                    repos.append(r)
            if len(repos) >= 2:
                break
        if repos:
            found.append({"doc_id": d.id, "title": d.title or d.filename,
                          "year": d.year, "repos": repos[:2]})
        if len(found) >= max_docs:
            break

    def probe(row: dict) -> dict:
        repo = row["repos"][0]
        if not online:
            info, degraded = None, True
        else:
            res = github_repo(repo, timeout=6, retries=1)
            info = res.data if res.ok else None
            degraded = res.degraded or not res.ok
        level, reasons = _grade(info)
        return {**row, "checked_repo": repo, "repo_info": info,
                "reproducibility": level, "reasons": reasons,
                "degraded": degraded,
                "source": "api.github.com" if info else ("unavailable" if degraded else "not_checked")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(probe, found))

    return {
        "items": results,
        "stats": {
            "documents_scanned": len(docs),
            "with_public_code": len(found),
            "high": sum(1 for x in results if x["reproducibility"] == "high"),
            "degraded": sum(1 for x in results if x["degraded"]),
        },
        "method": "正文正则抽 GitHub 链接 → GitHub REST API 查 star/license/pushed_at → 加权分级",
    }
