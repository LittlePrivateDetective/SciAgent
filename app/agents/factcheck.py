"""作者枚举类问题的确定性作答与核对。

为什么单独处理这一种：

PDF 首页的作者区在切块阶段是按版面切碎的，而「第 6 作者是谁」依赖**顺序**。
Attention 的实测结果是：证据第一块到 Jakob Uszkoreit（第 4 位）就断了，第二块
从 "oogle Brain" 开始，Aidan N. Gomez 那一整条落在缝里——模型手上根本没有第 6 位，
只能数错或者编。2026-11-14 的独立评审就抓到这里：问第 6 作者，它答成第 5 位
Llion Jones，而校验环节因为答案里没有 `[n]` 引用，完全旁观（verify 的 total=0，
连"不通过"都谈不上）。

`documents.authors` 取自 page1_lines（切块之前），顺序就是原文顺序，是这类问题
唯一可靠的依据。于是这里做两件事：

1. 问题命中「第 N 作者 / 第一作者 / 最后一位作者 / 共几位作者」时，直接按名单
   确定性作答，不消耗模型调用，也不会数错；
2. 模型自由作答时，用同一份名单核对它写出的人名，不一致就返回 `ok=False`，
   让编排层触发一次重答。

名单本身为空（首页作者区没抽出来）时一律返回 None / 不适用，把问题还给模型，
不拿空名单去"确定"一个答案。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 「共几位作者」「一共多少位作者」「how many authors」——数量类，优先判定，
# 否则会被下面的「第 N 作者」正则以 N=0 误吃。
_RE_COUNT = re.compile(
    r"(?:一共|总共|共有|共)\s*(?:有\s*)?([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(?:位|名|个)?\s*作者"
    r"|(?:多少|几)\s*(?:位|名|个)\s*作者"
    r"|\bhow many (?:authors|people)\b",
    re.I,
)
_RE_FIRST = re.compile(r"第一作者|首位作者|第一位作者|\bfirst author\b", re.I)
_RE_LAST = re.compile(r"最后(?:一)?位作者|末位作者|\blast author\b|\bcorresponding author\b", re.I)
_RE_NTH = re.compile(
    r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*(?:位|名|个)?\s*(?:作者|作 者)"
    r"|\b([0-9]{1,2})(?:st|nd|rd|th)\s+(?:author|author listed)\b",
    re.I,
)

# 单位行的形态：同一行里作者名后面跟机构名，PDF 首页几乎都是
# `Name Affiliation email`。中英文期刊都适用。
_AFFIL_HINT = re.compile(
    r"(?:University|Universit[a-z]*|Institute|Institution|College|Academy|Laborator|"
    r"Department|School of|Research|Inc\.|Ltd|Corp|Google|Microsoft|Meta |OpenAI|"
    r"DeepMind|大学|学院|研究院|研究所|实验室|公司|中心)",
)


def _to_int(s: str) -> int:
    """把「6」「六」「十二」都换成整数。超过两位的中文数字不认，返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        head, _, tail = s.partition("十")
        return _CN_DIGIT.get(head, 1 if not head else 0) * 10 + (_CN_DIGIT.get(tail, 0) if tail else 0)
    return _CN_DIGIT.get(s, 0)


def parse_author_query(question: str) -> dict | None:
    """识别"作者枚举"类问题。不是这类问题就返回 None。

    只认带**位次**或**数量**的说法。「作者的贡献是什么」这种不含序号的
    问题必须放过去走正常检索——这里多做一步判断，就多一份误伤风险。
    """
    q = question or ""
    if not q:
        return None
    if _RE_COUNT.search(q):
        return {"kind": "count"}
    if _RE_FIRST.search(q):
        return {"kind": "nth", "n": 1}
    if _RE_LAST.search(q):
        return {"kind": "last"}
    m = _RE_NTH.search(q)
    if m:
        n = _to_int(m.group(1) or m.group(2) or "")
        if n > 0:
            return {"kind": "nth", "n": n}
    return None


def _surname(name: str) -> str:
    """取姓做匹配。全名在 PDF 里可能是 `Łukasz Kaiser`、`Aidan N. Gomez`，
    而答案里常只写姓（"Gomez"）或带变音符号的写法，比全名稳。"""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return parts[-1] if parts else ""


@lru_cache(maxsize=64)
def _page1_text(pdf_path: str) -> str:
    """第 1 页纯文本，按路径缓存。只在命中作者枚举时才会走到这里。"""
    try:
        import pymupdf

        doc = pymupdf.open(pdf_path)
        try:
            return doc[0].get_text("text") if doc.page_count else ""
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 读不到就当没有，不影响作答
        return ""


def affiliation_of(name: str, pdf_path: str = "", roster: list[str] | None = None) -> str:
    """从首页原文里找这位作者的单位。

    两种版式都要照顾：
    - `Name Affiliation email` 排在同一行（多数双栏论文）；
    - `Name` 单独一行、单位另起一行（Attention 的 `Aidan N. Gomez∗†` 下面
      隔一行才是 `University of Toronto`）。

    跨行找的时候必须设边界：只往后看 3 行，一旦遇到**名单里另一位作者**就停。
    不设这个边界的话，同页十几位作者会把下一位的单位安到这一位头上，
    那比"未提及"更糟——它是错的，而且看起来很像对的。

    不做上标数字对齐（`Gomez^2` 与单位列表第 2 项）：PDF 里上标常被抽成独立
    字符甚至丢掉，硬对齐得到的机构看着确定，实际不可靠。
    """
    if not pdf_path or not Path(pdf_path).exists():
        return ""
    surname = _surname(name)
    if not surname:
        return ""
    lines = _page1_text(pdf_path).splitlines()
    others = {_surname(a).lower() for a in (roster or []) if a}
    others.discard(surname.lower())

    def clean(seg: str) -> str:
        seg = re.sub(r"\S+@\S+", " ", seg)
        seg = re.sub(r"[∗*†‡§¶\u2020\u2021\d]", " ", seg)
        seg = " ".join(seg.split()).strip(" ,;:-")
        return seg[:60] if seg and _AFFIL_HINT.search(seg) else ""

    for i, line in enumerate(lines):
        if surname not in line:
            continue
        same = clean(line.split(surname, 1)[1])
        if same:
            return same
        for nxt in lines[i + 1: i + 4]:
            seg = nxt.strip()
            if not seg:
                continue
            low = seg.lower()
            if any(o and o in low for o in others):
                break  # 到了下一位作者的地盘，说明这位没有独立单位行
            got = clean(seg)
            if got:
                return got
        return ""
    return ""


def author_answer(question: str, authors: list[str], pdf_path: str = "") -> str | None:
    """作者枚举类问题的确定性答案。不适用时返回 None。

    返回的文案要和项目的答题风格一致：第一句就给结论，后面才补依据。
    """
    q = parse_author_query(question)
    if not q or not authors:
        return None
    total = len(authors)

    if q["kind"] == "count":
        return f"这篇论文共有 {total} 位作者（按首页署名顺序）：{'、'.join(authors[:24])}。"

    n = total if q["kind"] == "last" else int(q.get("n") or 0)
    if not 1 <= n <= total:
        return None
    name = authors[n - 1]
    affil = affiliation_of(name, pdf_path, authors)
    who = f"第 {n} 作者是 {name}" + (f"，单位是 {affil}" if affil else "")
    return (
        f"{who}。这一条按原文首页的署名顺序数出（共 {total} 位作者），"
        f"与 PDF 首页作者区一致，不依赖检索到的正文片段。"
    )


def check_author_claim(question: str, answer: str, authors: list[str]) -> dict:
    """核对模型自由作答里的作者人名。

    `applicable=False` 表示"这不是作者枚举题，或名单缺失"，调用方应当原样放行；
    `ok=False` 才是"答错了"，可以触发重答。

    判定只认姓：全名在答案里可能被缩写、被翻译、带变音符；对不上姓才算错，
    宁可漏判也不要因为 `Lukasz` 少一个 `Ł` 就判模型答错。
    """
    q = parse_author_query(question)
    if not q or not authors or q["kind"] == "count":
        return {"applicable": False}
    total = len(authors)
    n = total if q["kind"] == "last" else int(q.get("n") or 0)
    if not 1 <= n <= total:
        return {"applicable": False}
    expected = authors[n - 1]
    exp_sur = _surname(expected).lower()
    low = (answer or "").lower()
    mentioned = [a for a in authors if _surname(a) and _surname(a).lower() in low]
    ok = bool(exp_sur) and exp_sur in low
    return {
        "applicable": True,
        "position": n,
        "expected": expected,
        "mentioned": mentioned[:8],
        "ok": ok,
        "detail": "" if ok else (
            f"第 {n} 位应为 {expected}，答案里出现的是"
            f"{'、'.join(mentioned[:3]) if mentioned else '未识别到名单内人名'}"
        ),
    }


# ---------------------------------------------------------------- 证据覆盖闸门
# 向量检索永远会返回 top-k，哪怕整篇文献跟问题毫无关系也能凑出十几条。
# 实测：问「对比 BERT 与 Transformer」而当前文献全文 0 处提到 BERT，
# 模型仍编出一段"承袭关系"，并挂上真实存在的 [13]；界面显示"引用校验 5/5 命中"
# ——那道校验只认编号有效，不认内容相关，反而给了用户"已验证"的错觉。
# 这里把"证据里有没有问题的主体"变成一个确定性判断，不交给模型自由发挥。

_ENTITY = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_ENTITY_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "has", "have", "can", "not", "but", "you", "how", "what", "why", "when",
    "which", "who", "use", "using", "used", "does", "did", "its", "their",
}


def question_entities(question: str) -> list[str]:
    """抽出问题里的英文专有名词/缩写（BERT / Transformer / BLEU / GPT-4）。

    只收**含大写字母**的词：这是"专有名词与缩写"的可靠特征，也正是模型最容易
    张冠李戴的对象。全小写词一律不收——实测收进来会误伤："Adam 的 beta1 是多少"
    里 beta1 是小写普通变量名，而证据把同一量写成希腊字母 β1，按字面比对必然
    判成"未提及"，把本来答得对的问题拦下来。
    """
    out: list[str] = []
    for m in _ENTITY.finditer(question or ""):
        w = m.group(0).strip("-")
        if len(w) < 3 or w.lower() in _ENTITY_STOP:
            continue
        if not any(c.isupper() for c in w):
            continue
        if w not in out:
            out.append(w)
    return out


def absent_entities(question: str, evidence: list[dict]) -> list[str]:
    """返回"问题提到、但证据文本里一次都没出现"的实体。

    只要有实体缺失就返回它——问题问的是这些词的对比，缺一个就凑不齐论据。
    大小写不敏感；连字符做归一（GPT-4 与 GPT4、BERT-base 与 BERTbase 视为同一个词），
    否则写法差异会把"文献其实讲过"的题误拦下来。
    """
    ents = question_entities(question)
    if not ents:
        return []
    blob = " ".join((e.get("text") or "") for e in evidence).lower()
    flat = blob.replace("-", "").replace(" ", "")
    miss: list[str] = []
    for e in ents:
        low = e.lower()
        if low in blob or low.replace("-", "").replace(" ", "") in flat:
            continue
        miss.append(e)
    return miss
