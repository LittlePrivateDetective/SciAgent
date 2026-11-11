"""锚点校验器的单元测试（`app/rag/anchors.verify`）。

这是全项目最该被单元测试罩住的一段：它决定"回答里的哪个数字/引用算幻觉"。
误判会冤枉模型（把真数字当编造），漏判会放过幻觉（把 28.4 写成 28.9）。
所以这里逐条钉住三类误报源：引号内的原文、arXiv 号、章节号与文件名。
"""
from __future__ import annotations

from app.rag.anchors import attach_anchors, verify

EV = [
    {
        "page": 5,
        "bbox": [312.0, 420.0, 468.0, 448.0],
        "text": "We trained on 8 NVIDIA P100 GPUs for 12 hours. BLEU 28.4 on WMT 2014 English-German.",
        "section_path": "5.3 Optimizer",
        "doc_name": "1706.03762v1.pdf",
        "anchor": "[p5@312,420,468,448]",
    },
    {
        "page": 8,
        "bbox": [100.0, 200.0, 500.0, 260.0],
        "text": "Table 2: BLEU 41.16 for the big model. arXiv:1706.03762v1",
        "section_path": "6.1 Training Data",
        "doc_name": "1706.03762v1.pdf",
        "anchor": "[p8@100,200,500,260]",
    },
]


def test_in_range_refs_pass():
    r = verify("用了 8 块 P100 [1]，BLEU 28.4 [1]。", EV)
    assert r["invalid_refs"] == []
    assert r["bad_numbers"] == []
    assert r["rate"] == 1.0


def test_out_of_range_ref_is_caught():
    r = verify("结论见 [3]。", EV)
    assert r["invalid_refs"] == [3]
    assert r["invalid"] == 1


def test_quoted_number_is_not_hallucination():
    """引号里是原文照抄：里面的 [18] 是论文自己的参考文献编号，不该算越界。"""
    r = verify('作者原话是 "as shown in [18], BLEU is 41.16"。', EV)
    assert r["invalid_refs"] == []


def test_arxiv_and_filename_are_not_numbers():
    """arXiv 号、文件名、章节号都是"数字长得像数值但不是答案"的坑。"""
    r = verify("见 arXiv:1706.03762v1 与 N19-1423.pdf，第 5.3 节。", EV)
    assert r["bad_numbers"] == []


def test_wrong_number_is_caught():
    """把 28.4 写成 28.9，必须被抓到——这是最隐蔽的一类幻觉。"""
    r = verify("BLEU 达到 28.9 [1]。", EV)
    assert "28.9" in r["bad_numbers"]


def test_number_from_question_is_allowed():
    """问题里自带的数字不算幻觉（用户说"8 块 GPU"，答案重复 8 是正常的）。"""
    r = verify("是的，用了 3 块 GPU [1]。", EV, question="用了 3 块还是 8 块 GPU？")
    assert r["bad_numbers"] == []


def test_fabricated_anchor_is_replaced():
    """模型自己编的坐标点不中任何证据 → 改成 [未核实]，不留假锚点。"""
    fake = "[p9@1,2,3,4]"
    r = verify(f"结论见 {fake}。", EV)
    assert r["invalid_anchors"] == [fake]
    assert "未核实" in r["cleaned"]
    assert fake not in r["cleaned"]


def test_attach_anchors_fills_coordinates():
    """模型只写 [1]，服务端负责把坐标补上——这是"坐标不进模型输出空间"的实现。"""
    out = attach_anchors("结论 [1]。", EV)
    assert "[p5@312,420,468,448]" in out


def test_empty_answer_is_safe():
    r = verify("", EV)
    assert r["total"] == 0 and r["rate"] == 0.0
