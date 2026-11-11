"""七个 Agent 节点的实现。

每个节点都是 SciState -> SciState 的纯函数，只做一件事，便于单测与替换。
设计上刻意让"检索"和"作答"分开：作答节点只能看见检索节点给的证据，
这样"答不出就说没有"才有可能被真正约束住。
"""
from __future__ import annotations

import json
import re
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
from app.core.config import settings
from app.core.llm import LLMError, gateway
from app.core.logger import get_logger
from app.db.models import Lesson
from app.db.session import SessionLocal
from app.rag.anchors import attach_anchors, citation_table, verify
from app.rag.hybrid import search_with_rerank, tokenize

log = get_logger(__name__)

# 只有用户明确表达"对比/差异/演进"意图时才走多篇对比模板
COMPARE_HINT = re.compile(
    r"(对比|比较|相比|异同|区别|差异|演进|承袭|变化|vs\.?|versus|compare|difference)",
    re.IGNORECASE,
)

# JSON 包裹在"推理模型 + 长表格"上极容易把输出预算烧光（实测返回空串）。
# 兜底改走非推理模型写纯文本，只保留 [n] 引用标记这一条硬约束。
PLAIN_ANSWER_PROMPT = """【证据（每条都可按 [编号] 精确回查原文）】
{evidence}

【问题】
{question}

要求：
- 第一句直接给结论；{mode_rule}
- 每个论断后带 [编号]，编号不得超过证据条数；
- 数字、型号、单位原样抄写，不换算；
- 只写正文，不要 JSON、不要前言、不要写"模型/系统/错误"等元话语；
- 300 字以内。"""


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
def _key_sentences(text: str, query: str, limit: int = 2) -> list[str]:
    """从证据段落里挑出与问题最相关的句子。

    为什么需要：PDF 切块后证据经常"从半句开始"（如 "ence pairs containing…"），
    模型容易只看开头就判定"未提及"。把含关键词的句子提到最前面，
    等于替模型做了句子级对齐，实测能显著减少"明明有却答没有"。
    """
    q = set(tokenize(query))
    sents = [s.strip() for s in re.split(r"(?<=[.。!?！？])\s+|\n", text) if len(s.strip()) >= 20]
    scored: list[tuple[int, int, str]] = []
    for s in sents:
        hit = len(q & set(tokenize(s)))
        if hit:
            scored.append((hit, -len(s), s))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out: list[str] = []
    for _, _, s in scored[:limit]:
        if s not in out:
            out.append(s)
    return out


def _evidence_block(evidence: list[dict], max_chars_each: int = 1600, query: str = "") -> str:
    lines = []
    for i, ev in enumerate(evidence, start=1):
        body = ev.get("text") or ""
        if len(body) > max_chars_each:
            cut = body[:max_chars_each]
            # 尽量切在句末，别把"半句话"喂给模型（否则它会答"证据被截断"）
            p = max(cut.rfind("。"), cut.rfind(". "), cut.rfind("\n"))
            if p > max_chars_each * 0.6:
                cut = cut[: p + 1]
            body = cut + " …（本段后文省略）"
        body = body.replace("\n", " ")
        sec = ev.get("section_path") or ""
        sec_info = f"（章节：{sec}）" if sec else ""
        key = _key_sentences(ev.get("text") or "", query) if query else []
        key_line = ("▸ 关键句：" + " ｜ ".join(key) + "\n") if key else ""
        lines.append(f"[{i}] 来源：{ev.get('doc_name')} 第{ev.get('page')}页{sec_info} | {body}\n{key_line}")
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

    # 是否走"多篇对比"模板：必须以用户意图为准。
    # 早期版本用"证据里出现多篇文档"就判定对比，结果单篇问题只要同时召回两篇
    # 就会被强行做成对比表格（实测"Adam 的 beta1"被拿去跟 PPO 论文对比）。
    mode = state.get("mode", "qa")
    doc_count = len({e.get("doc_id") for e in evidence})
    intent_hint = bool(COMPARE_HINT.search(state.get("goal", ""))) and doc_count > 1
    is_compare = mode == "compare" or intent_hint
    tpl = COMPARE_PROMPT if is_compare else ANSWER_PROMPT
    # 路由决策写进 trace：Multi-Agent 的"分流"必须可观测、可回归，
    # 而不是只能靠读代码推断。trigger 说明是用户显式选了对比模式，
    # 还是问题里的「对比/差异」触发了意图识别（后者是产品特性，不是误判）。
    _step(state, "writer", "route", mode=mode,
          template="compare" if is_compare else "qa",
          trigger="explicit_mode" if mode == "compare" else
                  ("intent_hint" if intent_hint else "default"),
          intent_hint=intent_hint, evidence_docs=doc_count)
    msgs = [
        {
            "role": "system",
            "content": SYSTEM_CONTRACT.format(role="科研写作助手" if not is_compare else "对比分析师"),
        },
        {
            "role": "user",
            "content": render(
                tpl,
                evidence=_evidence_block(evidence, query=state["goal"]),
                question=state["goal"],
            ),
        },
    ]

    try:
        data = gateway.chat_json(msgs, temperature=0.2, max_tokens=4096 if is_compare else None)
    except LLMError as exc:
        data = {"answer": f"作答失败：{exc}", "confidence": "low", "missing": ""}

    answer = (data.get("answer") or data.get("_raw") or "").strip()
    # 对比模式要输出表格，推理模型的思维 token 会把默认预算吃光，
    # 结果 content 为空。这里再放大预算重试一次，仍为空才报错。
    if not answer:
        try:
            data = gateway.chat_json(msgs, temperature=0.2, max_tokens=4096)
            answer = (data.get("answer") or data.get("_raw") or "").strip()
            _step(state, "writer", "retry_empty", note="首次返回空内容，已放大输出预算重试")
        except LLMError as exc:
            log.warning("空回答重试失败：%s", exc)
    if not answer:
        # 最后一次兜底：换**非推理模型**写纯文本。
        # 推理模型的思维链会吃掉 JSON 包裹的预算，实测对比长表格连续两次空串。
        try:
            plain = gateway.chat(
                [
                    {"role": "system", "content": SYSTEM_CONTRACT.format(role="科研写作助手")},
                    {"role": "user", "content": PLAIN_ANSWER_PROMPT.format(
                        evidence=_evidence_block(evidence, 1000, query=state.get("goal", "")),
                        question=state.get("goal", ""),
                        mode_rule="用 Markdown 表格逐项对比，并给出承袭关系/差异点"
                                  if is_compare else "用 1-3 个要点给依据",
                    )},
                ],
                temperature=0.2, max_tokens=3000, model=settings.trans_model,
            ).text.strip()
            if len(plain) > 30 and "模型未返回" not in plain:
                answer = plain
                data = {"answer": answer, "confidence": "medium", "missing": ""}
                _step(state, "writer", "fallback_plaintext",
                      model=settings.trans_model, chars=len(answer))
        except LLMError as exc:
            log.warning("纯文本兜底也失败：%s", exc)

    if not answer:
        answer = "证据已检索到，但模型未能生成有效回答。请稍后重试或把问题拆小。"
        state["error"] = "empty_answer"

    state["answer"] = answer
    state["confidence"] = data.get("confidence", "medium")
    state["missing"] = data.get("missing", "")
    _step(state, "writer", "answer", chars=len(answer), latency_ms=int((time.time() - t0) * 1000))
    return state


# ---------------- 校验 ----------------
def node_verify(state: dict) -> dict:
    t0 = time.time()
    evidence = state.get("evidence") or []
    result = verify(state.get("answer", ""), evidence, state.get("goal", ""))
    state["verify"] = result

    # 引用/数字溯源通过率过低 → 换检索式重来一次（动态重规划）
    if result["total"] and result["rate"] < 0.75 and state.get("retries", 0) < 1:
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
    """可选：让审稿 Agent 挑刺。仅当证据充分且上游答案有效时调用。"""
    if not state.get("evidence"):
        return state
    answer = state.get("answer", "")
    # P0 修复：上游若已经失败（空回答/兜底文案），审稿人会把"系统错误提示"
    # 当成正文去改写，产出一大段关于"回答不合格"的元话语（QA A01 实测）。
    # 上游无效就老实跳过，不要把降级文案再加工成看起来像答案的东西。
    if state.get("error") or len(answer.strip()) < 20 or re.search(
        r"模型未返回|系统错误|作答失败|任务执行失败|未能生成有效回答", answer
    ):
        _step(state, "reviewer", "skip", note="upstream_answer_invalid")
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
                        evidence=_evidence_block(state["evidence"][:5], 400, query=state.get("goal", "")),
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
    """拼装最终返回：正文 + 引用表；另存一份带坐标锚点的版本供导出核验。"""
    body = state.get("answer", "")
    evidence = state.get("evidence") or []
    # 以最终要展示的正文重算一次校验：审稿节点可能改写过答案，
    # 若沿用草稿的指标，页面上会出现"数字与看到的答案对不上"。
    if evidence:
        state["verify"] = verify(body, evidence, state.get("goal", ""))
    table = citation_table(evidence)
    state["answer_rendered"] = body + table
    # 导出用的"可核验版"：每个 [1] 后面直接跟 [p7@x0,y0,x1,y1]，拿到 PDF 里能精准定位
    state["answer_anchored"] = attach_anchors(body, evidence) + table
    return state
