"""Future Work 自动提炼 + 保守的库内方法/结果证据对撞。

候选必须确实是研究问题，工程发布计划不进入列表；“可能已涉及”必须给出
后续论文中的具体方法或结果原句，不再用标题/摘要里的通用词共现代替证据。
"""
from __future__ import annotations

import re
import time
from collections import Counter

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.core import runtime
from app.db.models import Chunk, Citation, Document
from app.rag.embed import embed_texts

log = get_logger(__name__)

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
# 参考文献条目与目录行：中文期刊的参考文献常被版面判成正文块（GB/T 7714 的
# `[C]//`、`[J].`、`pp.` 形态），一旦落到 text 块里，"未来研究方向"的触发词
# （limitation / future）会从条目里的标题命中，于是整条参考文献被当成研究空白。
# 这类句子没有任何研究动作，只是排版垃圾，必须在这里挡掉。
_REFERENCE_ENTRY = re.compile(
    r"\[[A-Z]{1,2}\]\s*//|\[(?:J|M|D|C|R|S|P)\]\.|//\s*[A-Z][A-Za-z]|"
    r"\bProceedings of (?:the )?\d|\bpp?\.\s*\d+\s*[-–—]\s*\d+|"
    r"\bvol\.\s*\d+\s*[,，(]|\bdoi:\s*10\.|\barXiv:\d{4}\.\d{4,5}",
)
# 目录行/导航句：点线 + 页码、或独立成行的 "目录 / CONTENTS"。
# 表格残留：勾选符号/伪表头会被 _METHOD_RESULT 里的 "results" 之类误命中
_TABLE_NOISE = re.compile(r"[✓✗×]|\|\s*-{2,}|\bJuly \d{4}\b")
_TOC_LINE = re.compile(
    r"(?:\.{3,}|…{2,}|·{3,})\s*\d{1,4}\s*$|^(?:目\s*录|contents|table of contents)\s*$", re.I
)
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
    """关键词兜底判据：句向量不可用（无 embedding Key / 纯本地没建向量）时才走这里。"""
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


# ---------------- 后继文献的语义比对 ----------------
# 为什么不再用「全库较新论文 + 关键词共现」：
#   共现不等于继承。一篇从没引用过它的论文，只要方法句撞上三个词，旧实现就标成
#   "可能已被后续工作触及"，被追问"哪篇做的"就站不住。这里改成两级证据：
#     ① 引用图上的后继（引用了本文、且不早于本文）—— 学术意义上的"后续工作"；
#     ② 一条后继都没有时，才退到同年或更晚的同主题文献，并如实标 basis=related，
#        不把弱证据伪装成强证据。
# 判据也从"数共享词"换成句子向量余弦：关键词只用来解释"为什么像"，
# 不再当门票——它太容易在通用词上误报。

_COS_MIN = 0.58          # 块级粗筛门限：只负责召回，不负责下结论
_SENT_COS_MIN = 0.65     # 句级精排门限：句与句同一粒度，这个阈值才代表"同一件事"


def _successors(db: Session) -> dict[str, list[str]]:
    """dst_doc_id -> [引用了它的 src_doc_id]。一次查全表，别在循环里查。"""
    rows = db.execute(
        select(Citation.dst_doc_id, Citation.src_doc_id).where(Citation.dst_doc_id.isnot(None))
    ).all()
    out: dict[str, list[str]] = {}
    for dst, src in rows:
        if dst and src and dst != src:
            out.setdefault(dst, []).append(src)
    return out


def _doc_matrix(db: Session, doc_id: str, dim: int, offline: bool,
                cache: dict[str, tuple]) -> tuple:
    """取一篇文献的块向量矩阵（已 L2 归一化），按 doc 缓存。

    双轨向量必须读对应那一列：云端 embedding / 本地 embedding_local。
    维度对不上的块直接跳过——历史上用 hash 兜底建过向量，混进来会算出无意义的相似度。
    """
    if doc_id in cache:
        return cache[doc_id]
    # 只取需要的列，不实例化 ORM 对象：一页几千块、几十篇下来，ORM 构造本身
    # 在线上单核沙箱里就是十几秒（实测整条链路 43 秒，大头在这里）。
    rows = db.execute(
        select(Chunk.id, Chunk.text, Chunk.page, Chunk.bbox,
               Chunk.embedding, Chunk.embedding_local)
        .where(Chunk.doc_id == doc_id, Chunk.kind == "text")
    ).all()
    vecs: list = []
    keep: list = []
    for c in rows:
        raw = c.embedding_local if offline else c.embedding
        if not raw:
            continue
        v = np.frombuffer(raw, dtype=np.float32)
        if dim and v.shape[0] != dim:
            continue
        vecs.append(v)
        keep.append(c)
    if not vecs:
        cache[doc_id] = (None, [])
        return cache[doc_id]
    mat = np.vstack(vecs).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    cache[doc_id] = ((mat / norms), keep)
    return cache[doc_id]


def _candidates(item_text: str, item_vec, doc: Document, mat, chunks: list[Chunk],
                min_cos: float = _COS_MIN, per_chunk: int = 2) -> list[dict]:
    """块级粗筛：找出"这段可能说的是同一件事"的候选句。

    粒度问题：未来工作是**短句**，块是**整段**，两者直接比余弦天然偏低，且阈值一放宽
    就会被"都提到 GPT-3 / image encoder"这种主题词带偏。所以这里只做召回（宁可多收），
    由调用方再用句向量做一次同粒度的精排——和主检索链路的「稠密召回 + 重排」是同一个思路。
    """
    if mat is None or item_vec is None:
        return []
    sims = mat @ item_vec
    order = np.argsort(-sims)
    idea_terms = set(_keywords(item_text, 12))
    out: list[dict] = []
    for idx in order:
        cos = float(sims[int(idx)])
        if cos < min_cos:
            break
        c = chunks[int(idx)]
        text = (c.text or "").replace("\n", " ")
        if not _METHOD_RESULT.search(text):
            continue
        taken = 0
        for sent in _SPLIT.split(text):
            if not _METHOD_RESULT.search(sent):
                continue
            if _TABLE_NOISE.search(sent) or _REFERENCE_ENTRY.search(sent):
                continue
            shared = sorted(idea_terms & set(_keywords(sent, 30)))
            if len(shared) < 2:
                continue
            out.append({"doc_id": doc.id, "title": doc.title or doc.filename, "year": doc.year,
                        "score": round(cos, 3), "block_cos": round(cos, 3),
                        "shared_terms": shared[:8], "sentence": sent,
                        "evidence": sent[:600], "page": c.page, "bbox": c.bbox,
                        "chunk_id": c.id})
            taken += 1
            if taken >= per_chunk:
                break
    return out


def _related_pool(docs: dict[str, Document], source: Document, limit: int = 20) -> list[str]:
    """没有引用后继时的退路：同年或更晚、不同族、按年份就近取。"""
    out: list[str] = []
    fam = _doc_family(source)
    for did, d in docs.items():
        if did == source.id or _doc_family(d) == fam:
            continue
        try:
            if int(d.year) < int(source.year):
                continue
        except (TypeError, ValueError):
            pass
        out.append(did)
    out.sort(key=lambda x: (docs[x].year or "", x))
    return out[:limit]



# 结果缓存：这条链路要算 142 条句向量 + 多篇块矩阵，实测 7–10 秒。库里文献不变时
# 反复点「未来工作方向」不该每次都重算——接口走的是读请求，反代 20 秒就切了。
_FW_CACHE: dict = {"key": None, "data": None, "ts": 0.0}
_FW_TTL = 3600.0


def collect_future_work(db: Session, max_items: int = 60) -> dict:
    key = (db.scalar(select(func.count()).select_from(Document)),
           db.scalar(select(func.count()).select_from(Chunk)),
           max_items)
    if _FW_CACHE["key"] == key and time.time() - _FW_CACHE["ts"] < _FW_TTL:
        return _FW_CACHE["data"]

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
            # 结构噪声先挡一层：条目/目录/表格行即使含 limitation 之类的词也不是研究问题
            if (_REFERENCE_ENTRY.search(sent) or _TOC_LINE.search(sent)
                    or _TABLE_NOISE.search(sent)):
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

    # 句向量一次批量算完：逐条调接口会把 60 条拉成 60 次网络往返。
    # 拿不到就退回关键词判据——宁可保守，不能因为向量服务抖动让整块功能失效。
    offline = runtime.is_offline()
    texts = [it["text"] for it in kept]
    vecs = None
    if texts:
        try:
            vecs = embed_texts(texts)
        except Exception as exc:  # noqa: BLE001
            log.warning("未来工作：句向量不可用，退回关键词判据（%s）", exc)
    dim = int(vecs.shape[1]) if vecs is not None and len(vecs) else 0

    successors = _successors(db)
    mat_cache: dict[str, tuple] = {}
    with_successor = 0

    # 第一段：块级召回。每条未来工作最多留 3 句进精排，控制第二次 embedding 的量。
    coarse: dict[int, list[dict]] = {}
    for i, item in enumerate(kept):
        item["keywords"] = sorted(_keywords(item["text"], 8))
        source = docs.get(item["doc_id"])
        if source is None:
            item["resolved_by"], item["status"], item["basis"] = [], "open", ""
            continue

        succ = [d for d in successors.get(item["doc_id"], [])
                if d in docs and _doc_family(docs[d]) != _doc_family(source)]
        if succ:
            with_successor += 1

        # 强证据：引用图上引用了本文的后继；弱证据：同年或更晚的同主题文献。
        pools: list[tuple[list[str], str]] = []
        if succ:
            pools.append((succ, "successor"))
        pools.append((_related_pool(docs, source), "related"))

        cands: list[dict] = []
        seen: set[str] = set()
        for pool, basis in pools:
            for did in pool:
                if did in seen:
                    continue
                seen.add(did)
                doc = docs.get(did)
                if doc is None:
                    continue
                if vecs is None:
                    hit = _evidence_match(item["text"], doc, chunks_by_doc.get(did, []))
                    if hit:
                        hit["basis"] = basis
                        hit["cos"] = 0.0
                        cands.append(hit)
                else:
                    mat, chs = _doc_matrix(db, did, dim, offline, mat_cache)
                    for c in _candidates(item["text"], vecs[i], doc, mat, chs):
                        c["basis"] = basis
                        cands.append(c)
            # 早停：引用后继里已经召回到候选，就不再扫同主题文献那一大池
            # （39 篇全扫是 43 秒的主要来源；这一步让大多数条目只比 5–10 篇）
            if cands:
                break
        if cands:
            cands.sort(key=lambda x: (x.get("basis") != "successor", -x.get("block_cos", 0)))
            coarse[i] = cands[:3]

    # 第二段：句级精排。句-句同一粒度，余弦才可比；块级分数只用来排序候选。
    polished: dict[int, list[dict]] = {}
    if coarse:
        flat = [(i, c) for i, cs in coarse.items() for c in cs]
        svecs = None
        if vecs is not None:
            try:
                svecs = embed_texts([c["sentence"] for _, c in flat])
            except Exception as exc:  # noqa: BLE001
                log.warning("未来工作：句级精排不可用，退回块级分数（%s）", exc)
        if svecs is None:
            for i, cs in coarse.items():
                polished[i] = sorted(cs, key=lambda x: -x.get("block_cos", 0))[:3]
        else:
            for (i, c), v in zip(flat, svecs):
                cos2 = float(np.dot(vecs[i], v))
                c["cos"] = round(cos2, 3)
                c["score"] = round(cos2, 3)
                c.pop("sentence", None)
                if cos2 >= _SENT_COS_MIN:
                    polished.setdefault(i, []).append(c)

    for i, item in enumerate(kept):
        got = polished.get(i, [])
        # 同一题里优先用「引用后继」那条证据，其次才看分数
        got.sort(key=lambda x: (x.get("basis") != "successor", -x.get("cos", 0)))
        item["resolved_by"] = got[:3]
        item["status"] = "possibly_addressed" if got else "open"
        # basis 说明结论强度：successor=引用图上的后继，related=只是同主题较新文献
        item["basis"] = got[0].get("basis", "") if got else ""
        item.pop("family", None)

    kept.sort(key=lambda x: (-len(x["resolved_by"]), x["doc_title"]))
    out = []
    for i, item in enumerate(kept[:max_items], start=1):
        item["id"] = i
        out.append(item)
    result = {
        "items": out,
        "stats": {"documents": len(docs), "candidate_sentences": len(raw),
                  "after_dedup": len(kept), "returned": len(out),
                  "possibly_addressed": sum(1 for x in out if x["status"] == "possibly_addressed"),
                  "duplicates_merged": sum(x["duplicates"] for x in out),
                  "with_citing_successor": with_successor},
        "method": ("研究问题/工程待办分离 → 同族 4-gram 去重 → "
                   "引用图上后继文献的句向量语义比对（无后继时退到同主题文献，标 basis=related）"),
    }
    _FW_CACHE.update({"key": key, "data": result, "ts": time.time()})
    return result
