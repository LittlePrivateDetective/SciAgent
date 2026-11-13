"""QA-P1-02：中文参考文献标题与编号条目。"""
from app.parse.structure import Block, RE_REF_HEAD, RE_REF_ITEM, classify, _extract_references


def test_chinese_reference_heading_and_items():
    assert RE_REF_HEAD.match("参考文献")
    assert RE_REF_HEAD.match(" 参考文献 ")
    assert RE_REF_ITEM.match("[139] HUANG Y. Factual consistency[J]. 2021.")
    blocks = [
        Block(1, 0, 0, "参考文献", (0, 0, 10, 10), size=12, bold=True),
        Block(1, 1, 0, "[1] 张三. 中文信息处理研究[J]. 中文信息学报, 2020: 1-8.", (0, 20, 100, 30)),
        Block(1, 2, 0, "续行：方法综述与实验分析。", (0, 32, 100, 38)),
        Block(1, 3, 0, "[2] LI S. Retrieval augmented generation[J]. Journal, 2024: 10-20.", (0, 40, 100, 50)),
        Block(1, 4, 0, "张三（2000—），教授，主要研究领域为自然语言处理。", (0, 55, 100, 65)),
    ]
    classified = classify(blocks, 10.0)
    refs = _extract_references(classified)
    assert [r["index"] for r in refs] == [1, 2]
    assert "中文信息处理研究" in refs[0]["text"]
    assert "续行" in refs[0]["text"]
    assert all("主要研究领域" not in r["text"] for r in refs)
