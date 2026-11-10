"""LangGraph 共享状态定义。

所有节点都是 SciState -> SciState 的纯函数，状态里只放数据不放连接，
这样任何一步都能单独重放/单测，也便于把 trace 落库做过程回放。
"""
from __future__ import annotations

from typing import TypedDict


class SciState(TypedDict, total=False):
    task_id: str
    goal: str
    mode: str                 # qa / compare / survey / review
    doc_ids: list[str]
    plan: list[dict]
    cursor: int
    evidence: list[dict]
    draft: str
    answer: str
    answer_rendered: str
    citations: list[dict]
    verify: dict
    retries: int
    need_replan: bool
    reflections: list[str]
    trace: list[dict]
    lessons_used: list[str]
    retrieval_query: str
    plan_reasoning: str
    confidence: str
    missing: str
    review: dict
    reflection: dict
    error: str
