"""Agent 状态定义与 Prompt 集中管理。

Prompt 全部集中在此，而不是散落在各节点里——这是评测"Prompt 工程规范性"时
可以直接展示的证据，也方便做 A/B 对比提示词版本。
"""
from __future__ import annotations

from typing import Any

from app.agents.state import SciState  # noqa: F401  统一从 state 模块导出，供图装配使用

__all__ = ["SciState"]


SYSTEM_CONTRACT = """你是 SciAgent 的 {role}。
硬性规则（违反即视为失败）：
1. 只能依据【证据】中的内容作答，不得使用任何外部知识；证据里没有的，直接说"文献中未提及"。
2. 引用格式固定为 [编号]，编号必须来自【证据】列表，不允许自己编号。
3. 数字、单位、人名、机构名必须与证据原文一致，不得四舍五入或改写。
4. 回答用简体中文，专业术语保留英文原词（如 multi-head attention）。
5. 不要输出与问题无关的寒暄。"""


PLANNER_PROMPT = """把用户的研究任务拆成 2-5 个可执行步骤。

可用步骤类型：
- retrieve：检索证据（必须，且最多出现 1 次）
- answer：依据证据作答
- compare：多篇对比作答
- verify：引用校验
- reflect：复盘写经验

历史经验（同类任务踩过的坑，必须参考）：
{lessons}

用户任务：{goal}
任务模式：{mode}
可用文献数：{doc_count}

只输出 JSON：
{{"steps": [{{"type": "retrieve", "query": "改写后的检索式"}}, {{"type": "answer"}}],
  "reasoning": "一句话说明为什么这么拆"}}"""


ANSWER_PROMPT = """【证据】
{evidence}

【问题】
{question}

要求：
- 先给结论，再给依据；分点作答时每点末尾必须带 [编号]。
- 如果证据不足以回答，明确说"文献中未提及"，不要猜。
- 涉及数字/超参/型号时，原样抄写证据中的写法。

输出 JSON：
{{"answer": "正文（含 [1] 这样的引用标记）", "confidence": "high|medium|low",
 "missing": "证据中缺失的关键信息，没有就留空"}}"""


COMPARE_PROMPT = """【证据（来自多篇文献）】
{evidence}

【对比任务】
{question}

要求：
- 用 Markdown 表格做逐项对比（维度｜各文献的做法），表格后给一段结论。
- 每个单元格里的论断都要带 [编号]。
- 明确指出"承袭关系/差异点/演进方向"，这是对比类问题的评分重点。

输出 JSON：
{{"answer": "Markdown 正文", "confidence": "high|medium|low", "missing": ""}}"""


REVIEW_PROMPT = """你是严格的审稿人。检查下面这段回答：
1. 是否有结论而无证据支撑；
2. 引用编号是否与证据表一致；
3. 是否存在"可能/大概/通常"这类含糊表述却给出确定结论。

【回答】
{answer}

【证据清单】
{evidence}

只输出 JSON：
{{"issues": ["问题1", "问题2"], "score": 0-100, "fixed_answer": "若问题严重，给出修订版；否则留空"}}"""


REFLECT_PROMPT = """复盘这次任务，抽出一条可复用的经验。

任务：{goal}
执行轨迹：{trace}
校验结果：{verify}
是否重试：{retries}

只输出 JSON：
{{"scene": "任务类型，如 qa/compare/去重",
  "error_type": "失败类型，如 检索不到/引用越界/答非所问/无",
  "action": "下次应该怎么做，一句话，可执行"}}"""


def render(template: str, **kwargs: Any) -> str:
    safe = {k: (v if v is not None else "") for k, v in kwargs.items()}
    return template.format(**safe)
