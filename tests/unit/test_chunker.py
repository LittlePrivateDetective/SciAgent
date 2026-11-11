"""切块器的单元测试（`app/rag/chunker`）。

重点罩住两件事：
1. `strip_carry` —— 重叠前缀必须剥干净，否则"证据文本里有、高亮框里没有"（曾经的 P0）；
2. 表格吸收 —— 解析器只把表题认成 table，网格行是独立单行块，必须被并进表格块，
   否则问"复杂度/超参/BLEU"时表格永远召回不到（缺陷 D-67）。
"""
from __future__ import annotations

from app.parse.structure import Block, ParsedDoc
from app.rag.chunker import _is_grid_fragment, _looks_like_grid, build_chunks, strip_carry


def B(text: str, page: int = 1, order: int = 0, kind: str = "text",
      bbox=(100.0, 100.0, 500.0, 120.0)) -> Block:
    return Block(page=page, order=order, col=0, text=text, bbox=bbox, kind=kind)


# ---------------------------------------------------------------- 重叠前缀
def test_strip_carry_removes_exact_overlap():
    prev = "……作为对比，self-attention layer connects all positions."
    # 重叠要超过 12 个字符才会被剥掉：短重叠（"positions."）因为歧义大，
    # 宁可留着也不误删正文——这是刻意的阈值，不是漏判
    cur = "connects all positions. A recurrent layer requires O(n) operations."
    out = strip_carry(prev, cur)
    assert out == "A recurrent layer requires O(n) operations."


def test_strip_carry_keeps_text_when_no_overlap():
    assert strip_carry("abc", "完全不相关的内容") == "完全不相关的内容"


def test_strip_carry_handles_empty():
    assert strip_carry("", "x") == "x"
    assert strip_carry("x", "") == ""


# ---------------------------------------------------------------- 网格判定
def test_grid_fragment_accepts_table_rows():
    assert _is_grid_fragment("Layer Type")
    assert _is_grid_fragment("Complexity per Layer")
    assert _is_grid_fragment("O(n2 · d)")


def test_grid_fragment_rejects_prose():
    assert not _is_grid_fragment("This section describes the training regime for our models.")
    assert not _is_grid_fragment("这一整段话是一个完整句子，以句号结尾。")
    # 太长的一行基本是正文
    assert not _is_grid_fragment("We " + "very " * 20 + "long sentence")


def test_multi_line_grid_detected():
    grid = "Layer Type\nSelf-Attention\nRecurrent\nConvolutional\nComplexity per Layer"
    assert _looks_like_grid(grid)


def test_multi_line_prose_rejected():
    prose = ("Motivating our use of self-attention we consider three desiderata. "
             "One is the total computational complexity per layer. "
             "Another is the amount of computation that can be parallelized. "
             "The third is the path length between long-range dependencies.")
    assert not _looks_like_grid(prose)


# ---------------------------------------------------------------- 表格吸收
def _doc_with_table() -> ParsedDoc:
    blocks = [
        B("3.2 Multi-Head Attention", order=0, kind="heading"),
        B("Table 1: Maximum path lengths, per-layer complexity and minimum number of "
          "sequential operations for different layer types.", order=1, kind="table",
          bbox=(107.0, 71.0, 504.0, 103.0)),
        B("Layer Type", order=2, bbox=(107.0, 105.0, 160.0, 118.0)),
        B("Maximum Path Length Operations Self-Attention", order=3, bbox=(170.0, 105.0, 400.0, 118.0)),
        B("Complexity per Layer", order=4, bbox=(107.0, 120.0, 200.0, 133.0)),
        B("O(n2 · d)", order=5, bbox=(300.0, 120.0, 360.0, 133.0)),
        B("Motivating our use of self-attention we consider three desiderata. "
          "One is the total computational complexity per layer.", order=6, bbox=(107.0, 150.0, 504.0, 190.0)),
    ]
    return ParsedDoc(page_count=1, char_count=1000, blocks=blocks)


def test_table_absorbs_grid_rows():
    chunks = build_chunks(_doc_with_table())
    tables = [c for c in chunks if c["kind"] == "table"]
    assert len(tables) == 1
    text = tables[0]["text"]
    assert "Table 1" in text
    # 网格行必须进来，这正是 D-67 的修法
    assert "Complexity per Layer" in text
    assert "O(n2 · d)" in text


def test_table_absorbs_bbox_is_union():
    chunks = build_chunks(_doc_with_table())
    t = [c for c in chunks if c["kind"] == "table"][0]
    x0, y0, x1, y1 = t["bbox"]
    assert y0 <= 71.0 and y1 >= 133.0          # 覆盖到吸收进来的最后一行
    assert x0 <= 107.0 and x1 >= 400.0


def test_table_stops_at_prose():
    chunks = build_chunks(_doc_with_table())
    t = [c for c in chunks if c["kind"] == "table"][0]
    assert "Motivating our use" not in t["text"]
    # 后面的正文仍然单独成块，没有被表格吞掉
    assert any("Motivating our use" in c["text"] for c in chunks if c["kind"] == "text")


def test_heading_is_hard_boundary():
    chunks = build_chunks(_doc_with_table())
    assert all("3.2 Multi-Head Attention" not in c["text"].split("\n")[0] for c in chunks
               if c["kind"] == "table")
