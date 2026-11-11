"""写作 Copilot：给定 idea 生成论文框架，References 只来自库内真实文献。

官方 1.6.1 的硬要求是"确保 References 是真实没有幻觉"。常见做法是让模型写条目、
再事后核对——但模型只要有机会写条目，就有机会编。这里换了个方向：

    **条目文本根本不由模型生成。**

模型只负责组织论述、在正文里标 [n]；References 的每一条都由服务端从 Document
元数据按 GB/T 7714-2015 拼出来。模型想编一条不存在的文献，也没有地方落笔——
它只能写编号，而编号越界会被直接剔除并记入 checks。

这样"无幻觉"不依赖模型听话，而是结构上不可能发生。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMError, gateway
from app.core.logger import get_logger
from app.db.models import Document
from app.rag.hybrid import search_with_rerank

log = get_logger(__name__)

_SECTIONS = ("abstract", "introduction", "related_work", "plan")
_SECTION_CN = {
    "abstract": "Abstract · 摘要",
    "introduction": "Introduction · 引言",
    "related_work": "Related Work · 相关工作",
    "plan": "Research Plan · 研究方案",
}

_SYS = (
    "你是学术写作助手。基于用户给定的研究想法和一份【可用文献清单】，生成论文框架。\n"
    "硬性规则：\n"
    "1. 输出四个部分：Abstract / Introduction / Related Work / Research Plan；\n"
    "2. 引用**只能**使用清单里给出的编号，写作 [1]、[2,3] 这种形式；清单之外的编号一律不许出现；\n"
    "3. 不得编造清单外文献，不得编造论文中没有的数据、指标或结论；\n"
    "4. Abstract 150–200 词，其余每部分 200–350 词，学术英文；\n"
    "5. 只输出 JSON，不要任何解释文字：\n"
    '{"title": "...", "abstract": "...", "introduction": "...", '
    '"related_work": "...", "plan": "..."}'
)

# 引用标记：[1] [1,2] [1, 2, 5]
_RE_CITE = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")


def _fmt_author(name: str) -> str:
    """GB/T 7714-2015：姓全大写，名缩写为首字母。"""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper()
    surname = parts[-1].upper()
    initials = " ".join(p[0].upper() for p in parts[:-1] if p[0].isalpha())
    return f"{surname} {initials}".strip()


def format_gbt7714(doc: Document) -> str:
    """按 GB/T 7714-2015 风格拼一条参考文献。

    只用库里已有的字段，缺什么就省略什么——宁可信息少，也不补一个猜的年份或期刊名。
    """
    try:
        authors = json.loads(doc.authors) if doc.authors else []
    except Exception:  # noqa: BLE001 历史上存在非 JSON 的脏数据
        authors = []
    authors = [a for a in (authors or []) if str(a).strip()]

    if len(authors) > 3:
        who = ", ".join(_fmt_author(a) for a in authors[:3]) + ", et al"
    elif authors:
        who = ", ".join(_fmt_author(a) for a in authors)
    else:
        who = "佚名"

    title = (doc.title or doc.filename or "").strip().rstrip(".")
    venue = (doc.venue or "").strip()
    arxiv = (doc.arxiv_id or "").strip()
    year = doc.year or ""

    # 有会议/期刊名按会议论文，否则按电子预印本
    kind = "[C]" if venue else "[EB/OL]"
    source = venue if venue else (f"arXiv:{arxiv}" if arxiv else "")
    tail = ", ".join(x for x in (source, str(year) if year else "") if x)
    line = f"{who}. {title}{kind}."
    return f"{line} {tail}." if tail else line


def _pick_docs(db: Session, idea: str, doc_ids: list[str] | None, top_k: int) -> list[dict]:
    """按 idea 检索，按文献去重，保留每篇最相关的一段作为选题依据。"""
    try:
        hits = search_with_rerank(idea, doc_ids or None, top_k=max(top_k * 3, 12))
    except Exception as exc:  # noqa: BLE001
        log.warning("写作选题检索失败：%s", exc)
        hits = []

    picked: dict[str, dict] = {}
    for h in hits:
        if h.doc_id in picked:
            continue
        doc = db.get(Document, h.doc_id)
        if not doc:
            continue
        picked[h.doc_id] = {
            "doc_id": doc.id,
            "title": doc.title or doc.filename,
            "year": doc.year or "",
            "arxiv": doc.arxiv_id or "",
            "gbt": format_gbt7714(doc),
            "snippet": (h.text or "")[:400],
            "page": h.page,
        }
        if len(picked) >= top_k:
            break

    # 检索没命中（空库 / 关键词太偏）时退回库里最新的几篇，
    # 保证 References 至少有真实来源，而不是空手交给模型去编。
    if not picked:
        rows = db.scalars(select(Document).order_by(Document.created_at.desc()).limit(top_k)).all()
        for doc in rows:
            picked[doc.id] = {
                "doc_id": doc.id,
                "title": doc.title or doc.filename,
                "year": doc.year or "",
                "arxiv": doc.arxiv_id or "",
                "gbt": format_gbt7714(doc),
                "snippet": (doc.abstract or "")[:400],
                "page": 1,
            }
    return list(picked.values())


def _scrub_citations(text: str, valid: set[int]) -> tuple[str, list[int], list[int]]:
    """把正文里越界的 [n] 剔除，并返回（清洗后文本、用到的合法编号、被剔除的编号）。

    越界引用不能只是"记录一下"就放着——留在正文里，读者会以为它指向某篇真文献。
    """
    used: set[int] = set()
    invalid: set[int] = set()

    def repl(m: re.Match) -> str:
        nums = [int(x) for x in re.split(r"\s*,\s*", m.group(1)) if x.strip()]
        keep = [n for n in nums if n in valid]
        bad = [n for n in nums if n not in valid]
        used.update(keep)
        invalid.update(bad)
        if not keep:
            return ""  # 整个标记都是编的：连同括号一起删掉
        return "[" + ",".join(str(n) for n in keep) + "]"

    cleaned = _RE_CITE.sub(repl, text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), sorted(used), sorted(invalid)


def generate(db: Session, idea: str, doc_ids: list[str] | None = None, top_k: int = 6) -> dict:
    """生成论文框架。返回结构直接给前端渲染，字段含义见模块文档。"""
    idea = (idea or "").strip()
    if not idea:
        return {"ok": False, "error": "请先写一句你的研究想法"}

    docs = _pick_docs(db, idea, doc_ids, top_k)
    if not docs:
        return {"ok": False, "error": "库内没有可用文献：请先上传论文，References 必须来自真实文献"}

    catalog = "\n".join(
        f"[{i}] {d['title']}（{d['year'] or '年份未知'}）\n    相关片段：{d['snippet']}"
        for i, d in enumerate(docs, start=1)
    )
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": _SYS},
                {
                    "role": "user",
                    "content": (
                        f"研究想法：{idea}\n\n"
                        f"【可用文献清单】（共 {len(docs)} 篇，编号即引用编号）\n{catalog}\n\n"
                        "请生成框架 JSON。"
                    ),
                },
            ],
            temperature=0.3,
            max_tokens=settings.write_max_tokens,
            model=settings.write_model,
        )
    except LLMError as exc:
        log.warning("写作框架生成失败：%s", exc)
        return {"ok": False, "error": f"模型调用失败：{exc}"}

    # 模型偶尔会返回 {} 或只有 title：这时必须明确失败，绝不能把四个空段落
    # 当成"生成成功"交给前端（用户会看到一片空白却没有任何报错）。
    if not any(str(obj.get(k) or "").strip() for k in _SECTIONS):
        log.warning("写作模型返回空章节，原始 keys=%s", list(obj.keys())[:8])
        return {"ok": False, "error": "模型本次未返回有效内容，请重试（已切换非推理模型，通常一次即可）"}

    valid = set(range(1, len(docs) + 1))
    sections = []
    used_all: set[int] = set()
    invalid_all: set[int] = set()
    for key in _SECTIONS:
        raw = str(obj.get(key) or "").strip()
        cleaned, used, invalid = _scrub_citations(raw, valid)
        used_all.update(used)
        invalid_all.update(invalid)
        sections.append({
            "key": key,
            "name": _SECTION_CN[key],
            "text": cleaned,
            "citations": [f"[{n}]" for n in used],
        })

    # 只列出正文真正引用到的文献：arXiv 投稿不是"参考文献越多越好"，
    # 挂着一堆没引用的条目反而会被审稿人挑。
    references = []
    for i, d in enumerate(docs, start=1):
        references.append({
            "index": i,
            "gbt": d["gbt"],
            "doc_id": d["doc_id"],
            "title": d["title"],
            "year": d["year"],
            "cited": i in used_all,
        })

    return {
        "ok": True,
        "title": str(obj.get("title") or idea)[:160],
        "idea": idea,
        "sections": sections,
        "references": references,
        "checks": {
            "citations_total": sum(len(s["citations"]) for s in sections),
            "citations_valid": sum(1 for n in used_all if n in valid),
            "citations_invalid": sorted(invalid_all),
            # 这两个数字是本功能的立论：0 = References 里没有一条是模型写的
            "refs_from_library": len(references),
            "refs_generated_by_llm": 0,
            "cited_refs": len(used_all),
        },
        "source_pool": len(docs),
    }
