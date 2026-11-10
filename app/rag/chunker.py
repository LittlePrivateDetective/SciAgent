"""结构化切块：把解析出的区块并成"可引用"的检索单元。

三条规则（直接决定溯源精度）：
1. 标题是硬边界——不跨越章节合并，且把章节路径写进 chunk 文本前缀，让检索能吃到上下文；
2. 图表/表格/公式/参考文献条目独立成块——它们本身就是被引用的对象，不该被正文淹没；
3. 同页内按字数窗口合并，保留**合并包围盒**（多个区块 bbox 的并集），前端据此画高亮框。
"""
from __future__ import annotations

import re

from app.core.config import settings
from app.parse.structure import Block, ParsedDoc, build_section_paths


def _union_bbox(blocks: list[Block]) -> list[float]:
    x0 = min(b.bbox[0] for b in blocks)
    y0 = min(b.bbox[1] for b in blocks)
    x1 = max(b.bbox[2] for b in blocks)
    y1 = max(b.bbox[3] for b in blocks)
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def _tail_overlap(text: str, chars: int) -> str:
    """取尾部若干字符做重叠，避免答案恰好被切在句子中间。"""
    if chars <= 0 or len(text) <= chars:
        return ""
    tail = text[-chars:]
    m = re.search(r"[.。!?！？]\s", tail)
    return tail[m.end() :] if m else tail


def build_chunks(parsed: ParsedDoc, max_chars: int | None = None, overlap: int | None = None) -> list[dict]:
    max_chars = max_chars or settings.chunk_max_chars
    overlap = settings.chunk_overlap if overlap is None else overlap

    section_paths = build_section_paths(parsed.blocks)
    chunks: list[dict] = []

    buf: list[Block] = []
    size = 0
    carry = ""  # 上一块尾巴，作为本块的重叠前缀

    def flush() -> None:
        nonlocal buf, size, carry
        if not buf:
            return
        body = "\n".join(b.text for b in buf).strip()
        if carry:
            body = f"{carry}\n{body}"
        if len(body) < 15:
            buf, size, carry = [], 0, ""
            return

        sec = section_paths.get(buf[0].order, "")
        # 只存正文：章节路径单独放 section_path 字段。
        # 之前把路径拼进正文，导致 "References/Attention Visualizations" 这类
        # 高频词污染 BM25，检索被图表页抢走。
        chunks.append(
            {
                "page": buf[0].page,
                "bbox": _union_bbox(buf),
                "text": body,
                "section_path": sec,
                "kind": buf[0].kind if len({b.kind for b in buf}) == 1 else "text",
                "char_len": len(body),
            }
        )
        carry = _tail_overlap(body, overlap)
        buf, size = [], 0

    for b in parsed.blocks:
        txt = b.text.strip()
        if not txt:
            continue

        # 规则 1：标题硬边界（标题本身不进正文块，但章节路径已记录）
        if b.kind == "heading":
            flush()
            continue

        # 规则 2：图表/公式/参考文献独立成块
        if b.kind in {"table", "figure", "formula", "reference"}:
            flush()
            sec = section_paths.get(b.order, "")
            chunks.append(
                {
                    "page": b.page,
                    "bbox": [round(v, 1) for v in b.bbox],
                    "text": txt,
                    "section_path": sec,
                    "kind": b.kind,
                    "char_len": len(txt),
                }
            )
            continue

        # 规则 3：窗口合并
        if buf and (b.page != buf[-1].page or size + len(txt) > max_chars):
            flush()
        buf.append(b)
        size += len(txt)

    flush()
    return chunks
