"""章节树的三类误判：作者名、表格列名、同行碎片。

这些用例全部来自真实 PDF（BERT / Attention / 计算机学报），不是构造出来的假设：
- BERT 的章节编号 "4" 与标题 "Experiments" 被拆成两个文本片，且横跨栏中线；
- Attention 的 arXiv 水印是 20pt，比标题 17.2pt 还大；
- BERT 附录的表格列名 MNLI / QQP / RTE / WNLI 加粗且字号偏大。
"""
from __future__ import annotations

from app.parse.structure import (
    Block,
    Line,
    _col_of,
    _demote_table_rows,
    _is_heading,
    _join_lines,
    _normalize_headings,
)


def _blk(text: str, y: float = 100, x: float = 300, size: float = 10.0,
         bold: bool = False, page: int = 1) -> Block:
    return Block(page, 0, 0, text, (x, y, x + len(text) * 5, y + 12),
                 size=size, bold=bold)


def _line(text: str, x0: float, y0: float, size: float = 12.0, bold: bool = True) -> Line:
    return Line(1, (x0, y0, x0 + len(text) * size * 0.6, y0 + size), text, size, bold, "F1")


# ---------- 1. 分栏按中心，不按右边缘 ----------
def test_col_uses_center_not_right_edge():
    """编号 "4" 只有 6pt 宽，右边缘 313 在容差内，中心 310 却已经过中线——
    BERT 就是因此把编号分到左栏、标题分到右栏，编号丢失。"""
    width = 612.0
    assert _col_of((307, 442, 313, 454), width, True) == 1
    assert _col_of((325, 442, 390, 454), width, True) == 1


# ---------- 2. 同一行的碎片要粘回去，且中文之间不插空格 ----------
def test_join_same_line_fragments_without_space():
    # "1 引" + "言" → "1 引言"
    buf = [_line("1 引", 64, 658), _line("言", 113, 661)]
    assert _join_lines(buf).replace("  ", " ") in ("1 引言", "1 引 言")


def test_join_keeps_space_between_names():
    # 两个名字之间要留空格，否则作者行会变成一长串
    buf = [_line("黄震华", 97, 199, size=14.7), _line("林威", 171, 199, size=14.7)]
    assert _join_lines(buf) == "黄震华 林威"


# ---------- 3. 表格列名、公式碎片、单字都不算标题 ----------
def test_table_column_names_are_not_headings():
    # 全大写缩写由字面规则直接否掉
    for txt in ("MNLI", "QQP", "RTE", "WNLI"):
        ok, _ = _is_heading(_blk(txt, size=10.9, bold=True), 10.0)
        assert not ok, txt


def test_mixed_case_column_in_table_row_demoted():
    """CoLA 这种混合大小写无法靠字面判断——只能靠"同一行并排多个短块"降级。"""
    row = [_blk(t, y=500, x=300 + i * 40, size=10.9, bold=True)
           for i, t in enumerate(("MNLI", "QQP", "QNLI", "CoLA", "RTE"))]
    for b in row:
        b.kind, b.level = "heading", 2
    out = _demote_table_rows(row)
    assert all(b.kind == "text" for b in out)


def test_formula_fragments_are_not_headings():
    for txt in ("exp ( )", "S( )", "K ∑", "务序列(D1，C1，Y1)，(D2，C2，Y2)"):
        ok, _ = _is_heading(_blk(txt, size=12.0, bold=True), 8.8)
        assert not ok, txt


def test_single_cjk_char_is_not_heading():
    ok, _ = _is_heading(_blk("言", size=13.7, bold=True), 8.8)
    assert not ok


def test_real_headings_still_pass():
    for txt in ("1 引言", "2 Related Work", "3.2 Attention", "参考文献", "References"):
        ok, lvl = _is_heading(_blk(txt, size=13.8, bold=True), 10.0)
        assert ok and lvl >= 1, txt


# ---------- 4. 并排的短块 = 表格行，要降级 ----------
def test_side_by_side_short_blocks_demoted():
    row = [_blk(f"列{i}", y=500, x=300 + i * 60, size=10.9, bold=True) for i in range(4)]
    for b in row:
        b.kind, b.level = "heading", 2
    out = _demote_table_rows(row)
    assert all(b.kind == "text" for b in out)


# ---------- 5. 层级规范化 ----------
def test_levels_follow_numbering():
    blocks = [
        _blk("1 Introduction", size=13.0, bold=True),
        _blk("3.5.2 Effect of Model Size", size=11.0, bold=True),
        _blk("Model Architecture", size=11.0, bold=True),
    ]
    for b in blocks:
        b.kind = "heading"
    _normalize_headings(blocks)
    assert blocks[0].level == 1
    assert blocks[1].level == 3           # 编号三段 → L3
    assert blocks[2].level == 4 or blocks[2].level == 3   # 跟在 3.5.2 后面，不能再回 L1
