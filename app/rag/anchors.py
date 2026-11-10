"""锚点协议与校验器 —— 全项目对抗幻觉的核心。

锚点格式：[p{页码}@{x0},{y0},{x1},{y1}]
例：回答中的 "使用 8 块 P100 GPU [p5@312,420,468,448]"

校验逻辑（为什么这样设计）：
LLM 自己编坐标是常态，所以**不让它写坐标**——它只从检索结果里引用 chunk_id，
服务端把 chunk_id 渲染成坐标。校验器再做一次兜底：锚点必须命中本次提供给模型的
证据集合（页码相等 + 坐标误差 ≤ 3pt），命中不了就判定越界引用。
"""
from __future__ import annotations

import re

ANCHOR_RE = re.compile(r"\[p(\d+)@(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]")
TOL = 3.0


def make_anchor(page: int, bbox) -> str:
    x0, y0, x1, y1 = bbox
    return f"[p{page}@{int(float(x0))},{int(float(y0))},{int(float(x1))},{int(float(y1))}]"


def _parse(text: str) -> list[tuple[str, int, tuple[float, float, float, float]]]:
    return [
        (m.group(0), int(m.group(1)), tuple(float(m.group(i)) for i in range(2, 6)))
        for m in ANCHOR_RE.finditer(text)
    ]


def verify(answer: str, evidence: list[dict]) -> dict:
    """返回 {total, valid, invalid, rate, invalid_anchors, cleaned}。"""
    anchors = _parse(answer)
    if not anchors:
        return {
            "total": 0,
            "valid": 0,
            "invalid": 0,
            "rate": 0.0,
            "invalid_anchors": [],
            "cleaned": answer,
            "has_citation": False,
        }

    valid = 0
    bad: list[str] = []
    cleaned = answer

    for token, page, bbox in anchors:
        ok = False
        for ev in evidence:
            if int(ev.get("page", -1)) != page:
                continue
            eb = ev.get("bbox") or [0, 0, 0, 0]
            if all(abs(float(eb[i]) - bbox[i]) <= TOL for i in range(4)):
                ok = True
                break
        if ok:
            valid += 1
        else:
            bad.append(token)
            cleaned = cleaned.replace(token, "[未核实]")

    total = len(anchors)
    return {
        "total": total,
        "valid": valid,
        "invalid": len(bad),
        "rate": round(valid / total, 4) if total else 0.0,
        "invalid_anchors": bad,
        "cleaned": cleaned,
        "has_citation": True,
    }


def attach_anchors(answer: str, evidence: list[dict]) -> str:
    """模型若只给了 chunk 序号（[1][2]），按出现顺序补成完整锚点。

    这样即便模型不肯写坐标，输出依然可点击、可校验。
    """
    used = 0
    for i, ev in enumerate(evidence, start=1):
        token = f"[{i}]"
        if token in answer:
            anchor = ev.get("anchor") or make_anchor(ev["page"], ev["bbox"])
            answer = answer.replace(token, f"[{i}] {anchor}", 1)
            used += 1
    return answer


def citation_table(evidence: list[dict]) -> str:
    """把证据清单渲染成 Markdown 表格，附在回答末尾，方便人工核对。"""
    if not evidence:
        return ""
    lines = ["\n\n---\n**引用来源**\n", "| # | 文献 | 页 | 锚点 | 片段 |", "| --- | --- | --- | --- | --- |"]
    for i, ev in enumerate(evidence, start=1):
        snippet = (ev.get("text") or "").replace("\n", " ")[:80]
        lines.append(
            f"| {i} | {ev.get('doc_name', '')[:40]} | {ev.get('page')} | "
            f"`{ev.get('anchor', '')}` | {snippet} |"
        )
    return "\n".join(lines)
