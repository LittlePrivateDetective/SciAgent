"""证据句级定位：把「高亮整段」收窄到「高亮真正回答问题的那几句」。

检索的单位是 chunk——一个 chunk 通常就是一整段（实测常见 bbox 高 94pt、7 行以上）。
前端若直接把 chunk bbox 画成一个矩形，用户看到的就是一大块黄方块：既盖住了同段里
无关的句子，也看不出到底哪一句支撑了结论（用户截图反馈的正是这个问题）。

这里在服务端把 chunk 拆句，按问题、答案里的实词以及答案里的数字给每句打分，
挑出真正命中的句子返回。前端再拿词级文本层把这些句子画成「贴着行」的多个小框，
句首从行中间开始、句尾到最后一个词为止，而不是套一个大矩形。

为什么放在服务端而不是前端：打分需要答案全文，而答案只在服务端生成；前端拿到的
evidence 里没有答案。放这里还能让「证据列表摘要」直接显示关键句。
"""

from __future__ import annotations

import math
import re

# 英文虚词：不参与打分，否则每句都"命中"一堆 the/of 看不出差别
STOP = set(
    """a an the of for and or to in on with via from by is are was were be been as at we our
this that these those it its their they he she them use used using based show shows shown
can could may might not no than then there here which who whom whose what when where how why
also more most much many such same other into over under between through during about
paper work works propose proposes proposed result results method methods approach model models
have has had do does did one two first second new""".split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_NUM = re.compile(r"\d+(?:\.\d+)?")
# 句末标点后必须跟空白+大写/数字/括号，才认为真的断句；
# "3.5 days" 里的点后面是小写，不会被误切。
_BREAK = re.compile(r"[.!?][\"')\]]?\s+")
# 这些缩写里的点不是句末
_ABBR = re.compile(
    r"(?:\b(?:e\.g|i\.e|et al|fig|figs|eq|eqs|sec|secs|tab|vs|cf|approx|ref|refs|no|al|resp|etc)\.)$",
    re.I,
)


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """把一段原文切成句子，返回 (句子, 起始下标, 结束下标)。

    下标是相对传入文本的，前端用不到，但调试与断言需要（可以回查原文位置）。
    切分刻意保守：宁可比真实句子长，也不要把一句拆成两半——拆错会导致
    「关键句只高亮半句」，比整段高亮更难解释。
    """
    body = re.sub(r"\s+", " ", (text or "")).strip()
    if not body:
        return []

    out: list[tuple[str, int, int]] = []
    start = 0
    for m in _BREAK.finditer(body):
        head = body[start:m.end()].strip()
        prev = body[max(0, m.start() - 14):m.start() + 1]
        if _ABBR.search(prev):
            continue
        # "J. Smith"、"Fig. 3" 这类单字母+点也跳过
        if re.search(r"\b[A-Z]\.$", prev):
            continue
        nxt = body[m.end():m.end() + 1]
        if nxt and not (nxt.isupper() or nxt.isdigit() or nxt in "([\"'"):
            continue
        # 碎片（"et al." 之后剩下的）不单独成句，留给下一句
        if len(head) < 24:
            continue
        out.append((head, start, m.end()))
        start = m.end()

    tail = body[start:].strip()
    if tail:
        out.append((tail, start, len(body)))
    return out


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in STOP}


def _score(sentence: str, q_terms: set[str], a_terms: set[str], a_nums: set[str]) -> float:
    """句子得分：问题实词命中为主，答案实词为辅，答案里的数字权重最高。

    数字单独加权是因为「BLEU 是多少」这类问题的答案句往往只有数字最有辨识度：
    整段里只有一句写着 28.4，靠 28.4 就能锁定，靠 transformer/model 反而会命中一片。
    最后按词数做对数归一，否则永远选中段落里最长的那句。
    """
    words = {w.lower() for w in _WORD.findall(sentence) if w.lower() not in STOP}
    if not words:
        return 0.0
    hit_q = len(words & q_terms)
    hit_a = len(words & a_terms)
    hit_n = sum(1 for n in a_nums if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", sentence))
    raw = hit_q * 1.0 + hit_a * 0.35 + hit_n * 3.0
    return raw / (1 + math.log(1 + len(words) / 12))


def focus_spans(text: str, question: str, answer: str = "", limit: int = 2) -> dict:
    """在 chunk 内挑出最可能支撑答案的句子。

    返回 {"sentences": [...], "mode": "sentence"|"whole", "scores": [...]}。
    `mode="whole"` 表示没找到有区分度的句子，调用方应退回"整段"（前端仍按行绘制）。
    """
    sents = split_sentences(text)
    if len(sents) <= 1:
        return {"sentences": [s[0] for s in sents], "mode": "whole", "scores": []}

    q_terms = _terms(question)
    a_terms = _terms(answer)
    # 只取答案里出现、且 chunk 里也有的数字：答案里的其他数字（如页码、年份）不算
    a_nums = {n for n in _NUM.findall(answer or "") if len(n) >= 2 and n in (text or "")}

    scored = [(s, st, en, _score(s, q_terms, a_terms, a_nums)) for s, st, en in sents]
    best = max(s for *_, s in scored)
    if best <= 0.0:
        return {"sentences": [], "mode": "whole", "scores": []}

    # 阈值取最高分的一半，但至少 0.9：低于这个分数的句子基本只是共用了
    # "model / transformer" 这类大词，标出来反而误导。
    cutoff = max(0.9, best * 0.55)
    picked = [x for x in scored if x[3] >= cutoff]
    picked.sort(key=lambda x: x[3], reverse=True)
    picked = picked[: max(1, limit)]
    picked.sort(key=lambda x: x[1])          # 回到原文顺序，前端按顺序画，不跳来跳去

    # 只挑出一句、且这句几乎占了整段（>70% 字符）时，等于还是整段，改为 whole
    total = sum(len(s) for s, *_ in sents) or 1
    if len(picked) == 1 and len(picked[0][0]) / total > 0.7:
        return {"sentences": [], "mode": "whole", "scores": [round(x[3], 3) for x in picked]}

    return {
        "sentences": [x[0] for x in picked],
        "mode": "sentence",
        "scores": [round(x[3], 3) for x in picked],
    }


def annotate_focus(evidence: list[dict], question: str, answer: str = "", limit: int = 2) -> list[dict]:
    """给每条证据补上 `focus`（关键句）与 `focus_mode`（sentence/whole）。

    不改变 evidence 的任何原有字段，也不落库：句级定位依赖"这一次的问题和答案"，
    换一个问题就该重新算，缓存下来只会让下次高亮错位。
    """
    for ev in evidence or []:
        try:
            r = focus_spans(ev.get("text") or "", question, answer, limit=limit)
            ev["focus"] = r["sentences"]
            ev["focus_mode"] = r["mode"]
        except Exception:  # noqa: BLE001 单条失败不能影响整轮回答
            ev["focus"] = []
            ev["focus_mode"] = "whole"
    return evidence
