"""双栏感知的 PDF 解析器。

产出：带页码 + bbox 的文本块（citation-ready），供 RAG 检索与前端高亮回跳使用。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable

import pymupdf


@dataclass
class Block:
    page: int          # 1-based
    order: int         # 阅读顺序
    col: int           # 0=左栏/单栏, 1=右栏, -1=跨栏
    text: str
    bbox: tuple[float, float, float, float]
    kind: str          # body / header / footer

    def as_dict(self) -> dict:
        d = asdict(self)
        d["bbox"] = [round(v, 1) for v in self.bbox]
        return d


def _spans(page) -> list[tuple[tuple[float, float, float, float], str]]:
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if text:
                out.append((tuple(line["bbox"]), text))
    return out


def _classify(lines, width, height, margin=45.0):
    """按列归属与页眉页脚标记。"""
    mid = width / 2
    tol = width * 0.04
    rows = []
    for bbox, text in lines:
        x0, y0, x1, y1 = bbox
        if x0 < mid - tol and x1 > mid + tol:
            col = -1
        elif x1 <= mid + tol:
            col = 0
        else:
            col = 1
        if (y1 < margin and len(text) < 90) or (y0 > height - margin and len(text) < 90):
            kind = "header" if y1 < margin else "footer"
        else:
            kind = "body"
        rows.append((bbox, text, col, kind))
    return rows


def _reading_order(rows):
    """跨栏行充当分隔符，把页面切成段；段内先左栏后右栏，各自按 y 排序。"""
    rows = sorted(rows, key=lambda r: (r[0][1], r[0][0]))
    segments: list[list] = []
    current: list = []
    for r in rows:
        if r[2] == -1:
            if current:
                segments.append(current)
                current = []
            segments.append([r])
        else:
            current.append(r)
    if current:
        segments.append(current)

    ordered = []
    for seg in segments:
        order = {0: 0, 1: 1, -1: 0}
        for r in sorted(seg, key=lambda r: (order[r[2]], r[0][1], r[0][0])):
            ordered.append(r)
    return ordered


def parse_pdf(path: str, keep_header_footer: bool = False) -> list[Block]:
    doc = pymupdf.open(path)
    blocks: list[Block] = []
    n = 0
    for pno, page in enumerate(doc, start=1):
        rect = page.rect
        rows = _classify(_spans(page), rect.width, rect.height)
        for bbox, text, col, kind in _reading_order(rows):
            if kind != "body" and not keep_header_footer:
                continue
            blocks.append(Block(pno, n, col, text, bbox, kind))
            n += 1
    doc.close()
    return blocks


def merge_blocks(blocks: Iterable[Block], max_chars: int = 600) -> list[dict]:
    """把相邻同页文字块并成检索单元，保留合并后的包围盒。"""
    chunks: list[dict] = []
    buf: list[Block] = []
    size = 0

    def flush():
        nonlocal buf, size
        if not buf:
            return
        x0 = min(b.bbox[0] for b in buf)
        y0 = min(b.bbox[1] for b in buf)
        x1 = max(b.bbox[2] for b in buf)
        y1 = max(b.bbox[3] for b in buf)
        chunks.append({
            "page": buf[0].page,
            "order": buf[0].order,
            "col": buf[0].col,
            "text": "\n".join(b.text for b in buf),
            "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
        })
        buf, size = [], 0

    for b in blocks:
        if buf and (b.page != buf[-1].page or size + len(b.text) > max_chars):
            flush()
        buf.append(b)
        size += len(b.text)
    flush()
    return chunks


def to_markdown(chunks: list[dict]) -> str:
    """带锚点的可读文本，方便人工核对解析质量。"""
    lines = []
    for c in chunks:
        anchor = f"p{c['page']}@[{int(c['bbox'][0])},{int(c['bbox'][1])}]"
        lines.append(f"<!-- {anchor} -->\n{c['text']}\n")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    blocks = parse_pdf(sys.argv[1])
    chunks = merge_blocks(blocks)
    print(f"pages/blocks/chunks: {blocks[-1].page if blocks else 0}/{len(blocks)}/{len(chunks)}")
    print(to_markdown(chunks[:5]))
