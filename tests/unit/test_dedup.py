"""去重算法的确定性部分：归一化、指纹、同族判定、阈值常量。

这里只钉"不该随环境变化"的东西——纯函数的行为、阈值之间的相对关系。
真正的判准（同篇该判重、不同篇该放行）依赖真实论文，放在
`tests/dedup_matrix.py` 里跑，因为它要解析 38 篇 PDF，不适合进 40 条单测。
"""
from __future__ import annotations

import random

from app.rag.dedup import (SIMHASH_MAX_DIST, TITLE_NEAR, TITLE_SAME, content_hash,
                           family_key, hamming, normalize, simhash64)


def _paper_text(n_para: int = 400, seed: int = 0) -> str:
    """造一段像论文正文的长文本：高频通用词 + 领域词。"""
    rnd = random.Random(seed)
    common = ("we model results table figure show that the of and in is are "
              "training data performance baseline experiment approach").split()
    domain = ("attention retrieval embedding transformer token retrieval corpus "
              "query document rerank passage").split()
    paras = []
    for _ in range(n_para):
        n = rnd.randint(14, 26)
        words = [rnd.choice(common if rnd.random() < 0.6 else domain) for _ in range(n)]
        paras.append(" ".join(words) + ".")
    return "\n".join(paras)


def test_normalize_drops_digits_and_punct():
    assert normalize("Attention Is All You Need!") == normalize("attention   is all you need")
    # 数字被抹掉：页码、年份、编号不该影响同文判定
    assert normalize("v1 page 12") == normalize("v7 page 99")


def test_content_hash_ignores_layout_digits():
    a = "We propose a model. 12 pages, 2017."
    b = "we   propose a model. 99 pages, 2021."
    assert content_hash(a) == content_hash(b)
    # 但真改了词就必须不同
    assert content_hash(a) != content_hash(a.replace("propose", "reject"))


def test_simhash_stable_and_small_edits_close():
    text = _paper_text()
    assert simhash64(text) == simhash64(text)          # 确定性
    # 删掉 10% 段落仍应落在阈值内（版式差异/修订不该破坏判重）
    paras = text.split("\n")
    rnd = random.Random(7)
    kept = [p for p in paras if rnd.random() > 0.10]
    assert hamming(simhash64(text), simhash64("\n".join(kept))) <= SIMHASH_MAX_DIST


def test_simhash_returns_zero_for_empty():
    assert simhash64("") == 0
    assert simhash64("...!!!") == 0        # 没有任何字母/汉字


def test_hamming_is_symmetric_and_masked():
    a, b = simhash64(_paper_text(seed=1)), simhash64(_paper_text(seed=2))
    assert hamming(a, b) == hamming(b, a)
    assert 0 <= hamming(a, b) <= 64
    # 有符号表示（SQLite 友好）不影响比较
    assert hamming(-1, 0) == 64


def test_family_key_normalizes_arxiv_version():
    base = family_key("1706.03762v1", "", "")
    assert base == "arxiv:1706.03762"
    assert family_key("1706.03762v7", "", "") == base
    assert family_key("1706.03762", "", "") == base


def test_family_key_uses_doi_then_title():
    assert family_key("", "Whatever", "see 10.1145/3292500.3330701 for details") \
        == "doi:10.1145/3292500.3330701"
    # 没有编号时退回标题哈希；同一标题的不同写法要归一
    a = family_key("", "Attention Is All You Need", "")
    b = family_key("", "attention is all you need!", "")
    assert a == b and a.startswith("title:")


def test_family_key_rejects_filename_like_titles():
    # 文件名式"标题"不能当同族依据，否则 paper_v1 / paper_v2 会被判成一族
    assert family_key("", "1706.03762v1_img", "") == ""
    assert family_key("", "paper_v2", "") == ""
    # 太短的标题也不可靠
    assert family_key("", "BERT", "") == ""


def test_thresholds_are_sane():
    # 门槛必须明显高于"不同论文碰巧像"的量级，又要低于"同篇加副标题"的水平。
    # 这两条是 tests/dedup_matrix.py 用 40 篇真实论文量出来的，改动阈值时先看那个脚本。
    assert 0.6 < TITLE_NEAR < TITLE_SAME <= 1.0
    assert 0 < SIMHASH_MAX_DIST < 16
