"""编排层：LangGraph 状态机（缺失时降级为等价执行器）。

图的语义（答辩时照这张图讲即可）：

    plan ──► retrieve ──► answer ──► verify
                                        │
                          ┌─────────────┴─────────────┐
                    引用越界 <80%                  通过
                          │                           │
                      replan ──► retrieve         review ──► reflect ──► END

两条边是"动态重规划"的实体：
- verify 的条件边 → replan，是"失败后改计划"而不是"原样重试"；
- 规划节点在做计划前会检索同类任务的历史经验（lessons），这是"自主进化"的实体。

为什么同时保留一个不依赖 LangGraph 的执行器：
评审环境的依赖安装经常出问题，作品不能因为一个包没装上就整体不可用。
两套执行的是同一组节点函数，行为一致，只在调度方式上不同。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from app.agents import nodes
from app.core.llm import gateway, want_thinking
from app.core.logger import get_logger
from app.db.models import Task, TaskStep
from app.db.session import SessionLocal

log = get_logger(__name__)

MAX_ROUNDS = 2


# ---------------- LangGraph 装配 ----------------
def build_langgraph():
    """编译 LangGraph 图；未安装 langgraph 时返回 None。"""
    try:
        from langgraph.graph import END, StateGraph
    except Exception:  # noqa: BLE001
        return None

    from app.agents.state import SciState

    g = StateGraph(SciState)
    g.add_node("plan", nodes.node_plan)
    g.add_node("retrieve", nodes.node_retrieve)
    g.add_node("answer", nodes.node_answer)
    g.add_node("verify", nodes.node_verify)
    g.add_node("review", nodes.node_review)
    g.add_node("reflect", nodes.node_reflect)
    g.add_node("replan", nodes.node_replan)

    g.set_entry_point("plan")
    g.add_edge("plan", "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", "verify")
    g.add_conditional_edges(
        "verify",
        lambda s: "replan" if s.get("need_replan") else "review",
        {"replan": "replan", "review": "review"},
    )
    g.add_edge("replan", "retrieve")
    g.add_edge("review", "reflect")
    g.add_edge("reflect", END)
    return g.compile()


_LG = None
_LG_TRIED = False


def _lg():
    global _LG, _LG_TRIED
    if not _LG_TRIED:
        _LG = build_langgraph()
        _LG_TRIED = True
        log.info("编排引擎：%s", "LangGraph" if _LG else "内置执行器（未安装 langgraph）")
    return _LG


# ---------------- 内置执行器 ----------------
def _run_fallback(state: dict) -> dict:
    state = nodes.node_plan(state)
    rounds = 0
    while True:
        state = nodes.node_retrieve(state)
        state = nodes.node_answer(state)
        state = nodes.node_verify(state)
        if state.get("need_replan") and rounds < MAX_ROUNDS:
            rounds += 1
            state = nodes.node_replan(state)
            continue
        break
    state = nodes.node_review(state)
    state = nodes.node_reflect(state)
    return state


# ---------------- 对外入口 ----------------
def run(
    goal: str,
    doc_ids: list[str] | None = None,
    mode: str = "qa",
    on_step: Callable[[dict], None] | None = None,
    thinking: bool | None = None,
) -> dict:
    """执行一次完整任务，落库任务轨迹，返回结构化结果。"""
    t0 = time.time()
    state: dict[str, Any] = {
        "goal": goal,
        "mode": mode,
        "doc_ids": doc_ids or [],
        # 在这里就归一成布尔：节点里大量 `if state.get("thinking")` 的判断，
        # 而 None 表示"跟随后台设置"，落到节点里会被当成 False ——
        # 深思模式于是静默跳过了审稿（实测对比题的 reviewer 一直 skip）。
        "thinking": want_thinking(thinking),
        "retries": 0,
        "trace": [],
    }

    engine = _lg()
    try:
        if engine is not None:
            state = dict(engine.invoke(state))
        else:
            state = _run_fallback(state)
    except Exception as exc:  # noqa: BLE001 编排失败也要给出可读结果
        log.exception("编排执行失败")
        state.setdefault("answer", f"任务执行失败：{exc}")
        state["error"] = str(exc)

    state = nodes.finalize(state)
    latency = int((time.time() - t0) * 1000)

    task_id = _persist(state, goal, mode, latency)
    if on_step:
        for step in state.get("trace", []):
            on_step(step)

    return {
        "task_id": task_id,
        "answer": state.get("answer_rendered", state.get("answer", "")),
        "confidence": state.get("confidence", ""),
        "missing": state.get("missing", ""),
        "evidence": state.get("evidence", []),
        "verify": state.get("verify", {}),
        "plan": state.get("plan", []),
        "plan_reasoning": state.get("plan_reasoning", ""),
        "trace": state.get("trace", []),
        "reflection": state.get("reflection", {}),
        "latency_ms": latency,
        "engine": "langgraph" if _lg() else "builtin",
        "llm": gateway.describe(),
    }


def _persist(state: dict, goal: str, mode: str, latency: int) -> str:
    """任务与步骤落库 → 前端"执行轨迹"面板、答辩时的过程证据。"""
    with SessionLocal() as db:
        task = Task(
            goal=goal[:2000],
            mode=mode,
            status="failed" if state.get("error") else "done",
            plan_json=json.dumps(state.get("plan", []), ensure_ascii=False),
            result_json=json.dumps(
                {
                    "confidence": state.get("confidence"),
                    "verify": state.get("verify"),
                    "reflection": state.get("reflection"),
                },
                ensure_ascii=False,
            ),
            steps_count=len(state.get("trace", [])),
            latency_ms=latency,
        )
        db.add(task)
        db.flush()

        for i, step in enumerate(state.get("trace", [])):
            db.add(
                TaskStep(
                    task_id=task.id,
                    idx=i,
                    agent=step.get("agent", ""),
                    tool=step.get("tool", ""),
                    input_json=json.dumps(step, ensure_ascii=False)[:4000],
                    status="ok",
                    latency_ms=int(step.get("latency_ms", 0) or 0),
                )
            )
        db.commit()
        return task.id
