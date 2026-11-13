"""证据预算：按问题类型决定"看多少条证据"。

为什么值得单独一个模块：这个判断有两个消费者——检索层（决定 top_k）和流式的
「思考过程」展示（决定"识别意图：数值抽取"那行文字）。两处各写一份正则，
迟早会出现"界面说这是数值抽取、实际却给了 10 条对比证据"的自相矛盾。

三条经验（都来自实测）：
1. **数值题不需要很多证据**：问"自注意力的计算复杂度"时，含答案的就是 Table 1
   那几行；给 8 条无关段落会让模型开始罗列别的表格行，答案反而更糊。
2. **对比题必须给够**：跨文献对比要两篇各自的段落都在场，少一条就变成
   "其中一篇未提及"。
3. **多给不等于更准**：证据条数与 prompt 长度线性相关，而 16 秒的等待里
   很大一部分是在等这段超长 prompt 的 prefill。
"""
from __future__ import annotations

import re

from app.core.config import settings

# 顺序有意义：先判对比/数值，再判概念。同一个问题往往同时命中多条规则，
# 越"具体"的意图越该优先（"对比两篇论文的参数量"应该是对比，不是数值抽取）。
_RE_COMPARE = re.compile(
    r"(对比|比较|相比|异同|区别|差异|演进|承袭|变化|vs\.?|versus|compare|difference)",
    re.IGNORECASE,
)
_RE_NUMERIC = re.compile(
    r"多少|几[个块倍张层]|BLEU|参数量|FLOPs|占比|比例|分数|准确率|复杂度|大小|"
    r"hyperparameter|learning rate|batch size",
    re.IGNORECASE,
)
_RE_CONCEPT = re.compile(r"区别|不同|关系|为什么|原理|机制|怎么实现|如何|是什么")
_RE_FACT = re.compile(r"第\s*\d+\s*作者|是谁|哪个机构|哪一年|哪里|哪篇|引用")


def classify(question: str, mode: str = "qa") -> str:
    """返回 compare / numeric / concept / fact / lookup 之一。"""
    if mode == "compare":
        return "compare"
    q = question or ""
    if _RE_COMPARE.search(q):
        return "compare"
    if _RE_NUMERIC.search(q):
        return "numeric"
    if _RE_CONCEPT.search(q):
        return "concept"
    if _RE_FACT.search(q):
        return "fact"
    return "lookup"


# 给前端「思考过程」用的中文标签。lookup 说"资料检索"而不是"其他"，
# 因为它对应的动作就是宽口径检索。
LABELS = {
    "compare": "跨文献对比",
    "numeric": "数值抽取",
    "concept": "概念辨析",
    "fact": "事实定位",
    "lookup": "资料检索",
}


def label(question: str, mode: str = "qa") -> str:
    return LABELS[classify(question, mode)]


def top_k(question: str, mode: str = "qa") -> int:
    """按意图取证据条数；后台改过参数就按后台的来。"""
    kind = classify(question, mode)
    if kind == "compare":
        return max(1, int(settings.evidence_topk_compare))
    if kind in ("numeric", "fact"):
        return max(1, int(settings.evidence_topk_fact))
    if kind == "concept":
        return max(1, int(settings.evidence_topk_concept))
    # lookup：没有明确形态，给中间值——比 concept 少一条，比 fact 多一条
    return max(1, int(settings.evidence_topk_concept) - 1)


def stitch_n(question: str, mode: str = "qa") -> int:
    """邻块扩展条数：对比题多补一点（跨段落论证常被切在两块之间），
    数值题一条都不补——答案就在那一行里，补出来的邻块只会是表格的下一行。
    """
    kind = classify(question, mode)
    if kind == "compare":
        return 3
    if kind in ("numeric", "fact"):
        return 0
    return 2
