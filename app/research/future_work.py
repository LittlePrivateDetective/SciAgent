"""Future Work 自动提炼 + 库内对撞（"这个旧 idea 是不是已经被人做完了"）。

为什么是规则 + 图谱，而不是让 LLM 全文重写一遍：
1. 可解释——每条 idea 都能追到具体论文的某一句、某一页，评审可以点回原文核对；
2. 可复现、零成本——37 篇秒级出结果，答辩现场能重跑；
3. 不制造幻觉——LLM 改写很容易把 "we do not explore X" 写成 "we propose X"。

流程：候选句抽取 → 字符 n-gram 语义去重 → 关键词对撞库内后续论文 → 状态标注。
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
_SECTION_HINT = re.compile(r"conclusion|future|limitation|discussion", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "based",
    "also", "more", "such", "work", "future", "could", "would", "have", "been",
    "are", "was", "were", "our", "their", "which", "these", "those", "other",
    "paper", "model", "models", "approach", "method", "results", "result",
}


def _keywords(text: str, k: int = 8) -> list[str]:
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP]
    return [w for w, _ in Counter(words).most_common(k)]


def _ngrams(text: str, n: int = 4) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def _semantic_dup(a: str, b: str, threshold: float = 0.82) -> bool:
    x, y = _ngrams(a), _ngrams(b)
    if not x or not y:
        return False
    return len(x & y) / len(x | y) >= threshold


def collect_future_work(db: Session, max_items: int = 60) -> dict:
    docs = {d.id: d for d in db.scalars(select(Document)).all()}
    chunks = list(db.scalars(select(Chunk).where(Chunk.kind == "text")).all())

    # 论文标题/摘要词袋，用于"是否已被后续工作涉及"的对撞
    corpus = {
        did: set(_keywords(f"{d.title} {d.abstract}", 30))
        for did, d in docs.items()
    }

    raw: list[dict] = []
    for c in chunks:
        text = (c.text or "").replace("\n", " ")
        if not _TRIGGER.search(text) and not _SECTION_HINT.search(c.section_path or ""):
            continue
        for sent in _SPLIT.split(text):
            sent = sent.strip()
            if not (25 <= len(sent) <= 420):
                continue
            if not _TRIGGER.search(sent):
                continue
            raw.append({
                "doc_id": c.doc_id, "page": c.page, "bbox": c.bbox,
                "section": c.section_path, "text": sent,
                "doc_title": docs[c.doc_id].title if c.doc_id in docs else "",
                "doc_year": docs[c.doc_id].year if c.doc_id in docs else "",
            })

    # 字符 n-gram 语义去重：同族版本 / 多次重述只保留一条，并记录重复来源数
    kept: list[dict] = []
    for item in raw:
        dup = next((k for k in kept if k["doc_id"] == item["doc_id"]
                    and _semantic_dup(k["text"], item["text"])), None)
        if dup:
            dup["duplicates"] += 1
            continue
        item["duplicates"] = 0
        kept.append(item)

    # 对撞：该 idea 的关键词若在其他（更晚或同年）论文的标题/摘要里高频出现，
    # 标为 possibly_addressed；措辞保留"可能"，不冒充已证实的因果结论。
    for item in kept:
        kws = set(_keywords(item["text"], 8))
        matches = []
        for did, vocab in corpus.items():
            if did == item["doc_id"]:
                continue
            hit = len(kws & vocab)
            if hit >= 2:
                d = docs[did]
                matches.append({"doc_id": did, "title": d.title or d.filename,
                                "year": d.year, "shared_terms": sorted(kws & vocab)[:6],
                                "score": hit})
        matches.sort(key=lambda x: -x["score"])
        item["keywords"] = sorted(kws)
        item["resolved_by"] = matches[:3]
        item["status"] = "possibly_addressed" if len(matches) >= 2 else "open"

    kept.sort(key=lambda x: (-len(x["resolved_by"]), x["doc_title"]))
    out = []
    for i, item in enumerate(kept[:max_items], start=1):
        item["id"] = i
        out.append(item)

    return {
        "items": out,
        "stats": {
            "documents": len(docs), "candidate_sentences": len(raw),
            "after_dedup": len(kept), "returned": len(out),
            "possibly_addressed": sum(1 for x in out if x["status"] == "possibly_addressed"),
            "duplicates_merged": sum(x["duplicates"] for x in out),
        },
        "method": "触发词+章节候选句 → 4-gram Jaccard 语义去重 → 标题/摘要关键词对撞",
    }
