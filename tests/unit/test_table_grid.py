"""表格网格重建的单元测试（`app/parse/structure`）。

这组测试对应缺陷 D-68：解析器把表题判成 table 后，网格行因为是
"同字号 + 垂直相邻"被合并成段落块，**列归属在这一步丢失**，模型看到
`O(n2 · d)` 却不知道它属于 Self-Attention，于是回答"证据未给出"。

重建的输入是 `Line`（合并前、每格都带 bbox），所以这里用**坐标**造数据，
不依赖真实 PDF——真实 PDF 只作为验收脚本 `tests/table_recall.py` 的输入。
"""
from __future__ import annotations

from app.parse.structure import Line, _column_spans, _rebuild_tables, _cluster_rows, _table_grid, Block


def L(text: str, x0: float, x1: float, y: float, page: int = 1, h: float = 10.0) -> Line:
    return Line(page=page, bbox=(x0, y, x1, y + h), text=text, size=9.0, bold=False, font="")


def _as_cell(line: Line):
    from app.parse.structure import _Cell

    return _Cell(*line.bbox, line.text)


def _caption(text: str, y0: float = 60.0, y1: float = 70.0, x1: float = 500.0) -> Block:
    return Block(page=1, order=0, col=0, text=text, bbox=(100.0, y0, x1, y1), kind="table")


def _grid_lines(y: float = 90.0) -> list[Line]:
    """三行三列的小表，坐标照抄 Attention 论文 Table 1 的相对位置。"""
    out = []
    for i, (a, b, c) in enumerate(
        [
            ("Self-Attention", "O(n2 · d)", "O(1)"),
            ("Recurrent", "O(n · d2)", "O(n)"),
            ("Convolutional", "O(k · n · d2)", "O(1)"),
        ]
    ):
        yy = y + i * 11.0
        out.append(L(a, 124.5, 190.0, yy))
        out.append(L(b, 264.4, 302.9, yy))
        out.append(L(c, 351.1, 371.7, yy))
    return out


# ---------------------------------------------------------------- 行/列切分
def test_rows_merge_superscript_cells_into_same_row():
    """带上标的格子 bbox 顶边更高，仍必须与同行文字聚成一行。"""
    cells = [
        Line(1, (124.5, 140.1, 181.6, 149.3), "Self-Attention", 9.0, False, ""),   # 普通文字
        Line(1, (264.4, 136.1, 302.9, 145.3), "O(n2 · d)", 9.0, False, ""),      # 上标抬高 4pt
    ]
    rows = _cluster_rows([_as_cell(c) for c in cells])
    assert len(rows) == 1
    assert len(rows[0]["cells"]) == 2


def test_rows_keep_tightly_spaced_text_lines_apart():
    """行距 1.8pt 的相邻两行不能并成一行（后者是正文，不是表格）。"""
    cells = [
        Line(1, (108.0, 100.0, 504.0, 109.2), "第一行正文", 9.0, False, ""),
        Line(1, (108.0, 111.0, 504.0, 120.2), "第二行正文", 9.0, False, ""),
    ]
    assert len(_cluster_rows([_as_cell(c) for c in cells])) == 2


def test_column_spans_split_on_whitespace():
    """列缝靠"没有任何格子覆盖的空白"切分，不依赖 x 坐标聚类阈值。"""
    cells = [
        Line(1, (124.5, 100, 181.6, 110), "a", 9, False, ""),
        Line(1, (264.4, 100, 302.9, 110), "b", 9, False, ""),
        Line(1, (351.1, 100, 371.7, 110), "c", 9, False, ""),
    ]
    spans = _column_spans([_as_cell(c) for c in cells])
    assert len(spans) == 3


# ---------------------------------------------------------------- 端到端重建
def test_grid_renders_one_row_per_line():
    cap = _caption("Table 1: Maximum path lengths, per-layer complexity …")
    body, box = _table_grid(cap, _grid_lines(), 612.0)
    lines = body.split("\n")
    assert len(lines) == 3
    assert lines[0] == "Self-Attention | O(n2 · d) | O(1)"
    assert lines[2].startswith("Convolutional |")
    assert box[1] < box[3]


def test_wrapped_header_is_joined_into_previous_row():
    """表头折行（"Sequential" 下面一行只有 "Operations"）要并进上一行，
    不能单独占一行，否则列名与列错位。"""
    cap = _caption("Table 1: caption")
    lines = [
        L("Layer Type", 124.5, 169.9, 80.0),
        L("Complexity", 239.7, 327.5, 80.0),
        L("Sequential", 340.3, 382.4, 80.0),
        L("Operations", 339.5, 383.2, 91.0),          # 折行，只有一格
        L("Self-Attention", 124.5, 181.6, 103.0),
        L("O(n2 · d)", 264.4, 302.9, 103.0),
        L("O(1)", 351.1, 371.7, 103.0),
    ]
    body, _ = _table_grid(cap, lines, 612.0)
    assert "Sequential Operations" in body
    assert body.count("\n") == 1                    # 表头 + 一行数据


def test_paragraph_below_table_is_not_swallowed():
    """表格下方隔一段空白就是正文，不能把它当网格继续吃。"""
    cap = _caption("Table 1: caption")
    lines = _grid_lines(90.0) + [
        L("As noted in Table 1, a self-attention layer connects all positions", 108.0, 504.0, 160.0),
        L("with a constant number of sequentially executed operations.", 108.0, 400.0, 171.0),
    ]
    body, box = _table_grid(cap, lines, 612.0)
    assert "As noted" not in body
    assert box[3] < 160


def test_table_without_grid_returns_none():
    """只有表题、下面直接是正文 → 不重建（宁可保留表题，也不能瞎吃）。"""
    cap = _caption("Table 2 summarizes our results and compares our translation quality …")
    lines = [
        L("architectures from the literature. We estimate the number of floating point", 108.0, 504.0, 80.0),
        L("operations used to train a model by multiplying the training time.", 108.0, 504.0, 91.0),
    ]
    assert _table_grid(cap, lines, 612.0) is None


def test_rebuild_removes_consumed_blocks_and_extends_bbox():
    """重建后：表题块带上网格文本、bbox 扩到整张表，网格块从文档里消失。"""
    cap = _caption("Table 1: caption")
    grid_lines = _grid_lines(90.0)
    stray = Block(page=1, order=1, col=0, text="Self-Attention O(n2 · d) O(1)", bbox=(124.5, 90.0, 371.7, 100.0))
    blocks = [cap, stray]
    out = _rebuild_tables(blocks, {1: grid_lines}, {1: 612.0})
    assert len(out) == 1 and out[0] is cap
    assert "Self-Attention | O(n2 · d) | O(1)" in cap.text
    assert cap.bbox[3] > 90.0
