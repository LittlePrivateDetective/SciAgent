"""Future Work 自动提炼 + 保守的库内方法/结果证据对撞。

候选必须确实是研究问题，工程发布计划不进入列表；“可能已涉及”必须给出
后续论文中的具体方法或结果原句，不再用标题/摘要里的通用词共现代替证据。
"""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document

_TRIGGER = re.compile(
    r"\b(future work|future research|future direction|in future work|we plan to|we intend to|"
    r"we leave|remains? (?:an? )?open|open question|not (?:yet )?explor|"
    r"could be (?:extended|improved)|limitation|worth investigating|left for future)\b",
    re.I,
)
_ENGINEERING_TODO = re.compile(
    r"\b(?:make|release|publish|share|open[ -]source|available|repository|github)\b.*"
    r"\b(?:code|source|data(?:set)?|models?|checkpoints?)\b|"
    r"\b(?:code|source|data(?:set)?|models?|checkpoints?)\b.*\b(?:available|release|publish|share)\b",
    re.I,
)
_RESEARCH_ACTION = re.compile(
    r"\b(?:investigat|explor|improv|extend|generaliz|address|overcome|"
    r"understand|analy[sz]|compar|optimi[sz]|develop|study|learn|reason|align)", re.I,
)
_METHOD_RESULT = re.compile(
    r"\b(?:we propose|we introduce|we present|our method|our approach|results? (?:show|demonstrate)|"
    r"we achieve|we outperform|improves?|reduces?|outperforms?)\b", re.I,
)
_SECTION_HINT = re.compile(r"conclusion|future|limitation|discussion", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "based",
    "also", "more", "such", "work", "future", "could", "would", "have", "been",
    "are", "was", "were", "our", "their", "which", "these", "those", "other",
    "paper", "model", "models", "approach", "method", "methods", "results", "result",
    # 工程发布词和过泛领域词不能充当“问题已解决”的证据。
    "available", "code", "source", "release", "released", "dataset", "datasets", "data",
    "github", "repository", "make", "used", "train", "training", "evaluate", "evaluation",
    "language", "large", "neural", "network", "networks", "learning", "performance",
}


def _keywords(text: str, k: int = 8) -> list[str]:
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP]
    return [w for w, _ in Counter(words).most_common(k)]


def _is_research_future(text: str) -> bool:
    """工程发布计划不是研究开放问题；同句有明确研究动词时仍保留。"""
    if not _TRIGGER.search(text or ""):
        return False
    if _ENGINEERING_TODO.search(text) and not _RESEARCH_ACTION.search(text):
        return False
    return True


def _ngrams(text: str, n: int = 4) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def _semantic_dup(a: str, b: str, threshold: float = 0.82) -> bool:
    x, y = _ngrams(a), _ngrams(b)
    if not x or not y:
        return False
    return len(x & y) / len(x | y) >= threshold


def _doc_family(doc: Document) -> str:
    if getattr(doc, "family_key", ""):
        return str(doc.family_key)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (doc.title or doc.filename or "").lower())


def _evidence_match(item_text: str, doc: Document, evidence_chunks: list[Chunk]) -> dict | None:
    """返回具体方法/结果证据；通用词、无方法结果句或证据不足一律不匹配。"""
    idea_terms = set(_keywords(item_text, 12))
    overview_terms = set(_keywords(f"{doc.title or ''} {doc.abstract or ''}", 30))
    # 标题/摘要至少承接一个具体主题词，方法/结果原句至少三个。
    if not idea_terms & overview_terms:
        return None
    best: dict | None = None
    for c in evidence_chunks:
        if not _METHOD_RESULT.search(c.text or ""):
            continue
        for sent in _SPLIT.split((c.text or "").replace("\n", " ")):
            if not _METHOD_RESULT.search(sent):
                continue
            shared = sorted(idea_terms & set(_keywords(sent, 30)))
            if len(shared) < 3:
                continue
            candidate = {"doc_id": doc.id, "title": doc.title or doc.filename, "year": doc.year,
                         "shared_terms": shared[:8], "score": len(shared),
                         "evidence": sent[:600], "page": c.page, "bbox": c.bbox,
                         "chunk_id": c.id}
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def collect_future_work(db: Session, max_items: int = 60) -> dict:
    docs = {d.id: d for d in db.scalars(select(Document)).all()}
    chunks = list(db.scalars(select(Chunk).where(Chunk.kind == "text")).all())
    chunks_by_doc: dict[str, list[Chunk]] = {}
    for c in chunks:
        chunks_by_doc.setdefault(c.doc_id, []).append(c)

    raw: list[dict] = []
    # 同族优先保留文字版，避免 OCR 残句抢占规范来源。
    for c in sorted(chunks, key=lambda x: bool(docs.get(x.doc_id) and docs[x.doc_id].is_scanned)):
        text = (c.text or "").replace("\n", " ")
        if not _TRIGGER.search(text) and not _SECTION_HINT.search(c.section_path or ""):
            continue
        for sent in _SPLIT.split(text):
            sent = sent.strip()
            if not (25 <= len(sent) <= 420) or not _is_research_future(sent):
                continue
            raw.append({"doc_id": c.doc_id, "page": c.page, "bbox": c.bbox,
                        "section": c.section_path, "text": sent,
                        "doc_title": docs[c.doc_id].title if c.doc_id in docs else "",
                        "doc_year": docs[c.doc_id].year if c.doc_id in docs else ""})

    kept: list[dict] = []
    for item in raw:
        family = _doc_family(docs[item["doc_id"]]) if item["doc_id"] in docs else item["doc_id"]
        dup = next((k for k in kept if k["family"] == family
                    and _semantic_dup(k["text"], item["text"])), None)
        if dup:
            dup["duplicates"] += 1
            continue
        item["duplicates"] = 0
        item["family"] = family
        kept.append(item)

    for item in kept:
        item["keywords"] = sorted(_keywords(item["text"], 8))
        matches = []
        source = docs.get(item["doc_id"])
        for did, d in docs.items():
            if did == item["doc_id"] or (source and _doc_family(source) == _doc_family(d)):
                continue
            try:
                if str(item["doc_year"]).isdigit() and str(d.year).isdigit() and int(d.year) < int(item["doc_year"]):
                    continue
            except (TypeError, ValueError):
                pass
            match = _evidence_match(item["text"], d, chunks_by_doc.get(did, []))
            if match:
                matches.append(match)
        matches.sort(key=lambda x: -x["score"])
        item["resolved_by"] = matches[:3]
        item["status"] = "possibly_addressed" if matches else "open"
        item.pop("family", None)

    kept.sort(key=lambda x: (-len(x["resolved_by"]), x["doc_title"]))
    out = []
    for i, item in enumerate(kept[:max_items], start=1):
        item["id"] = i
        out.append(item)
    return {
        "items": out,
        "stats": {"documents": len(docs), "candidate_sentences": len(raw),
                  "after_dedup": len(kept), "returned": len(out),
                  "possibly_addressed": sum(1 for x in out if x["status"] == "possibly_addressed"),
                  "duplicates_merged": sum(x["duplicates"] for x in out)},
        "method": "研究问题/工程待办分离 → 同族 4-gram 去重 → 具体方法或结果原句对撞",
    }
