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


def strip_carry(prev: str, cur: str, limit: int = 200) -> str:
    """去掉分块时的重叠前缀，返回**严格属于本块 bbox** 的正文。

    切块为了"答案不被切在句子中间"，会把上一块的尾巴贴到下一块开头。
    检索时这是优点，但 `Chunk.bbox` 只覆盖本块的区块——于是
    `chunk.text` 里有一段文字根本不在锚点框内（QA A04 实测溯源只有 1/10）。
    凡是要把 chunk 文本当作"原文证据"展示的地方，都必须先过这里：
    混合检索语料、中英对照段落、答案引用校验。

    用"上一块结尾 == 本块开头"精确还原，比任何启发式都可靠，
    因为那段文字本来就是从上块末尾复制过来的。
    """
    if not prev or not cur:
        return cur
    best = 0
    for k in range(min(limit, len(cur)), 12, -1):
        if prev.endswith(cur[:k]):
            best = k
            break
    if not best:
        return cur
    rest = cur[best:].lstrip("\n ")
    # 重叠恰好卡在词中间时，rest 会以半截单词开头（如 "ned from ..."），
    # 丢掉这个残词：它在前一段已经完整出现过。
    m = re.match(r"^([a-z]{1,6})\s+", rest)
    if m and prev.endswith(m.group(1)):
        rest = rest[m.end():]
    return rest or cur


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


def _is_grid_fragment(txt: str) -> bool:
    """单行网格片段：短、不以句末标点结尾、不含完整句子。

    解析器会把表格网格拆成很多**单行区块**（"Layer Type"、"Complexity per Layer"、
    "O(n²·d)"），单独看每一行都不像表格，只有连起来才是。所以判据放在"行"这一级：
    不超过 60 字符、不以 . 。 ? ! : 结尾，就认为可能是网格的一行。
    正文段落几乎不可能满足——它们要么长、要么带句号。
    """
    t = txt.strip()
    if not t or "\n" in t or len(t) > 60:
        return False
    if t[-1] in ".。?!:：":
        return False
    return True


def _looks_like_grid(txt: str) -> bool:
    """判断一段文字是不是"表格网格"而不是正文段落。

    为什么需要：解析器只把**表题**（"Table 1: Maximum path lengths…"）认成 table，
    紧随其后的网格行（"Layer Type / Complexity per Layer / Self-Attention / O(n²·d)"）
    因为字号与正文一致，被当成了普通正文块。于是表格块里只有一句标题、
    真正的数值在隔壁的 text 块里——问"计算复杂度是多少"时，两路召回都答不上来
    （实测 Table 1 排第 55 名，缺陷 D-67）。

    判据是"短行占比高 + 几乎没有句末标点"：表格行通常 ≤40 字符，
    且不会以 ". " 结尾；正文段落正好相反。
    """
    lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
    if len(lines) < 4:
        return False
    short = sum(1 for ln in lines if len(ln) <= 40)
    ends = txt.count(". ") + txt.count("。") + txt.count("? ") + txt.count("! ")
    return short / len(lines) >= 0.6 and ends <= max(1, len(lines) // 6)


def _render_grid(blocks: list[Block]) -> str:
    """把表格块 + 网格块渲染成"按行对齐"的文本。

    为什么不能直接按阅读顺序拼：网格行被切成一个个单行块后，阅读顺序是
    "先左栏后右栏"，拼出来是

        Layer Type / Maximum Path Length Operations Self-Attention /
        Complexity per Layer / O(n2 · d) / O(1) Recurrent / O(1) ...

    列名和数值交错，模型读不出 "O(n2 · d)" 属于 Self-Attention 那一行，
    于是明明有答案也说"证据未给出"。

    这里用 bbox 的 y 中心把同一行的格子聚在一起，行内按 x 排序，用 " | " 分隔：

        Self-Attention | O(n2 · d) | O(1) | O(1)

    这不是完整的表格结构还原（没有合并单元格、没有跨页续表的合并），
    但足以让模型把"哪一列属于哪一行"对上。第一块是表题，单独保留。
    """
    if not blocks:
        return ""
    head = blocks[0].text.strip()
    cells = blocks[1:]
    if not cells:
        return head

    rows: list[dict] = []
    for b in cells:
        t = b.text.strip()
        if not t:
            continue
        if "\n" in t:                      # 多行网格块：原样成行，不参与对齐
            for ln in t.split("\n"):
                if ln.strip():
                    rows.append({"y": None, "x": 0.0, "t": ln.strip()})
            continue
        yc = (b.bbox[1] + b.bbox[3]) / 2
        for r in rows:
            if r["y"] is not None and abs(r["y"] - yc) <= 6:
                r["cells"].append((b.bbox[0], t))
                break
        else:
            rows.append({"y": yc, "x": b.bbox[0], "t": t, "cells": [(b.bbox[0], t)]})

    lines = [head]
    for r in rows:
        if r["y"] is None:
            lines.append(r["t"])
        else:
            cells_sorted = [t for _, t in sorted(r["cells"], key=lambda c: c[0])]
            lines.append(" | ".join(cells_sorted))
    return "\n".join(lines)


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

    blocks = parsed.blocks
    i = 0
    while i < len(blocks):
        b = blocks[i]
        txt = b.text.strip()
        if not txt:
            i += 1
            continue

        # 规则 1：标题硬边界（标题本身不进正文块，但章节路径已记录）
        if b.kind == "heading":
            flush()
            i += 1
            continue

        # 规则 2：图表/公式/参考文献独立成块
        if b.kind in {"table", "figure", "formula", "reference"}:
            flush()
            sec = section_paths.get(b.order, "")
            body = txt
            box = [round(v, 1) for v in b.bbox]

            # 表格：把紧随其后的网格行吃进来，并按 y 坐标重排成行。
            # 只吃同页、同类型的正文块，且累计不超过 1800 字——
            # 超过这个量通常已经把下一段论述也拖进来了。
            if b.kind == "table":
                grid = [b]
                j = i + 1
                taken = 0
                while j < len(blocks) and taken < 40:
                    nb = blocks[j]
                    ntxt = nb.text.strip()
                    if not ntxt or nb.page != b.page or nb.kind != "text":
                        break
                    if sum(len(g.text) for g in grid) + len(ntxt) > 1800:
                        break
                    if not (_looks_like_grid(ntxt) or _is_grid_fragment(ntxt)):
                        break
                    grid.append(nb)
                    box = [
                        min(box[0], round(nb.bbox[0], 1)),
                        min(box[1], round(nb.bbox[1], 1)),
                        max(box[2], round(nb.bbox[2], 1)),
                        max(box[3], round(nb.bbox[3], 1)),
                    ]
                    taken += 1
                    i = j
                    j += 1
                body = _render_grid(grid)

            chunks.append(
                {
                    "page": b.page,
                    "bbox": box,
                    "text": body,
                    "section_path": sec,
                    "kind": b.kind,
                    "char_len": len(body),
                }
            )
            i += 1
            continue

        # 规则 3：窗口合并
        if buf and (b.page != buf[-1].page or size + len(txt) > max_chars):
            flush()
        buf.append(b)
        size += len(txt)
        i += 1

    flush()
    return chunks
