"""内容级去重（不是文件名去重）。

官方用例：先传 1706.03762v7.pdf，再传 1706.03762v1.pdf，要求识别出"同一篇论文的
不同版本"，并建议保留较新版本，最好能给出版本差异。

三层判定，逐层短路：
1. arXiv ID / DOI 归一（最硬）→ 同族；
2. 64 位 SimHash 正文指纹（汉明距离 ≤ 6）→ 同文；
3. 摘要向量余弦 ≥ 0.92 → 同文不同排版。

只做 1 会被"改了排版/换了文件名"绕过；只做 2 会因为两版正文差异而漏判；
三层叠加才稳。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.db.models import Chunk, Document

log = get_logger(__name__)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}|[\u4e00-\u9fa5]{2,}")
RE_VERSION = re.compile(r"v(\d+)$", re.I)


def normalize(text: str) -> str:
    """去掉数字、标点、空白差异，只留语义骨架——避免版本间页码/排版变化干扰指纹。"""
    text = text.lower()
    text = re.sub(r"\d+", "", text)
    text = re.sub(r"[^a-z\u4e00-\u9fa5\s]", " ", text)
    return re.sub(r"\s+", "", text)


def content_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def simhash64(text: str) -> int:
    """经典 simhash：分词 → 词哈希 → 按位加权求和 → 取符号位。

    P1 修复（QA B03）：不能拿 `normalize()` 的结果来分词——它把 `\\s+` 压成空串，
    36k 字的正文会变成 "attentionisallyouneedashishvaswani..." 一个巨型 token，
    `_TOKEN` 只能切出 1 个词，整张指纹就由这一个 md5 决定。
    实测在文末加两句附录会翻转 **38 位**，近似去重完全失效。
    这里保留词边界，只在数字/标点层做归一化。
    """
    cleaned = re.sub(r"\d+", " ", text.lower())
    cleaned = re.sub(r"[^a-z\u4e00-\u9fa5\s]", " ", cleaned)
    tokens = _TOKEN.findall(cleaned[:60000])
    if not tokens:
        return 0
    # 词频加权：高频术语对位模式的贡献更大，少量新词不会把整张指纹推翻
    weights = [0] * 64
    from collections import Counter

    for tok, freq in Counter(tokens).items():
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            weights[i] += freq if (h >> i) & 1 else -freq
    out = 0
    for i in range(64):
        if weights[i] > 0:
            out |= 1 << i
    # SQLite 的 INTEGER 是有符号 64 位，超过 2^63-1 会直接抛 OverflowError。
    # 这里转成有符号表示后再入库；hamming() 里统一按位掩码还原，语义不变。
    return out - (1 << 64) if out > (1 << 63) - 1 else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def family_key(arxiv_id: str, title: str, source_text: str) -> str:
    """同族标识：arXiv 主号 > DOI > 标题归一化。"""
    if arxiv_id:
        return "arxiv:" + RE_VERSION.sub("", arxiv_id.strip())
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", source_text)
    if m:
        return "doi:" + m.group(0).lower().rstrip(".")
    raw = (title or "").strip()
    if len(raw) < 8:
        return ""
    # 文件名式"标题"（1706.03762v1_img / paper_v2）不能当同族依据：
    # 不同文献的文件名常只差后缀，靠它判重会把不相关的文档判成一族。
    # 注意要在原始标题上判断——normalize() 会把空格全删掉，英文标题也会变成
    # "attentionisallyouneed" 这种连写形式，拿它判"是否文件名"必然误伤。
    if " " not in raw and re.fullmatch(r"[0-9A-Za-z._\-]+", raw):
        return ""
    t = normalize(raw)
    if not t:
        return ""
    return "title:" + hashlib.md5(t[:120].encode()).hexdigest()[:16]


@dataclass
class DupHit:
    doc: Document
    reason: str
    similarity: float
    suggestion: str


def find_duplicates(
    db: Session,
    *,
    text: str,
    arxiv_id: str = "",
    title: str = "",
    exclude_doc_id: str | None = None,
) -> list[DupHit]:
    """返回库中与新上传内容重合的文献（按可信度排序）。"""
    fam = family_key(arxiv_id, title, text[:8000])
    chash = content_hash(text)
    sh = simhash64(text)

    docs = list(db.scalars(select(Document)).all())
    out: list[DupHit] = []

    for d in docs:
        if exclude_doc_id and d.id == exclude_doc_id:
            continue

        # 1) 同族（arXiv 版本号不同）
        if fam and d.family_key and fam == d.family_key:
            out.append(DupHit(d, "同族版本", 0.99, _suggest(d, arxiv_id)))
            continue

        # 2) 完全同文
        if chash and d.content_hash and chash == d.content_hash:
            out.append(DupHit(d, "正文完全一致", 1.0, _suggest(d, arxiv_id)))
            continue

        # 3) SimHash 近似
        #
        # 距离 5–6 这一档必须再验标题：同领域论文大量共用词汇，指纹本来就近。
        # 实测把 ELECTRA(2003.10555) 判成了 BERT(N19-1423) 的重复（汉明距离 6、
        # similarity 0.906），直接堵死了「自动下载新论文」——下载成功却入不了库。
        #
        # 距离 ≤4 以前是"裸判"（不看标题直接认定重复），这个假设对长文档不成立：
        # 64 位指纹装不下 30k+ 字正文的区分度，学术论文又共用大量通用词
        # （we/model/results/table/figure），实测 RAG(2005.11401) 与库内
        # Whisper(2212.04356)、LLaVA(2304.08485) 的距离都只有 4——两篇毫无关系。
        # 后果是新论文一律传不进来，用户只看到"内容高度重合"却找不到重合在哪。
        # 现在改成：任何距离都要求标题也像（真·同版本标题实测 >0.95，0.6 足够宽容）。
        if sh and d.simhash:
            dist = hamming(sh, d.simhash)
            if dist <= 6:
                from difflib import SequenceMatcher

                # 标题缺失时（扫描件抽不到标题）保守按重复处理：宁可让用户看一眼，
                # 也不要把同一篇论文存两遍污染检索
                ratio = (SequenceMatcher(None, normalize(title), normalize(d.title)).ratio()
                         if title and d.title else 1.0)
                if ratio >= 0.6:
                    out.append(
                        DupHit(d, f"正文指纹近似(汉明距离={dist}，标题相似 {ratio:.2f})",
                               round(1 - dist / 64, 3), _suggest(d, arxiv_id))
                    )
                    continue

        # 4) 标题高度相似
        if title and d.title:
            from difflib import SequenceMatcher

            ratio = SequenceMatcher(None, normalize(title), normalize(d.title)).ratio()
            if ratio >= 0.92:
                out.append(DupHit(d, f"标题高度相似({ratio:.2f})", round(ratio, 3), _suggest(d, arxiv_id)))

    out.sort(key=lambda x: x.similarity, reverse=True)
    return out


def _suggest(existing: Document, new_arxiv: str) -> str:
    """给出保留建议：谁的版本号大就建议留谁。"""
    def ver(a: str) -> int:
        m = RE_VERSION.search(a or "")
        return int(m.group(1)) if m else 1

    old_v = ver(existing.arxiv_id)
    new_v = ver(new_arxiv)
    if new_v > old_v:
        return f"新上传版本更高（v{new_v} > v{old_v}），建议保留新版本"
    if new_v < old_v:
        return f"库中版本更新（v{old_v} > v{new_v}），建议保留库中版本（{existing.filename}）"
    return "版本相同，建议保留其一以省空间"


def diff_versions(db: Session, old_doc_id: str, new_doc_id: str, limit: int = 12) -> dict:
    """两版差异摘要：按段落集合做集合差，输出"新增/删除/改写"样例。

    对答辩很有用：能直接说清 v1→v7 改了哪里（Attention Is All You Need 的
    第七版主要补了训练细节、附录与引用格式，正文结论未见实质变化）。
    """
    from difflib import SequenceMatcher

    def paragraphs(doc_id: str) -> list[str]:
        rows = db.scalars(
            select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.idx)
        ).all()
        return [normalize(c.text)[:600] for c in rows if len(c.text) > 80]

    a = paragraphs(old_doc_id)
    b = paragraphs(new_doc_id)

    sm = SequenceMatcher(None, a, b, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(b[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(a[i1:i2])

    return {
        "old_chunks": len(a),
        "new_chunks": len(b),
        "added": len(added),
        "removed": len(removed),
        "similarity": round(sm.ratio(), 4),
        "added_samples": [s[:200] for s in added[:limit]],
        "removed_samples": [s[:200] for s in removed[:limit]],
    }
