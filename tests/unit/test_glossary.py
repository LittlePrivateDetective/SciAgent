"""术语表与公式判据的单测。

这两块都只有一个失败模式：**判错方向**。术语表错 = 把正常句子改坏；
公式判据错 = 该翻的正文没翻（用户看到"这一页没翻"）。所以用例都取自真实段落，
而不是构造的理想句子。
"""
from __future__ import annotations

from app.parse.structure import looks_formula_text
from app.rag import glossary


def test_glossary_loaded():
    terms = glossary.load()
    assert len(terms) > 50, "术语表应当覆盖论文里的常用术语"
    en = {t.en.lower() for t in terms}
    for must in ("self-attention", "layer normalization", "retrieval-augmented generation"):
        assert must in en


def test_for_text_only_picks_present_terms():
    ts = glossary.for_text("We apply layer normalization before every sub-layer.")
    names = {t.en for t in ts}
    assert "layer normalization" in names
    # 没出现的术语不该被塞进来（否则每批都多花 token，还诱导模型堆术语）
    assert "diffusion model" not in names


def test_for_text_respects_limit():
    long_text = " ".join(t.en for t in glossary.load())
    assert len(glossary.for_text(long_text, limit=5)) <= 5


def test_for_text_longest_wins():
    # self-attention 内部含 attention，不能被拆成两条注入
    ts = glossary.for_text("self-attention is all you need")
    assert [t.en for t in ts] == ["self-attention"]


def test_prompt_block_marks_keep_en():
    ts = glossary.for_text("We use LoRA and attention head") or []
    block = glossary.prompt_block(list(ts))
    assert "LoRA" in block
    if any(t.keep_en for t in ts):
        assert "保留英文原词" in block
    assert glossary.prompt_block([]) == ""


def test_compliance_flags_inconsistent_terms():
    pairs = [
        ("layer normalization is applied", "我们使用层归一化"),
        ("another layer normalization", "另一处用了分层归一"),   # 没用标准译名
        ("and layer normalization again", "再次层归一化"),
        ("yet another layer normalization", "还有分层归一"),
    ]
    bad = glossary.compliance(pairs)
    assert any(b["en"] == "layer normalization" for b in bad)
    assert all(b["rate"] < 0.6 for b in bad)


def test_compliance_ignores_single_occurrence():
    # 只出现一次时无法判断"一致不一致"，不该报
    assert glossary.compliance([("layer normalization", "层归一化")]) == []


# ---------------------------------------------------------------- 公式判据
def test_formula_detects_greek_fragments():
    # 上一轮翻译体检里被判成"正文"的碎片：希腊字母 χ/φ 不在旧的符号表里
    assert looks_formula_text("χi χi j Cov[yi, yj | x] φ2")
    assert looks_formula_text("LN(x) = α ⊙ (x − μ) / σ + β")
    assert looks_formula_text("σl = sqrt(1/H * sum_i (a_i - mu)^2)")


def test_formula_keeps_prose():
    prose = ("We used the Adam optimizer [18] with β1 = 0.9, β2 = 0.98 and ϵ = 10−9. "
             "We varied the learning rate over the course of training.")
    assert not looks_formula_text(prose)
    assert not looks_formula_text("The model achieves 28.4 BLEU on WMT 2014.")
    assert not looks_formula_text("Figure 2: The Transformer architecture.")


def test_formula_does_not_swallow_sentences_with_colon():
    # 以冒号收尾的正文段（后面紧跟公式）不能被整段吞成公式，否则翻译时原样保留英文
    assert not looks_formula_text(
        "Now let's perform some algebraic manipulations on the loss function. We seek:")
