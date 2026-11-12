"""翻译层的纯函数：退化译文拦截、序号清洗、原样保留判定。

这些是"不调模型就能钉死"的部分。真正的译文质量要跑
`tests/trans_bench.py`（多模型对同一批论文章节，量保真度）。
"""
from __future__ import annotations

from app.rag.bilingual import (_clean_zh, _degenerate, detect_lang,
                               keep_verbatim)


def test_clean_zh_strips_leading_index():
    # 模型偶尔把输入序号带进译文
    assert _clean_zh("[1] Mistral 7B 采用滑动窗口注意力。").startswith("Mistral")
    assert _clean_zh("1. 该模型采用分组查询注意力。").startswith("该模型")
    # 正文里正常的编号不能动（"3.1 节"不是行首序号）
    assert _clean_zh("3.1 节的实验表明……").startswith("3.1")


def test_clean_zh_collapses_spaces():
    assert _clean_zh("注意力   机制") == "注意力 机制"


def test_degenerate_catches_stub_translation():
    # 实测事故：LayerNorm 第 5 页整段译文落库成 "5"
    src = ("As a result, for the same parameter update in the normalized model, "
           "the norm of the weight vector effectively controls the learning rate.")
    assert _degenerate(src, "5")
    assert _degenerate(src, "")


def test_degenerate_keeps_short_captions():
    # 标题/表注本来就短，不能被当成退化
    assert not _degenerate("Table 1: Model architecture.", "表 1：模型架构。")
    assert not _degenerate("Layer Normalization", "层归一化")
    # 原文也短时，不设长度门槛
    assert not _degenerate("Dropout", "丢弃法")


def test_keep_verbatim_formula_and_reference():
    assert keep_verbatim("reference", "Kiros et al. Skip-thought vectors. 2015.")
    assert keep_verbatim("formula", r"\sigma = \sqrt{\frac{1}{H}\sum (a-\mu)^2}")
    # 正文里出现一个行内公式，仍要翻
    assert not keep_verbatim(
        "text",
        "We normalize each feature by subtracting the mean and dividing by the "
        "standard deviation, which keeps the gradient scale stable during training.",
    )
    # 代码块（判据要求 ≥3 行且命中 ≥3 个代码特征）
    code = ("def forward(self, x):\n"
            "    h = self.linear(x)\n"
            "    return self.dropout(h)\n")
    assert keep_verbatim("text", code)


def test_detect_lang_mixed():
    assert detect_lang("Transformer 的自注意力机制") == "zh"
    assert detect_lang("Self-attention is all you need.") == "en"
    assert detect_lang("") == "en"


def test_keep_verbatim_prose_marked_as_formula():
    # 解析器把"夹着希腊字母的整句正文"标成 formula，这类要翻，不能原样保留
    prose = ("We used the Adam optimizer [18] with β1 = 0.9, β2 = 0.98 and ϵ = 10−9. "
             "We varied the learning rate over the course of training.")
    assert not keep_verbatim("formula", prose)
    # 真正的公式行仍然保留
    assert keep_verbatim("formula", "LN(x) = α ⊙ (x − μ) / σ + β")
    assert keep_verbatim("formula", "σl = sqrt(1/H * sum_i (a_i - mu)^2)")


def test_fix_caption_colon():
    from app.rag.bilingual import _fix_caption
    # 实测 qwen-flash 会漏掉冒号
    assert _fix_caption("图 2.1 线性回归模型。") == "图 2.1：线性回归模型。"
    assert _fix_caption("表3 实验结果。") == "表 3：实验结果。"
    # 已经有冒号的不动
    assert _fix_caption("图 2.1：线性回归模型。") == "图 2.1：线性回归模型。"
    # 不是图注的不动
    assert _fix_caption("如图所示，误差随层数增加。") == "如图所示，误差随层数增加。"
