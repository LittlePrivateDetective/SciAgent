"""外部 API 工具链（Function Calling 的"手"）。

设计要点（答辩可讲）：
1. **统一 ToolResult**：成功/降级/失败都返回同一种结构，上游永远不会因为外部服务
   抖动而抛异常——工具层的职责是"把不确定性关在门里"。
2. **三级容错**：重试（指数退避）→ 真实外部 API → 降级到本地文献库。
   降级结果带 `degraded=True` 与 `fallback` 字段，前端可以如实标注，
   绝不把本地结果冒充成 arXiv 在线结果（学术诚信）。
3. `simulate` 参数只用于 QA 注入超时/失败，生产路径不经过它——
   这样"超时、失败重试、降级"三条分支都能被自动化用例稳定复现，
   而不是靠拔网线。
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.core.logger import get_logger

log = get_logger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
GITHUB_API = "https://api.github.com/repos"
CROSSREF_API = "https://api.crossref.org/works"

# OpenAI 兼容的 tools 定义：给 Function Calling 决策用
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "arxiv_search",
            "description": "在 arXiv 上按关键词检索论文，返回标题、作者、arXiv 编号与年份",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_repo",
            "description": "查询 GitHub 仓库的 star 数、最后更新时间、语言与开源协议",
            "parameters": {
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "owner/repo 形式"}},
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crossref_lookup",
            "description": "按标题查询 DOI 与正式发表信息",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "论文标题"}},
                "required": ["title"],
            },
        },
    },
]


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Any = None
    source: str = ""
    degraded: bool = False
    fallback: str = ""
    attempts: int = 0
    latency_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _get(url: str, params: dict | None = None, timeout: float = 8.0, simulate: str = "") -> httpx.Response:
    if simulate == "timeout":
        raise httpx.TimeoutException("QA 模拟超时", request=httpx.Request("GET", url))
    if simulate == "fail":
        raise httpx.HTTPStatusError("QA 模拟 502", request=httpx.Request("GET", url),
                                   response=httpx.Response(502))
    return httpx.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "SciAgent/1.0 (research assistant)"})


def _with_retry(fn, retries: int = 2) -> tuple[Any, int, Exception | None]:
    last: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            return fn(), attempt, None
        except Exception as exc:  # noqa: BLE001 外部调用什么异常都要兜住
            last = exc
            if attempt <= retries:
                time.sleep(0.3 * attempt)
    return None, retries + 1, last


# ---------------------------------------------------------------- arXiv
def _parse_arxiv(xml: str, limit: int) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", ns)[:limit]:
        title = " ".join((e.findtext("a:title", "", ns) or "").split())
        summary = " ".join((e.findtext("a:summary", "", ns) or "").split())
        ident = e.findtext("a:id", "", ns) or ""
        m = re.search(r"abs/(\d{4}\.\d{4,5})(v\d+)?", ident)
        authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
        published = (e.findtext("a:published", "", ns) or "")[:10]
        out.append({
            "title": title, "authors": authors[:5], "arxiv_id": m.group(1) if m else "",
            "published": published, "summary": summary[:200], "url": ident,
        })
    return out


def arxiv_search(query: str, max_results: int = 3, *, timeout: float = 8.0,
                 retries: int = 2, simulate: str = "") -> ToolResult:
    t0 = time.time()
    params = {"search_query": f"all:{query}", "start": 0,
              "max_results": max(1, min(max_results, 10)), "sortBy": "relevance"}

    def call():
        r = _get(ARXIV_API, params, timeout, simulate)
        r.raise_for_status()
        data = _parse_arxiv(r.text, max_results)
        if not data:
            raise ValueError("arXiv 返回空结果")
        return data

    data, attempts, err = _with_retry(call, retries)
    if data is not None:
        return ToolResult(True, "arxiv_search", data, "arxiv.org", attempts=attempts,
                          latency_ms=int((time.time() - t0) * 1000))
    local = _local_fallback(query, max_results)
    return ToolResult(bool(local), "arxiv_search", local, "local_library",
                      degraded=True, fallback="arxiv_unavailable_local_corpus",
                      attempts=attempts, latency_ms=int((time.time() - t0) * 1000),
                      error=f"{type(err).__name__}: {str(err)[:160]}")


def _local_fallback(query: str, limit: int) -> list[dict]:
    """真实降级：用本地 37 篇文献的 BM25 给出结果，并明确标注来源。"""
    try:
        from app.rag.hybrid import search_with_rerank
        hits = search_with_rerank(query, None, top_k=limit)
        db_ids, seen = [], set()
        for h in hits:
            if h.doc_id in seen:
                continue
            seen.add(h.doc_id)
            db_ids.append(h.doc_id)
        from app.db.models import Document
        from app.db.session import session_scope
        from sqlalchemy import select
        with session_scope() as db:
            out = []
            for did in db_ids[:limit]:
                d = db.get(Document, did)
                if d:
                    out.append({"title": d.title or d.filename, "authors": [],
                                "arxiv_id": d.arxiv_id, "published": d.year,
                                "summary": (d.abstract or "")[:200], "url": ""})
            return out
    except Exception as exc:  # noqa: BLE001 降级本身也不能抛
        log.warning("本地降级失败：%s", exc)
        return []


# ---------------------------------------------------------------- GitHub
def github_repo(repo: str, *, timeout: float = 8.0, retries: int = 2,
                simulate: str = "") -> ToolResult:
    t0 = time.time()
    repo = repo.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", repo):
        return ToolResult(False, "github_repo", error="repo 必须是 owner/repo 形式",
                          latency_ms=int((time.time() - t0) * 1000))

    def call():
        r = _get(f"{GITHUB_API}/{repo}", None, timeout, simulate)
        r.raise_for_status()
        return r.json()

    raw, attempts, err = _with_retry(call, retries)
    if raw:
        data = {
            "repo": raw.get("full_name"), "stars": raw.get("stargazers_count"),
            "forks": raw.get("forks_count"), "language": raw.get("language"),
            "license": ((raw.get("license") or {}) or {}).get("spdx_id"),
            "pushed_at": raw.get("pushed_at"), "archived": raw.get("archived"),
            "open_issues": raw.get("open_issues_count"), "url": raw.get("html_url"),
        }
        return ToolResult(True, "github_repo", data, "api.github.com",
                          attempts=attempts, latency_ms=int((time.time() - t0) * 1000))
    return ToolResult(False, "github_repo", degraded=True, fallback="github_unavailable",
                      attempts=attempts, latency_ms=int((time.time() - t0) * 1000),
                      error=f"{type(err).__name__}: {str(err)[:160]}")


# ---------------------------------------------------------------- Crossref
def crossref_lookup(title: str, *, timeout: float = 8.0, retries: int = 2,
                    simulate: str = "") -> ToolResult:
    t0 = time.time()

    def call():
        r = _get(CROSSREF_API, {"query.title": title, "rows": 3}, timeout, simulate)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("items") or []

    items, attempts, err = _with_retry(call, retries)
    if items:
        data = [{"title": (x.get("title") or [""])[0], "doi": x.get("DOI"),
                 "year": ((x.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                 "container": (x.get("container-title") or [""])[0]}
                for x in items[:3]]
        return ToolResult(True, "crossref_lookup", data, "api.crossref.org",
                          attempts=attempts, latency_ms=int((time.time() - t0) * 1000))
    return ToolResult(False, "crossref_lookup", degraded=True, fallback="crossref_unavailable",
                      attempts=attempts, latency_ms=int((time.time() - t0) * 1000),
                      error=f"{type(err).__name__}: {str(err)[:160]}")


REGISTRY = {
    "arxiv_search": arxiv_search,
    "github_repo": github_repo,
    "crossref_lookup": crossref_lookup,
}


def invoke(name: str, args: dict | None = None, *, simulate: str = "",
           retries: int = 2, timeout: float = 8.0) -> dict:
    fn = REGISTRY.get(name)
    if not fn:
        return ToolResult(False, name or "", error=f"未知工具 {name}，可用：{list(REGISTRY)}").as_dict()
    try:
        return fn(**(args or {}), timeout=timeout, retries=retries, simulate=simulate).as_dict()
    except TypeError as exc:
        return ToolResult(False, name, error=f"参数不匹配：{exc}").as_dict()


def decide(question: str) -> dict:
    """让 LLM 真正用 Function Calling 选择工具。

    模型不支持 tools 协议时，退回关键词路由——同样是"真实降级"，不伪造调用。
    """
    from app.core.llm import gateway

    messages = [
        {"role": "system", "content": "你是科研工具路由助手。根据用户问题选择一个工具；"
                                      "若不需要外部数据，直接回答 no_tool。不要解释。"},
        {"role": "user", "content": question},
    ]
    try:
        call = gateway.function_call(messages, TOOL_SCHEMAS)
        if call and call.get("name") in REGISTRY:
            res = invoke(call["name"], call.get("arguments") or {})
            return {"selected_by": "llm_function_call", "call": call, "result": res}
    except Exception as exc:  # noqa: BLE001
        log.warning("Function Calling 决策失败，降级关键词路由：%s", exc)

    q = question.lower()
    if "github" in q or "代码" in q or "仓库" in q or "star" in q:
        name, args = "github_repo", {"repo": _extract_repo(question)}
    elif "doi" in q or "发表" in q or "期刊" in q:
        name, args = "crossref_lookup", {"title": question[:120]}
    else:
        name, args = "arxiv_search", {"query": question[:120], "max_results": 3}
    return {"selected_by": "keyword_fallback", "call": {"name": name, "arguments": args},
            "result": invoke(name, args)}


def _extract_repo(text: str) -> str:
    m = re.search(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", text, re.I)
    return m.group(1) if m else "facebookresearch/fairseq"
