"""分词与数值搬运的单元测试。

分词看着琐碎，但它决定了"问数字能不能检索到"：
- `GPUs` 必须能被 `gpu` 命中（复数归并）；
- `28.4` 必须是一个整体，不能被切成 `28` 和 `4`（否则 BLEU 分数永远匹配不上）；
- `β2`、`d_model` 这类符号要保留，否则超参题全军覆没。
"""
from __future__ import annotations

from app.rag.hybrid import tokenize
from app.tools.chart import _g, _num


# ---------------------------------------------------------------- 分词
def test_english_plural_collapses():
    toks = tokenize("8 GPUs are used")
    assert "gpu" in toks and "8" in toks


def test_decimal_stays_whole():
    toks = tokenize("BLEU 28.4 on WMT 2014")
    assert "28.4" in toks
    assert "28" not in toks


def test_chinese_is_tokenized():
    toks = tokenize("多头注意力")
    assert any(t in toks for t in ("多头", "注意力", "多头注意力"))


def test_zeros_not_prefixed():
    """0.98、β2=0.98 这类超参里的数字要能单独命中。"""
    assert "0.98" in tokenize("beta2 = 0.98")


def test_symbols_are_lowered():
    assert "bleu" in tokenize("BLEU score")


# ---------------------------------------------------------------- 数值搬运
def test_num_extracts_number():
    assert _num("28.4") == 28.4
    assert _num("34.5%") == 34.5
    assert _num("") == 0.0


def test_g_keeps_trailing_zero():
    """表格里写 41.0，生成的代码里也必须是 41.0——这是"逐格可核对"的前提。"""
    assert _g("41.0") == "41.0"
    assert _g("41") == "41"


def test_g_strips_unit_for_data_literal():
    """带单位的单元格：数据字面量只留数值，单位交给坐标轴标签／图注去说明。

    早期有人在 28.4% 这种格子上直接原样塞进代码，生成的 pgfplots 会多出一个
    '%'（LaTeX 注释符），整段编译失败——所以这里刻意剥掉单位。
    """
    assert _g("28.4%") == "28.4"
    assert _g("1,000") == "1"      # 千分位不当数字处理，按 _num 取首个数
    assert _g("N/A") == "0"
