"""作者枚举题：确定性作答与核对。

用例全部来自 2026-11-14 独立评审的 D-QA03：官方样例问 Attention v1 的
「第 6 作者是谁及机构」，系统答成了第 5 位 Llion Jones，而校验环节因为
答案没有 [n] 引用完全旁观。根因是首页作者区在切块时被切断，第 6 位
（Aidan N. Gomez）整条掉在块与块之间的缝里。
"""
from __future__ import annotations

from app.agents.factcheck import (
    _to_int,
    affiliation_of,
    author_answer,
    check_author_claim,
    parse_author_query,
)

# 与数据库中 Attention Is All You Need 的 authors 字段完全一致（顺序即原文顺序）
ATTENTION = [
    "Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit",
    "Llion Jones", "Aidan N. Gomez", "Łukasz Kaiser", "Illia Polosukhin",
]


# ---------- 1. 问题识别 ----------
def test_parse_nth_author_chinese_and_english():
    assert parse_author_query("这篇论文第6作者是谁？") == {"kind": "nth", "n": 6}
    assert parse_author_query("第六作者是谁及机构？") == {"kind": "nth", "n": 6}
    assert parse_author_query("第 6 位作者是谁") == {"kind": "nth", "n": 6}
    assert parse_author_query("who is the 6th author of this paper") == {"kind": "nth", "n": 6}
    assert parse_author_query("第二位作者的单位") == {"kind": "nth", "n": 2}


def test_parse_first_last_count():
    assert parse_author_query("第一作者是谁") == {"kind": "nth", "n": 1}
    assert parse_author_query("最后一位作者是谁") == {"kind": "last"}
    assert parse_author_query("这篇论文共有几位作者")["kind"] == "count"
    assert parse_author_query("一共多少位作者？")["kind"] == "count"
    assert parse_author_query("how many authors are there")["kind"] == "count"


def test_parse_ignores_non_enumeration():
    # 这些不是"第几位"问题，必须放过去走正常检索，不能被确定性分支截胡
    for q in ("作者提出的方法有什么创新", "这篇文章讲了什么", "多作者的贡献如何划分",
              "作者为什么这样设计", "谁提出的 Transformer"):
        assert parse_author_query(q) is None, q


def test_cn_number_conversion():
    assert _to_int("六") == 6
    assert _to_int("十") == 10
    assert _to_int("十二") == 12
    assert _to_int("二十三") == 23
    assert _to_int("6") == 6
    assert _to_int("") == 0


# ---------- 2. 确定性作答 ----------
def test_sixth_author_is_gomez():
    """评审里答错的那一题，现在必须答对。"""
    out = author_answer("这篇论文第6作者是谁？", ATTENTION)
    assert out and "Aidan N. Gomez" in out
    assert "Llion Jones" not in out


def test_nth_author_counts_from_one():
    assert "Ashish Vaswani" in author_answer("第1作者是谁", ATTENTION)
    assert "Noam Shazeer" in author_answer("第2作者是谁", ATTENTION)


def test_last_and_count():
    assert "Illia Polosukhin" in author_answer("最后一位作者是谁", ATTENTION)
    out = author_answer("这篇论文共有几位作者", ATTENTION)
    assert "8 位作者" in out


def test_out_of_range_and_empty_roster_fall_through():
    # 越界不去编，交给模型走正常路径；名单为空也不能"确定"任何东西
    assert author_answer("第 99 作者是谁", ATTENTION) is None
    assert author_answer("第6作者是谁", []) is None
    assert author_answer("这篇论文讲了什么", ATTENTION) is None


def test_affiliation_missing_is_omitted_not_invented():
    # 没有 PDF 路径时不猜单位，答案里不能出现"单位是"
    out = author_answer("第6作者是谁及机构", ATTENTION, pdf_path="")
    assert out and "Aidan N. Gomez" in out
    assert "单位是" not in out


def test_affiliation_read_from_page1():
    """真实 PDF 首页能读到位次对应的单位（Attention 第 6 位在 Toronto）。

    这一篇是"名字一行、单位另起一行"的版式，所以跨行提取和"遇到下一位作者
    就停"的边界都得生效，只看同行会读不到。
    """
    pdf = r"D:\projects\sciagent\data\pdf\1706.03762v1.pdf"
    import os

    if not os.path.exists(pdf):
        return  # 样本不在就跳过，不把环境缺失当成失败
    got = affiliation_of("Aidan N. Gomez", pdf, ATTENTION)
    assert "Toronto" in got
    # 第一位是 Google Brain，不能串成别人的单位
    first = affiliation_of("Ashish Vaswani", pdf, ATTENTION)
    assert "Google" in first


# ---------- 3. 自由作答的核对 ----------
def test_check_catches_wrong_author():
    """模型答成第 5 位时要判 ok=False，否则重答不会触发。"""
    wrong = "第 6 作者是 Llion Jones，来自 Google Research。[1]"
    r = check_author_claim("第6作者是谁", wrong, ATTENTION)
    assert r["applicable"] and r["ok"] is False
    assert r["expected"] == "Aidan N. Gomez"
    assert "Llion Jones" in r["mentioned"]


def test_check_passes_correct_author():
    right = "第 6 作者是 Aidan N. Gomez，单位是 University of Toronto。"
    r = check_author_claim("第6作者是谁", right, ATTENTION)
    assert r["applicable"] and r["ok"] is True


def test_check_not_applicable_for_other_questions():
    r = check_author_claim("这篇论文讲了什么", "……", ATTENTION)
    assert r["applicable"] is False
    # 名单缺失时也不能判人家错
    r2 = check_author_claim("第6作者是谁", "随便答", [])
    assert r2["applicable"] is False
