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
# 模型输出的引用记号：[1] / [12]
REF_RE = re.compile(r"\[(\d{1,3})\]")
# 引号内的内容是原文照抄，里面的 [18] 是论文自己的参考文献编号，
# 不能当成"引用了不存在的证据"——这是实测出来的误报来源。
_QUOTE_RE = re.compile(r'"[^"]{0,500}"|“[^”]{0,500}”|「[^」]{0,500}」')
# 数字：前后不能紧邻字母（排除 beta1、p7、WMT2014 里的粘连同字母），支持 1,000 / 28.4
_NUM_RE = re.compile(r"(?<![^\W\d_])(?<![0-9.])\d+(?:[.,]\d+)*(?![^\W\d_])")
# 出处前缀里的 arXiv 号（1706.03762v1 / arXiv:2001.08361）不是答案数字
_RE_ARXIV_LIKE = re.compile(r"(?:arxiv[:\s]*)?\b\d{4}\.\d{4,5}(?:v\d+)?\b", re.I)
# 文件名同理（2001.08361v1.pdf / N19-1423.pdf）
_RE_FILENAME = re.compile(r"[\w.\-]+\.pdf", re.I)
TOL = 3.0

# 论文正文里数词常写成英文单词（"twelve hours"），另一页才写成 "12 hours"。
# 答案写「12 小时」却引用了含 "twelve" 的那段时，逐字比对会误报幻觉
# （QA 最终轮 C07 实测 bad_numbers=['12']，而 p2 原文确实是 twelve hours）。
# 只把这些词**加进证据池**，不放宽答案侧：模型编造的数字依然无处可藏。
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "million": 1000000,
    "billion": 1000000000,
}
_RE_WORD_NUM = re.compile(
    r"\b(" + "|".join(_WORD_NUM) + r")\b", re.IGNORECASE
)


def _number_words(text: str) -> list[float]:
    """把证据文本里的英文数词转成数值，供数字溯源池使用。"""
    return [float(_WORD_NUM[m.group(1).lower()]) for m in _RE_WORD_NUM.finditer(text)]


def make_anchor(page: int, bbox) -> str:
    x0, y0, x1, y1 = bbox
    return f"[p{page}@{int(float(x0))},{int(float(y0))},{int(float(x1))},{int(float(y1))}]"


def _parse(text: str) -> list[tuple[str, int, tuple[float, float, float, float]]]:
    return [
        (m.group(0), int(m.group(1)), tuple(float(m.group(i)) for i in range(2, 6)))
        for m in ANCHOR_RE.finditer(text)
    ]


def _to_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _numbers(text: str) -> list[tuple[str, float]]:
    """抽取文中的量化数字，返回 (原始写法, 数值)。

    先剥掉锚点和引用编号，否则 [p7@107,153,504,206] 会被当成 107、153 等数字。
    再剥掉 arXiv 号与文件名：答案里常带「1706.03762v1 第7页」这样的出处前缀，
    其中的 1706.0376 会被当成"答案里的数字"，造成大量溯源误报。
    """
    body = REF_RE.sub(" ", ANCHOR_RE.sub(" ", text))
    body = _RE_ARXIV_LIKE.sub(" ", body)
    body = _RE_FILENAME.sub(" ", body)
    out: list[tuple[str, float]] = []
    for m in _NUM_RE.finditer(body):
        v = _to_float(m.group(0))
        if v is not None:
            out.append((m.group(0), v))
    return out


def _supported(value: float, pool: list[float]) -> bool:
    """允许千分位与轻微的写法差异（28.4 vs 28.40），但不容忍改数值。"""
    tol = max(1e-6, 0.005 * abs(value))
    return any(abs(v - value) <= tol for v in pool)


def verify(answer: str, evidence: list[dict], question: str = "") -> dict:
    """三重校验，返回可量化指标。

    1) 引用编号合法性：模型写的 [k] 必须落在本次证据条数范围内（防止它引用不存在的第 9 条）
    2) 锚点合法性：若模型自行写了 [pN@...]，坐标必须能在证据里找到（防止编造坐标）
    3) 数字溯源：回答里出现的每个数字，必须出现在它引用的证据原文里
       —— 这条是对抗"把 28.4 写成 28.9"这类最隐蔽的幻觉，也是评委最容易验证的一条
    """
    anchors = _parse(answer)
    # 只统计引号外的编号：引号内是原文照抄，其中的 [18] 属于论文自己的参考文献
    refs = [int(x) for x in REF_RE.findall(_QUOTE_RE.sub(" ", answer))]
    n_ev = len(evidence)

    checks = 0
    valid = 0
    bad_anchors: list[str] = []
    bad_refs: list[int] = []
    bad_numbers: list[str] = []
    cleaned = answer

    # ---- 1) 锚点合法性 ----
    for token, page, bbox in anchors:
        checks += 1
        hit = False
        for ev in evidence:
            if int(ev.get("page", -1)) != page:
                continue
            eb = ev.get("bbox") or [0, 0, 0, 0]
            if all(abs(float(eb[i]) - bbox[i]) <= TOL for i in range(4)):
                hit = True
                break
        if hit:
            valid += 1
        else:
            bad_anchors.append(token)
            cleaned = cleaned.replace(token, "[未核实]")

    # ---- 2) 引用编号合法性 ----
    for k in refs:
        checks += 1
        if 1 <= k <= n_ev:
            valid += 1
        else:
            bad_refs.append(k)

    # ---- 3) 数字溯源 ----
    cited = [evidence[k - 1] for k in {k for k in refs if 1 <= k <= n_ev}]
    pool_src = cited or evidence
    # 章节路径里的编号（如 "5.3 Optimizer"）也算证据的一部分，否则会被误判为幻觉数字
    pool_text = " ".join(
        ((ev.get("text") or "") + " " + (ev.get("section_path") or "")) for ev in pool_src
    )
    pool = [v for _, v in _numbers(pool_text)] + _number_words(pool_text)
    q_pool = [v for _, v in _numbers(question)] if question else []

    for raw, val in _numbers(_QUOTE_RE.sub(" ", answer)):
        checks += 1
        if _supported(val, pool) or _supported(val, q_pool):
            valid += 1
        else:
            bad_numbers.append(raw)

    total = checks
    return {
        "total": total,
        "valid": valid,
        "invalid": total - valid,
        "rate": round(valid / total, 4) if total else 0.0,
        "invalid_anchors": bad_anchors,
        "invalid_refs": bad_refs,
        "bad_numbers": bad_numbers[:12],
        "cleaned": cleaned,
        "has_citation": bool(refs or anchors),
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
