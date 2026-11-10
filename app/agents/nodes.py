"""七个 Agent 节点的实现。

每个节点都是 SciState -> SciState 的纯函数，只做一件事，便于单测与替换。
设计上刻意让"检索"和"作答"分开：作答节点只能看见检索节点给的证据，
这样"答不出就说没有"才有可能被真正约束住。
"""
from __future__ import annotations

import json
import time

from sqlalchemy import select

from app.agents.prompts import (
    ANSWER_PROMPT,
    COMPARE_PROMPT,
    PLANNER_PROMPT,
    REFLECT_PROMPT,
    REVIEW_PROMPT,
    SYSTEM_CONTRACT,
    render,
)
from app.core.llm import LLMError, gateway
from app.core.logger import get_logger
from app.db.models import Lesson
from app.db.session import SessionLocal
from app.rag.anchors import citation_table, verify
from app.rag.hybrid import search_with_rerank

log = get_logger(__name__)


def _step(state: dict, agent: str, tool: str = "", **extra) -> None:
    """往 trace 里记一步，前端据此画 Agent 执行轨迹。"""
    state.setdefault("trace", []).append(
        {"agent": agent, "tool": tool, "t": round(time.time(), 3), **extra}
    )


# ---------------- 规划 ----------------
def node_plan(state: dict) -> dict:
    t0 = time.time()
    mode = state.get("mode", "qa")
    doc_count = len(state.get("doc_ids") or [])

    lessons = _load_lessons(mode)
    state["lessons_used"] = lessons

    try:
        data = gateway.chat_json(
            [
                {"role": "system", "content": SYSTEM_CONTRACT.format(role="任务规划器")},
                {
                    "role": "user",
                    "content": render(
                        PLANNER_PROMPT,
                        lessons="；".join(lessons) if lessons else "（暂无）",
                        goal=state["goal"],
                        mode=mode,
                        doc_count=doc_count,
                    ),
                },
            ],
            temperature=0.1,
        )
        steps = data.get("steps") or []
        reasoning = data.get("reasoning", "")
    except LLMError as exc:
        log.warning("规划失败，使用默认计划：%s", exc)
        steps, reasoning = [], "模型不可用，走确定性流程"

    # 兜底计划：保证流程永不因为规划失败而中断
    if not isinstance(steps, list) or not steps:
        steps = [
            {"type": "retrieve", "query": state["goal"]},
            {"type": "compare" if mode == "compare" else "answer"},
        ]

    # 去重 + 类型白名单 + 步数上限
    allowed = {"retrieve", "answer", "compare", "verify", "reflect"}
    seen: list[dict] = []
    for s in steps[:6]:
        t = (s or {}).get("type")
        if t in allowed and not (t == "retrieve" and any(x["type"] == "retrieve" for x in seen)):
            seen.append(s)
    if not any(s["type"] == "retrieve" for s in seen):
        seen.insert(0, {"type": "retrieve", "query": state["goal"]})

    state["plan"] = seen
    state["plan_reasoning"] = reasoning
    state["cursor"] = 0
    _step(state, "planner", "plan", steps=len(seen), latency_ms=int((time.time() - t0) * 1000))
    return state


def _load_lessons(mode: str, limit: int = 3) -> list[str]:
    """读取同类任务的历史经验（"自主进化"的读取端）。"""
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(Lesson)
                .where(Lesson.scene.in_([mode, "通用"]))
                .where(Lesson.error_type != "无")
                .order_by(Lesson.weight.desc(), Lesson.hits.desc())
                .limit(limit)
            ).all()
        )
        out = []
        for r in rows:
            out.append(f"[{r.error_type}] {r.action}")
            r.hits += 1
        db.commit()
    return out


def _expand_query(question: str) -> str:
    """中文问题 → 补英文检索词。

    论文正文是英文、问题是中文，纯词法匹配会大面积漏召。
    这里让模型把问题改写成"中文原句 + 英文关键词"，只在有真实模型时启用
    （mock 模式直接返回原句，不浪费一次调用）。
    """
    if gateway.route() == "mock":
        return question
    try:
        data = gateway.chat_json(
            [
                {
                    "role": "system",
                    "content": "你是检索查询改写器。把中文问题转成适合在英文论文中检索的关键词串。只输出 JSON。",
                },
                {
                    "role": "user",
                    "content": (
                        f"问题：{question}\n"
                        '输出：{"keywords": "英文关键词，空格分隔，保留数字与型号，如 NVIDIA P100 GPU number"}'
                    ),
                },
            ],
            temperature=0,
            max_tokens=120,
        )
        kw = (data.get("keywords") or "").strip()
        return f"{question} {kw}".strip() if kw else question
    except LLMError:
        return question


# ---------------- 检索 ----------------
def node_retrieve(state: dict) -> dict:
    t0 = time.time()
    query = state["goal"]
    for s in state.get("plan", [])[state.get("cursor", 0) :]:
        if s.get("type") == "retrieve" and s.get("query"):
            query = s["query"]
            break

    expanded = _expand_query(query)
    hits = search_with_rerank(expanded, state.get("doc_ids"), top_k=8)
    evidence = [h.to_dict() for h in hits]
    state["evidence"] = evidence
    state["retrieval_query"] = expanded
    _step(
        state, "retriever", "hybrid_search",
        query=expanded, hits=len(evidence), latency_ms=int((time.time() - t0) * 1000),
    )
    return state


# ---------------- 作答 ----------------
def _evidence_block(evidence: list[dict], max_chars_each: int = 700) -> str:
    lines = []
    for i, ev in enumerate(evidence, start=1):
        body = (ev.get("text") or "")[:max_chars_each].replace("\n", " ")
        sec = ev.get("section_path") or ""
        sec_info = f"（章节：{sec}）" if sec else ""
        lines.append(f"[{i}] 来源：{ev.get('doc_name')} 第{ev.get('page')}页{sec_info} | {body}")
    return "\n".join(lines) if lines else "（无检索结果）"


def node_answer(state: dict) -> dict:
    t0 = time.time()
    evidence = state.get("evidence") or []
    if not evidence:
        state["answer"] = "文献库中没有检索到与该问题相关的内容。请确认已上传相关 PDF，或换一种问法。"
        state["citations"] = []
        state["verify"] = {"total": 0, "valid": 0, "invalid": 0, "rate": 0.0, "cleaned": state["answer"]}
        _step(state, "writer", "answer", note="no_evidence")
        return state

    is_compare = state.get("mode") == "compare" or len({e["doc_id"] for e in evidence}) > 1
    tpl = COMPARE_PROMPT if is_compare else ANSWER_PROMPT

    try:
        data = gateway.chat_json(
            [
                {"role": "system", "content": SYSTEM_CONTRACT.format(role="科研写作助手" if not is_compare else "对比分析师")},
                {
                    "role": "user",
                    "content": render(tpl, evidence=_evidence_block(evidence), question=state["goal"]),
                },
            ],
            temperature=0.2,
        )
    except LLMError as exc:
        data = {"answer": f"作答失败：{exc}", "confidence": "low", "missing": ""}

    answer = data.get("answer") or data.get("_raw") or ""
    state["answer"] = answer
    state["confidence"] = data.get("confidence", "medium")
    state["missing"] = data.get("missing", "")
    _step(state, "writer", "answer", chars=len(answer), latency_ms=int((time.time() - t0) * 1000))
    return state


# ---------------- 校验 ----------------
def node_verify(state: dict) -> dict:
    t0 = time.time()
    evidence = state.get("evidence") or []
    result = verify(state.get("answer", ""), evidence)
    state["verify"] = result

    # 引用越界比例过高 → 触发一次重规划（换检索式再来一遍）
    if result["total"] and result["rate"] < 0.8 and state.get("retries", 0) < 1:
        state["retries"] = state.get("retries", 0) + 1
        state["need_replan"] = True
    else:
        state["need_replan"] = False

    _step(
        state, "reviewer", "anchor_verify",
        total=result["total"], valid=result["valid"], rate=result["rate"],
        latency_ms=int((time.time() - t0) * 1000),
    )
    return state


def node_review(state: dict) -> dict:
    """可选：让审稿 Agent 挑刺。仅当证据充分时调用，省 token。"""
    if not state.get("evidence"):
        return state
    try:
        data = gateway.chat_json(
            [
                {"role": "system", "content": SYSTEM_CONTRACT.format(role="审稿人")},
                {
                    "role": "user",
                    "content": render(
                        REVIEW_PROMPT,
                        answer=state.get("answer", "")[:3000],
                        evidence=_evidence_block(state["evidence"][:5], 300),
                    ),
                },
            ],
            temperature=0.1,
        )
        state["review"] = data
        if data.get("fixed_answer") and (data.get("score", 100) < 60):
            state["answer"] = data["fixed_answer"]
    except LLMError:
        state["review"] = {}
    _step(state, "reviewer", "self_review", score=(state.get("review") or {}).get("score"))
    return state


# ---------------- 重规划 ----------------
def node_replan(state: dict) -> dict:
    """引用越界时不再原样重试，而是换检索式：把问题拆成"实体+属性"重查。"""
    alt = state["goal"]
    if state.get("retrieval_query"):
        alt = f"{state['retrieval_query']} 具体数值 原文"
    state["plan"] = [
        {"type": "retrieve", "query": alt},
        {"type": "compare" if state.get("mode") == "compare" else "answer"},
        {"type": "verify"},
    ]
    state["cursor"] = 0
    _step(state, "planner", "replan", query=alt)
    return state


# ---------------- 反思 ----------------
def node_reflect(state: dict) -> dict:
    """把经验写进长期记忆。error_type="无" 也记，用于统计成功率。"""
    t0 = time.time()
    verify_info = state.get("verify") or {}
    try:
        data = gateway.chat_json(
            [
                {"role": "system", "content": SYSTEM_CONTRACT.format(role="反思者")},
                {
                    "role": "user",
                    "content": render(
                        REFLECT_PROMPT,
                        goal=state["goal"][:400],
                        trace=json.dumps(state.get("trace", [])[-8:], ensure_ascii=False)[:1500],
                        verify=json.dumps(verify_info, ensure_ascii=False)[:400],
                        retries=state.get("retries", 0),
                    ),
                },
            ],
            temperature=0.3,
        )
    except LLMError:
        data = {}

    scene = data.get("scene") or state.get("mode", "qa")
    error_type = data.get("error_type") or "无"
    action = data.get("action") or ""

    if action and error_type != "无":
        with SessionLocal() as db:
            db.add(
                Lesson(
                    scene=scene[:120],
                    error_type=error_type[:60],
                    action=action[:1000],
                    weight=1.0 + 0.2 * state.get("retries", 0),
                )
            )
            db.commit()

    state["reflection"] = {"scene": scene, "error_type": error_type, "action": action}
    _step(state, "reflector", "write_memory", error_type=error_type,
          latency_ms=int((time.time() - t0) * 1000))
    return state


def finalize(state: dict) -> dict:
    """拼装最终返回：正文 + 引用表。"""
    body = state.get("answer", "")
    if state.get("mode") != "compare" or "|" not in body:
        pass
    table = citation_table(state.get("evidence") or [])
    state["answer_rendered"] = body + table
    return state
