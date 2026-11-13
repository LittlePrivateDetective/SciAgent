"""QA-P2-01/02/05：首页多行标题、内联摘要与中英文作者。"""
from app.parse.structure import Block, ParsedDoc, _extract_meta, _is_heading


def block(text, y, size=17.2, kind="text", x=0):
    return Block(1, 0, 0, text, (x, y, x + 200, y + 18), size=size, kind=kind)


def test_multiline_title_inline_abstract_and_authors():
    blocks = [block("ALBERT: A LITE BERT", 80), block("FOR SELF-SUPERVISED LEARNING", 80, x=210),
              block("Zhenzhong Lan & Mingda Chen", 130, size=10),
              block("Abstract We study parameter-efficient pretraining with cross-layer sharing.", 200, size=10),
              block("1 Introduction", 300, size=12, kind="heading")]
    d = ParsedDoc()
    _extract_meta(d, blocks, 10.0, [])
    assert d.title == "ALBERT: A LITE BERT FOR SELF-SUPERVISED LEARNING"
    assert d.authors == ["Zhenzhong Lan", "Mingda Chen"]
    assert d.abstract.startswith("We study")


def test_chinese_authors_and_inline_abstract():
    blocks = [block("大语言模型评测综述", 100), block("罗文1,王厚峰1", 140, size=11),
              block("摘 要:本文系统综述大语言模型评测方法、基准与挑战，并分析后续研究方向。", 190, size=9),
              block("关键词:大语言模型;评测", 230, size=9)]
    d = ParsedDoc()
    _extract_meta(d, blocks, 9.0, [])
    assert d.authors == ["罗文", "王厚峰"]
    assert d.abstract.startswith("本文系统综述")
    assert "关键词" not in d.abstract


def test_org_names_are_not_authors():
    from app.parse.structure import _names_from_block
    assert _names_from_block("Google Research")[0] == []
    assert _names_from_block("Toyota Technological")[0] == []
    assert _names_from_block("Łukasz Kaiser")[0] == ["Łukasz Kaiser"]


def test_homepage_author_and_org_are_not_headings():
    authors = block("Yuntao Bai, Saurabh Kadavath, Sandipan Kundu", 150, size=10)
    org = block("Anthropic", 180, size=10)
    assert not _is_heading(authors, 10)[0]
    assert not _is_heading(org, 10)[0]
