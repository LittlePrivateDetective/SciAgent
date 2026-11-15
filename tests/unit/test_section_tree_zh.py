"""中文期刊（计算机学报）的章节树：编号带空格、无"参考文献"标题、附录人名。

三个用例都来自真实 PDF（`data/samples_zh_real`），修之前的表现分别是：
- 小节标题全丢（只剩 6 个一级标题），因为 "3. 1　新闻推荐…" 的点号后面有空格；
- 参考文献 0 条、树里没有"参考文献"这一节，因为这家期刊不印这四个字；
- Phi-3 附录的贡献者名单被排成 "1. Gordon Chi / 2. Virginia Adams …"，全成了章节。
"""
from __future__ import annotations

from app.parse.structure import (
    Block,
    _infer_reference_section,
    _is_heading,
    _looks_person_name,
    _norm_num_text,
    _normalize_headings,
    _plausible_heading_tail,
)


def _blk(text: str, size: float = 10.3, bold: bool = False, page: int = 5,
         y: float = 200, x: float = 65) -> Block:
    return Block(page, 0, 0, text, (x, y, x + len(text) * 9, y + 14),
                 size=size, bold=bold)


# ---------- 1. 编号归一 ----------
def test_spaced_number_is_normalized():
    assert _norm_num_text("3. 1　新闻推荐模型域增量学习问题描述") == "3.1 新闻推荐模型域增量学习问题描述"
    assert _norm_num_text("3. 2. 1　阶段1:增强数据生成") == "3.2.1 阶段1:增强数据生成"
    # 本来正常的编号不能被改坏
    assert _norm_num_text("4.2 知识回放") == "4.2 知识回放"
    assert _norm_num_text("参考文献") == "参考文献"


def test_spaced_number_heading_levels():
    ok, lvl = _is_heading(_blk("3. 1　新闻推荐模型域增量学习问题描述"), 10.3, left=65.0)
    assert ok and lvl == 2
    ok, lvl = _is_heading(_blk("3. 2. 1　阶段1:增强数据生成"), 10.3, left=65.0)
    assert ok and lvl == 3


# ---------- 2. 表格单元格不是标题 ----------
def test_unit_cells_are_not_headings():
    """Kaplan 的表格首行 "7.5 B Model (GB)"、GPT-4 正文的 "90 M speakers"。"""
    assert not _plausible_heading_tail("B Model (GB)")
    assert not _plausible_heading_tail("M speakers")
    # 真的标题不能被连坐
    assert _plausible_heading_tail("INTRODUCTION")
    assert _plausible_heading_tail("A Survey of Large Language Models")
    assert _plausible_heading_tail("新闻推荐方法")

    ok, _ = _is_heading(_blk("90 M speakers", size=10.0, page=33, x=115), 7.4, left=115.0)
    assert not ok


# ---------- 3. 无标题参考文献区 ----------
def test_reference_section_without_heading():
    """计算机学报不印"参考文献"，条目直接从 ［1］ 开始、字号比正文小 3pt。"""
    refs = [_blk(f"[{i}] 张三， 李四. 新闻推荐研究进展. 计算机学报， 2023， {40 + i}（1）： 1-10.",
                 size=7.3, y=380 + 10 * i) for i in range(1, 7)]
    out = _infer_reference_section([_blk("正文段落，这里是一句普通的话。"), *refs], body=10.3)
    kinds = [b.kind for b in out]
    assert kinds.count("heading") == 1
    assert out[1].text == "参考文献" and out[1].level == 1
    assert kinds.count("reference") == 6


def test_figure_caption_run_is_not_reference_section():
    """正文里零散的 [1] 不是参考文献区（不足 4 条连续条目）。"""
    blocks = [_blk("正文 [1] 提到的方法"),
              _blk("[1] 张三. 一篇文献.", size=7.3, y=500),
              _blk("又是正文段落，很长的一句话。", y=520)]
    out = _infer_reference_section(blocks, body=10.3)
    assert all(b.kind == "text" for b in out)


# ---------- 4. 附录人名不算章节 ----------
def test_person_name_run_is_demoted():
    names = ["1 Gordon Chi", "2 Virginia Adams", "3 Max Du", "4 Ben Prystawski"]
    blocks = [_blk("A Contributors", size=12.0, bold=True), *(_blk(n) for n in names)]
    for b in blocks:
        b.kind, b.level = "heading", 1
    _normalize_headings(blocks)
    assert blocks[0].kind == "heading"
    assert all(b.kind == "text" for b in blocks[1:]), [b.text for b in blocks]


def test_normal_two_word_headings_survive():
    """不能误伤 "Introduction / Related Work / Method" 这类正常的两词标题。"""
    texts = ["1 Introduction", "2 Related Work", "3 Method"]
    blocks = [_blk(t) for t in texts]
    for b in blocks:
        b.kind, b.level = "heading", 1
    _normalize_headings(blocks)
    assert all(b.kind == "heading" for b in blocks)


def test_person_name_shape():
    assert _looks_person_name("Gordon Chi")
    assert _looks_person_name("Virginia Adams")
    assert not _looks_person_name("Related Work")
    assert not _looks_person_name("Model Architecture")
    assert not _looks_person_name("新闻推荐方法")


# ---------- 5. 表格里的数据行 ----------
def test_data_rows_are_not_headings():
    """Llama 3 的 CrowS-Pairs 表：每行都是"百分数 + 列名 + 百分数"。"""
    from app.parse.structure import _looks_like_data_row

    assert _looks_like_data_row("67.8 Nationality 64.2 61.6")
    assert _looks_like_data_row("62.9 Disability 66.7")
    assert _looks_like_data_row("76.2 Socioeconomic status 71.5")
    # 真标题不能被连坐：编号本身是小数、"1.7T" 这种带单位的数也不算列值
    assert not _looks_like_data_row("5.2 CrowS-Pairs")
    assert not _looks_like_data_row("3.1 Transformer with Post-Layer Normalization")
    assert not _looks_like_data_row("4.1 GPT-4 (1.7T params)")
    assert not _looks_like_data_row("5.4 与现有方法的性能对比")


# ---------- 6. 附录里的有序列表不是章节 ----------
def test_ordered_list_inside_appendix_is_not_heading():
    """Whisper 的 C 节正文是 1./2./3. 的规范化清单，形态与 "3 Results" 一模一样。"""
    from app.parse.structure import _enforce_numbering

    def blk(text, lvl=1):
        b = Block(9, 0, 0, text, (65, 200, 400, 214), size=10.9, bold=False)
        b.kind, b.level = "heading", lvl
        return b

    blocks = [
        blk("C. Text Standardization"),
        blk("1. Remove any phrases between bracketing brackets"),
        blk("3 Remove any of the following words: hmm, mm, mmm"),
        blk("7 Remove periods (.) not followed by numbers"),
        blk("D. Raw Performance Tables"),
    ]
    out = _enforce_numbering(blocks)
    assert out[0].kind == "heading" and out[0].text.startswith("C.")
    assert all(b.kind == "text" for b in out[1:4]), [b.text for b in out]
    assert out[4].kind == "heading" and out[4].text.startswith("D.")


def test_body_numbered_headings_survive():
    """数字编号的正文标题不能被上面那条规则误伤。"""
    from app.parse.structure import _enforce_numbering

    def blk(text, lvl=1):
        b = Block(3, 0, 0, text, (65, 200, 400, 214), size=12.0, bold=True)
        b.kind, b.level = "heading", lvl
        return b

    blocks = [blk("1 Introduction"), blk("2 Approach"), blk("3 Experiments")]
    out = _enforce_numbering(blocks)
    assert all(b.kind == "heading" for b in out)


# ---------- 7. 段首加粗引导词（run-in heading）不是标题 ----------
def test_run_in_sentence_is_not_heading():
    """Llama 3 正文里满是"加粗引导词 + 同段正文"，形态与加粗小标题完全一样。"""
    from app.parse.structure import _looks_like_sentence

    assert _looks_like_sentence("Method. Specifically, Singh et al. (2024) propose to")
    assert _looks_like_sentence("MGSM (Shi et al., 2022). We use the same native")
    assert _looks_like_sentence("Finetuning data. The quality and diversity of our data")


def test_abbreviations_do_not_look_like_sentences():
    """缩写 `vs.` / `v.s.` / 附录字母 `D.` 后面也跟大写，不能当句中断句。"""
    from app.parse.structure import _looks_like_sentence

    assert not _looks_like_sentence("4 QLoRA vs. Standard Finetuning")
    assert not _looks_like_sentence("Appendix D. Preprocessed Examples")
    assert not _looks_like_sentence("F.1. Distillation v.s. Reinforcement Learning")
    assert not _looks_like_sentence("5.2 Multilingual")
    assert not _looks_like_sentence("A. Evaluation Datasets.")
