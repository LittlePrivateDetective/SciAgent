"""作者枚举题：确定性作答与核对。

用例全部来自 2026-11-14 独立评审的 D-QA03：官方样例问 Attention v1 的
「第 6 作者是谁及机构」，系统答成了第 5 位 Llion Jones，而校验环节因为
答案没有 [n] 引用完全旁观。根因是首页作者区在切块时被切断，第 6 位
（Aidan N. Gomez）整条掉在块与块之间的缝里。
"""
from __future__ import annotations

from app.agents.factcheck import (
    absent_entities,
    question_entities,
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


# ---------------------------------------------------------------- 证据覆盖闸门
def test_entities_extracted_from_mixed_question():
    """中英混写的问题里，英文术语要被抽成硬实体。"""
    assert question_entities("对比 BERT 与 Transformer 编码器在结构上的承袭关系") == [
        "BERT",
        "Transformer",
    ]


def test_chinese_only_question_has_no_entities():
    """纯中文问题不触发闸门，否则正常提问会被误拦。"""
    assert question_entities("这篇论文的主要贡献是什么") == []
    assert absent_entities("这篇论文的主要贡献是什么", [{"text": "任意内容"}]) == []


def test_absent_entities_blocks_unrelated_evidence():
    """真实事故：当前文献全文 0 处 BERT，模型却答出了承袭关系。"""
    ev = [{"text": "定义 5（深度集成架构）L = <E, c, φ>，c 为增广算子"}, {"text": "级联层由基学习器集合构成"}]
    assert absent_entities("对比 BERT 与 Transformer 编码器", ev) == ["BERT", "Transformer"]


def test_absent_entities_passes_when_mentioned():
    """证据里提到过就不拦（大小写不敏感，词根命中即可）。"""
    ev = [{"text": "BERT 采用 Transformer 的双向编码结构"}]
    assert absent_entities("对比 BERT 与 Transformer", ev) == []
    assert absent_entities("BERT 怎么训练", [{"text": "bert 使用掩码语言模型"}]) == []


def test_absent_entities_partial_hit_not_blocked():
    """只缺一个不算全缺：模型仍可基于命中的那部分回答。"""
    ev = [{"text": "Transformer 由多层注意力堆叠"}]
    assert absent_entities("对比 BERT 与 Transformer", ev) == ["BERT"]
    assert absent_entities("对比 BERT 与 Transformer", ev) != []


def test_lowercase_variable_not_treated_as_entity():
    """误伤案例：证据把 beta1 写成 β1。全小写变量名不该被当成硬实体，
    否则"本来答得对"的参数题会被拦下来。"""
    q = "Adam 优化器的 beta1 和 beta2 是多少"
    assert question_entities(q) == ["Adam"]          # beta1/beta2 全小写，不收
    ev = [{"text": "Adam 优化器中 β1 = 0.9，β2 = 0.98，ϵ = 10−9"}]
    assert absent_entities(q, ev) == []
