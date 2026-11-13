"""版面感知 PDF 解析器。

核心思路（对应评分点"文档智能解析"）：
1. 不按字符流取文本，而是取"行级区块 + 字号 + 字体 + 坐标"，先做**阅读顺序还原**（跨栏行断段 → 段内先左后右）；
2. 再按「字号一致性 + 垂直相邻」把碎片行**合并成语义区块**——这一步专治单栏标题被拆成碎片；
3. 用字号分布识别正文字号，超出阈值且符合编号/短句特征的区块判为标题，形成章节树；
4. 用字体名（CM*、Math*、Symbol）识别公式，用 Figure/Table 编号识别图表；
5. 无文本层的页判为扫描件，交给 OCR 兜底。

每个区块都带 bbox，后续 chunk 继承它 → 锚点可回跳原页高亮。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from app.core.logger import get_logger

log = get_logger(__name__)

# ---------------- 正则 ----------------
RE_NUM_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+\S")
RE_APPENDIX = re.compile(r"^(Appendix|APPENDIX)\s*[A-Z0-9]?", re.I)
RE_FIGURE = re.compile(r"^(Figure|Fig\.)\s*\d+", re.I)
# 表题的三种写法：`Table 1: …`（ACL/CVPR 常用）、`Table 1 | …`（DeepSeek 系）、
# `Table 1 Overview of …`（Llama 3）。裸编号 `Table 1` 单独成块也算（GPT-3）。
RE_TABLE = re.compile(r"^Table\s*\d+\s*(?:[:.|]|$)", re.I)
# 无标点那种要"编号后紧跟大写开头的名词短语"：正文引用写的是
# "Table 3 summarizes the performance …"（小写动词），靠这一点区分。
# 不能再用"长度 < 120"兜底——Llama 3 的表题普遍 130 字以上，会被误杀。
RE_TABLE_LOOSE = re.compile(r"^Table\s*\d+\s+[A-Z]")
RE_REF_HEAD = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d{1,3})*\.?\s+)?"
    r"(references|reference|bibliography|literature cited|works cited)\s*:?\s*$",
    re.I,
)
# 条目编号样式：数字 [12] / "12. " / 作者键 [SLA+18] / [RNSS18]
RE_REF_ITEM = re.compile(r"^\s*\[(\d{1,3})\]|^\s*(\d{1,3})\.\s+[A-Z]|^\s*\[[A-Z][A-Za-z0-9+.\-]{1,14}\]")
# 单独成块的引用标签（[SLA+18] 这种），需要粘回下一条正文
RE_REF_LABEL = re.compile(r"^\s*\[[A-Za-z][A-Za-z0-9+.\-]{0,14}\]\s*$")
RE_ARXIV = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
RE_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# 常见一级标题白名单：没有编号也当标题
L1_WORDS = {
    "abstract", "introduction", "background", "related work", "method",
    "methods", "approach", "experiments", "experiment", "results",
    "discussion", "conclusion", "conclusions", "references",
    "acknowledgments", "acknowledgements", "appendix", "limitations",
    "future work", "preliminaries", "evaluation", "analysis",
}

MATH_FONT_HINTS = ("cm", "math", "symbol", "mtmi", "mtsy", "cmsy", "cmmi", "cmsl", "msam", "msbm")


@dataclass
class Line:
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    bold: bool
    font: str


@dataclass
class Block:
    page: int
    order: int
    col: int                 # 0=左/单栏 1=右栏 -1=跨栏
    text: str
    bbox: tuple[float, float, float, float]
    kind: str = "text"       # text/heading/figure/table/formula/reference
    level: int = 0           # 标题层级，0=非标题
    size: float = 0.0
    bold: bool = False

    @property
    def anchor(self) -> str:
        x0, y0, x1, y1 = self.bbox
        return f"[p{self.page}@{int(x0)},{int(y0)},{int(x1)},{int(y1)}]"


@dataclass
class ParsedDoc:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    abstract: str = ""
    arxiv_id: str = ""
    doi: str = ""
    page_count: int = 0
    char_count: int = 0
    is_scanned: bool = False
    blocks: list[Block] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)

    def body_text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


# ---------------- 行提取 ----------------
def _extract_lines(page, pno: int) -> list[Line]:
    out: list[Line] = []
    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001 损坏页不阻断整篇
        return out

    for blk in data.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            size = max((s.get("size", 0) for s in spans), default=0)
            font = spans[0].get("font", "")
            flags = spans[0].get("flags", 0)
            bold = bool(flags & 2 ** 4) or "bold" in font.lower() or "heavy" in font.lower()
            out.append(Line(pno, tuple(line["bbox"]), text, float(size), bold, font))
    return out


def _body_size(lines: list[Line]) -> float:
    """正文字号 = 按字符数加权出现最多的字号。"""
    counter: Counter[float] = Counter()
    for ln in lines:
        counter[round(ln.size, 1)] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else 10.0


# ---------------- 栏归属 ----------------
def _detect_two_column(lines: list[Line], width: float) -> bool:
    if len(lines) < 12:
        return False
    mid = width / 2
    tol = width * 0.03
    left = sum(1 for ln in lines if ln.bbox[2] <= mid + tol)
    right = sum(1 for ln in lines if ln.bbox[0] >= mid - tol)
    return left >= 0.25 * len(lines) and right >= 0.25 * len(lines)


def _col_of(bbox, width: float, two_col: bool) -> int:
    if not two_col:
        return 0
    mid = width / 2
    tol = width * 0.035
    x0, _, x1, _ = bbox
    if x0 < mid - tol and x1 > mid + tol:
        return -1
    return 0 if x1 <= mid + tol else 1


# ---------------- 区块合并 ----------------
def _merge_lines(lines: list[Line], width: float, two_col: bool) -> list[Block]:
    """同栏、同字号、垂直相邻的行合并为一个区块。

    关键词：先合并再判标题。这样被切成 5 段的大标题会先粘回一整块，
    再被识别成 heading —— 这就是"标题碎片化"的解法。
    """
    ordered = sorted(lines, key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    blocks: list[Block] = []
    buf: list[Line] = []

    def flush() -> None:
        if not buf:
            return
        x0 = min(l.bbox[0] for l in buf)
        y0 = min(l.bbox[1] for l in buf)
        x1 = max(l.bbox[2] for l in buf)
        y1 = max(l.bbox[3] for l in buf)
        text = " ".join(l.text for l in buf).strip()
        text = re.sub(r"\s{2,}", " ", text)
        blocks.append(
            Block(
                page=buf[0].page,
                order=len(blocks),
                col=_col_of((x0, y0, x1, y1), width, two_col),
                text=text,
                bbox=(x0, y0, x1, y1),
                size=max(l.size for l in buf),
                bold=sum(1 for l in buf if l.bold) * 2 >= len(buf),
            )
        )
        buf.clear()

    for ln in lines:
        # 参考文献条目是天然硬边界：形如 [12] / 12. 开头的行必须新起一块，
        # 否则整段参考文献会被粘成一大块，条目数严重偏少。
        if RE_REF_ITEM.match(ln.text):
            flush()
            buf.append(ln)
            continue
        if not buf:
            buf.append(ln)
            continue
        prev = buf[-1]
        same_col = _col_of(prev.bbox, width, two_col) == _col_of(ln.bbox, width, two_col)
        same_size = abs(prev.size - ln.size) <= 0.6
        gap = ln.bbox[1] - prev.bbox[3]
        line_h = max(prev.bbox[3] - prev.bbox[1], 1.0)
        # 行距阈值：正常行距 < 0.8 行高；超大间距视为另一个区块
        adjacent = -1.5 <= gap <= line_h * 0.85
        # 上一行已收尾（句号/冒号）且新行像新段落 → 断开
        sentence_end = prev.text.rstrip().endswith((".", "。", "?", "！", ":", "："))
        new_para = sentence_end and gap > line_h * 0.35
        if same_col and same_size and adjacent and not new_para:
            buf.append(ln)
        else:
            flush()
            buf.append(ln)
    flush()
    return blocks


# ---------------- 阅读顺序 ----------------
def _reading_order(blocks: list[Block]) -> list[Block]:
    """跨栏区块当分隔符切段，段内先左栏后右栏，各自按 y 排序。"""
    sorted_b = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    segments: list[list[Block]] = []
    cur: list[Block] = []
    for b in sorted_b:
        if b.col == -1:
            if cur:
                segments.append(cur)
                cur = []
            segments.append([b])
        else:
            cur.append(b)
    if cur:
        segments.append(cur)

    out: list[Block] = []
    for seg in segments:
        for b in sorted(seg, key=lambda x: (x.col, x.bbox[1], x.bbox[0])):
            out.append(b)
    for i, b in enumerate(out):
        b.order = i
    return out


# ---------------- 分类 ----------------
# 表格行/数据行特征：出现这些符号基本可以判定不是标题
NOT_HEADING_CHARS = ("·", "[", "%", "±", "→", "↑", "↓", "≤", "≥", "<", ">", "=", "|", "／")


def _is_heading(block: Block, body: float) -> tuple[bool, int]:
    txt = block.text.strip()
    if not txt or len(txt) > 220:
        return False, 0
    # arXiv 水印 / 页眉 / 纯数字行一律不是标题
    if txt.startswith("arXiv:") or RE_ARXIV.match(txt) or "/" in txt[:6]:
        return False, 0
    if sum(c.isdigit() for c in txt) > len(txt) * 0.4:
        return False, 0
    if any(c in txt for c in NOT_HEADING_CHARS):
        return False, 0
    # 邮箱行、以冒号收尾的标签行（"Encoder:"）都不是标题
    if "@" in txt or txt.endswith(":"):
        return False, 0
    # 纯人名行：只在第 1 页作者区（标题下方、摘要上方）判定。
    # 不能全局用，否则 "Model Architecture" 这种两词标题会被误杀。
    if block.page == 1 and block.bbox[1] < 300 and NAME_RE.match(txt):
        return False, 0
    # 短且带数字却不是编号开头 → 表格数据行（"WSJ 23 F1"）
    if len(txt) < 30 and any(c.isdigit() for c in txt) and not RE_NUM_HEADING.match(txt):
        return False, 0

    low = txt.lower().rstrip(" :")
    # 参考文献标题单独处理
    if RE_REF_HEAD.match(txt):
        return True, 1

    m = RE_NUM_HEADING.match(txt)
    if m and len(txt) < 120:
        depth = m.group(1).count(".") + 1
        # 编号 + 字号偏大 或 加粗 → 判标题；且编号后的文字必须像短语（首字母大写、不含小数点续写）
        rest = txt[len(m.group(1)) :].lstrip(". ")
        looks_like_title = bool(rest) and rest[0].isupper() and not re.match(r"^\d", rest)
        if looks_like_title and block.size >= body * 0.98:
            return True, min(depth, 4)

    if RE_APPENDIX.match(txt) and len(txt) < 100:
        return True, 1

    # 加粗 + 短句 + 不带句号 → 二级标题。
    # Attention 这类论文的 "3.1 Encoder and Decoder Stacks" 字号与正文几乎一致，
    # 唯一可靠的信号是加粗，只能靠 bold 兜住。
    if block.bold and block.size >= body * 0.95 and len(txt) < 90 and not txt.endswith("."):
        if m:
            return True, min(m.group(1).count(".") + 1, 4)
        if txt[0].isupper():
            return True, 2

    # 白名单关键词 + 短句 + 字号不小
    if low in L1_WORDS and block.size >= body * 1.02:
        return True, 1

    # 纯字号驱动（大标题无编号，例如论文题目、章节大字）
    if block.size >= body * 1.35 and len(txt) < 80 and not txt.endswith("."):
        return True, 1
    if block.size >= body * 1.12 and len(txt) < 60 and txt[0].isupper() and not txt.endswith("."):
        return True, 2
    return False, 0


# 希腊字母整段范围（U+0370–U+03FF）+ 常见数学算符。原先手写的那串只覆盖了
# αβγδθλμπσφψω，恰好漏掉 χ、ϵ、ϕ、∇ —— 而 χ² 检验、Adam 的 ϵ、势函数 ϕ
# 在论文里非常常见，正是被漏掉的那几个符号让整行公式被判成正文。
_RE_GREEK = re.compile(r"[\u0370-\u03ff]")
_RE_MATH_OP = re.compile(r"[=+×÷∑∏∫√≤≥≠≈≡∂∇∞±∈∉⊂⊃∀∃→←↔·⋅−]")


def looks_formula_text(txt: str) -> bool:
    """纯文本判据：这一行是不是"应当原样保留、不要翻译"的公式。

    与老的 `_looks_formula` 的区别是加了**符号密度**，并显式豁免"成句的正文"：
    - `χi χi j Cov[yi, yj | x] φ2` 这类块，希腊字母 3 个、长度 28，密度 0.11 → 公式；
    - `We used the Adam optimizer [18] with β1 = 0.9, β2 = 0.98 and ϵ = 10−9.`
      符号也有 5 个，但小写英文词有 7 个且以句号结尾 → 正文，必须翻译。

    这条判据同时被翻译层（`bilingual.keep_verbatim`）和入库前的重分类脚本复用，
    所以写成只依赖文本的纯函数——两边各写一份，迟早会出现"解析说公式、翻译说正文"。
    """
    t = (txt or "").strip()
    if not t or len(t) > 200:
        return False
    sym = (len(_RE_MATH_OP.findall(t)) + len(_RE_GREEK.findall(t))
           + t.count("^") + t.count("_"))
    if sym < 2:
        return False
    low = re.findall(r"\b[a-z]{3,}\b", t)
    # 成句正文：小写英文词成串（≥5 个）就按正文处理，即使符号很多。
    # 不要求"以句号结尾"——实测 `Now let's perform some algebraic manipulations
    # on the loss function. We seek:` 以冒号收尾，按结尾判会把整段正文吞成公式，
    # 翻译时原样保留英文，用户看到的是"这一页没翻"。
    if len(low) >= 5:
        return False
    density = sym / max(len(t), 1)
    return density >= 0.08 or (len(low) <= 1 and sym >= 3)


def _looks_formula(block: Block) -> bool:
    """块级判据 = 纯文本判据 + 老的长度/词比规则（保留以兼容历史样本）。"""
    txt = block.text
    if looks_formula_text(txt):
        return True
    if len(txt) > 160:
        return False
    words = re.findall(r"[A-Za-z]{4,}", txt)
    ratio = len(words) / max(len(txt.split()), 1)
    symbols = sum(txt.count(c) for c in "=+−×÷∑∫√≤≥∂αβγδθλμπσφψωΔΣΠΩ")
    return symbols >= 2 and ratio < 0.45


def classify(blocks: list[Block], body: float) -> list[Block]:
    in_refs = False
    for b in blocks:
        txt = b.text.strip()

        if RE_REF_HEAD.match(txt):
            in_refs = True
            b.kind, b.level = "heading", 1
            continue

        ok, lvl = _is_heading(b, body)
        if ok:
            b.kind, b.level = "heading", lvl
            # 进入参考文献区后，形如 [12] / "12. " 的条目单独归类
            if re.match(r"^(appendix|supplementary|a\s+appendix)", txt.lower()):
                in_refs = False
            continue

        # 图表编号要**先于**"参考文献区"判定：不少论文（LoRA、Qwen2.5）把附录表格
        # 排在 References 之后，按顺序判会整片归成 reference，附录表一张都进不了库。
        if RE_FIGURE.match(txt):
            b.kind = "figure"
            continue
        if RE_TABLE.match(txt) or RE_TABLE_LOOSE.match(txt):
            b.kind = "table"
            continue

        # 参考文献区：不同出版社的编号样式差异极大（[12] / [SLA+18] / 作者-年份），
        # 逐条按正则判会漏一大半。既然已经进入 References 区，就整段视为条目，
        # 由 _extract_references 按长度和标签再清洗。
        if in_refs:
            if len(txt) >= 25 and not re.fullmatch(r"[\d\W]{0,8}", txt):
                b.kind = "reference"
            continue

        if _looks_formula(b):
            b.kind = "formula"
            continue
    return blocks


# ---------------- 表格网格重建 ----------------
@dataclass
class _Cell:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


def _cluster_rows(cells: list[_Cell], overlap_ratio: float = 0.5) -> list[dict]:
    """把单元格按**垂直重叠**聚成行。

    不能用"y 中心距离 < N"：表格里 `O(n2 · d)` 带上标，bbox 顶边比同行的
    `Self-Attention` 高 4pt，中心距离会被上标拉偏；而两行之间只隔 1.8pt，
    阈值稍大就把上下两行并了。纵向重叠比例对这两种情况都稳。
    """
    rows: list[dict] = []
    for c in sorted(cells, key=lambda c: (c.y0, c.x0)):
        for r in rows:
            ov = min(r["y1"], c.y1) - max(r["y0"], c.y0)
            h = min(r["y1"] - r["y0"], c.y1 - c.y0)
            if h > 0 and ov / h >= overlap_ratio:
                r["cells"].append(c)
                r["y0"] = min(r["y0"], c.y0)
                r["y1"] = max(r["y1"], c.y1)
                break
        else:
            rows.append({"y0": c.y0, "y1": c.y1, "cells": [c]})
    rows.sort(key=lambda r: r["y0"])
    return rows


def _column_spans(cells: list[_Cell], min_gap: float = 5.0) -> list[tuple[float, float]]:
    """用"整块区域内的空白"切列，而不是聚类 x 坐标。

    换成聚类中心会很难定阈值：Table 1 第一列的四个标签中心分别是
    144 / 147 / 153 / 176（"Self-Attention (restricted)" 最宽），跨度 32pt；
    而第一列与第二列只隔 30pt。按中心聚，阈值取 32 会并列、取 30 会拆列。
    改成"所有格子的水平区间取并集，并集之间剩下的空白就是列缝"，
    就不用赌阈值——空白是客观存在的。
    """
    segs = sorted((c.x0, c.x1) for c in cells)
    if not segs:
        return []
    out = [[segs[0][0], segs[0][1]]]
    for a, b in segs[1:]:
        if a - out[-1][1] <= min_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _col_index(cell: _Cell, spans: list[tuple[float, float]]) -> int:
    cx = (cell.x0 + cell.x1) / 2
    for i, (a, b) in enumerate(spans):
        if a <= cx <= b:
            return i
    # 落在列缝里（数字比表头窄、且右对齐）：归到最近的一列
    return min(range(len(spans)), key=lambda i: min(abs(cx - spans[i][0]), abs(cx - spans[i][1])))


def _table_grid(caption: Block, lines: list[Line], width: float) -> tuple[str, tuple] | None:
    """从表题下方的原始行重建网格，返回 (按行文本, 包围盒)。

    重建放在**区块合并之前的信息**上做：`_merge_lines` 会把
    "Maximum Path Length" / "Operations" / "Self-Attention" 粘成一个块，
    列归属就此丢失（D-68）。这里直接读 `Line`，每格还带着自己的 bbox。
    """
    x0, y0, x1, y1 = caption.bbox
    span = max(x1 - x0, 1.0)
    cand: list[_Cell] = []
    for ln in lines:
        if ln.bbox[1] < y1 - 1 or ln.bbox[1] > y1 + 420:
            continue
        w = max(ln.bbox[2] - ln.bbox[0], 1.0)
        overlap = min(ln.bbox[2], x1) - max(ln.bbox[0], x0)
        # 双栏排版里另一栏的正文不能混进来；表题通常与表格同宽
        if overlap < min(60.0, 0.35 * w):
            continue
        cand.append(_Cell(ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3], ln.text.strip()))
    if len(cand) < 4:
        return None

    rows = _cluster_rows(cand)
    heights = sorted(r["y1"] - r["y0"] for r in rows)
    med_h = heights[len(heights) // 2] or 9.0

    taken: list[dict] = []
    prev_y1 = None
    skipped = 0
    for i, r in enumerate(rows):
        # 与上一行之间出现明显空白 → 表格结束（正文与表格之间至少有 2 行间隔）
        if prev_y1 is not None and r["y0"] - prev_y1 > med_h * 2.5:
            break
        if len(r["cells"]) >= 2:
            taken.append(r)
        else:
            if not taken and skipped < 2:
                # 表题最后一行常被切成独立块（"not be compared to per-word …"），
                # 它落在表题下方但不是表格内容。允许跳过开头至多两行这种残句，
                # 再往后遇到单格行就当表格已结束。
                skipped += 1
                prev_y1 = r["y1"]
                continue
            # 单格行只在"下一行是多格"且贴得很近时才算表头续行
            # （"Sequential / Operations" 这种折行表头）
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt and len(nxt["cells"]) >= 2 and nxt["y0"] - r["y1"] <= med_h * 1.2:
                taken.append(r)
            else:
                break
        prev_y1 = r["y1"]

    if len(taken) < 2:
        return None
    # 列边界只用**密集行**推：合并表头（"BLEU" 横跨 EN-DE/EN-FR 两列、
    # "Training Cost (FLOPs)" 横跨两列）会把两列之间的空白填掉，
    # 一旦参与取并集，两个子列就被并成一列（Table 2 实测）。
    max_cells = max(len(r["cells"]) for r in taken)
    dense = [
        c
        for r in taken
        if len(r["cells"]) >= max(2, int(max_cells * 0.6))
        for c in r["cells"]
    ]
    spans = _column_spans(dense or [c for r in taken for c in r["cells"]])
    if len(spans) < 2:
        return None

    rendered: list[list[str]] = []
    for r in taken:
        bucket: list[list[str]] = [[] for _ in spans]
        for c in r["cells"]:
            bucket[_col_index(c, spans)].append(c.text)
        cells_txt = [" ".join(v) for v in bucket]
        filled = sum(1 for v in cells_txt if v)
        # 表头折行只在**开头两行内**合并；否则 Table 3 里那些只填了两三格的
        # 数据行（"2 | 5.01 | 25.1"）会被并进上一行，六行糊成一团
        if rendered and len(rendered) <= 2 and filled <= max(1, len(spans) // 3):
            for k, v in enumerate(cells_txt):
                if v:
                    rendered[-1][k] = f"{rendered[-1][k]} {v}".strip()
            continue
        rendered.append(cells_txt)

    body = "\n".join(" | ".join(v for v in row if v) for row in rendered)
    if not body.strip():
        return None
    box = (
        min(c.x0 for r in taken for c in r["cells"]),
        min(c.y0 for r in taken for c in r["cells"]),
        max(c.x1 for r in taken for c in r["cells"]),
        max(c.y1 for r in taken for c in r["cells"]),
    )
    return body, box


def _rebuild_tables(
    blocks: list[Block], lines_by_page: dict[int, list[Line]], widths: dict[int, float]
) -> list[Block]:
    """把 table 块（表题）与它下方的网格行合并成一个**结构完整的表格块**。

    不这么做的话，网格行会被 `_merge_lines` 粘成若干段落块，列归属丢失：
    模型看到 `O(n2 · d)` 却不知道它属于 Self-Attention（D-68）。
    """
    consumed: set[int] = set()
    for b in blocks:
        if b.kind != "table":
            continue
        lines = lines_by_page.get(b.page)
        if not lines:
            continue
        grid = _table_grid(b, lines, widths.get(b.page, 600.0))
        if not grid:
            continue
        body, box = grid
        b.text = f"{b.text.strip()}\n{body}"
        b.bbox = (
            min(b.bbox[0], box[0]),
            min(b.bbox[1], box[1]),
            max(b.bbox[2], box[2]),
            max(b.bbox[3], box[3]),
        )
        for nb in blocks:
            if nb is b or nb.page != b.page or nb.kind in {"heading", "table", "figure", "reference"}:
                continue
            yc = (nb.bbox[1] + nb.bbox[3]) / 2
            if box[1] - 1 <= yc <= box[3] + 1:
                consumed.add(id(nb))
    if not consumed:
        return blocks
    log.debug("表格网格重建：合并 %d 个网格块", len(consumed))
    return [b for b in blocks if id(b) not in consumed]


def _merge_number_prefix(blocks: list[Block]) -> list[Block]:
    """把单独成块的章节号（"3"、"3.2"）粘回后面的标题块。

    很多 LaTeX 模板把编号和标题分开放，不处理的话会出现一堆 "2"、"3" 的孤立块，
    章节树也就拿不到层级。
    """
    for i in range(len(blocks) - 1):
        b, nxt = blocks[i], blocks[i + 1]
        if (
            b.kind == "text"
            and re.fullmatch(r"\d{1,2}(?:\.\d{1,3}){0,3}", b.text.strip())
            and nxt.kind == "heading"
            and nxt.page == b.page
            and nxt.col == b.col
        ):
            num = b.text.strip()
            nxt.text = f"{num} {nxt.text.strip()}"
            # 编号是层级的权威来源：3.2.1 → level 3，不能被加粗规则判出的 2 覆盖
            nxt.level = min(num.count(".") + 1, 4)
            b.text = ""  # 标记为已消费
    return [b for b in blocks if b.text.strip()]


# ---------------- 元信息 ----------------
def _split_authors(text: str) -> list[str]:
    text = re.sub(r"[\d\*†‡§¶#,]+", " ", text)
    parts = re.split(r",| and |;|\u2020|\u2021", text)
    out = []
    for p in parts:
        p = p.strip(" .*0123456789")
        if 1 < len(p) <= 40 and not RE_EMAIL.search(p):
            out.append(p)
    return out[:20]


ORG_RE = re.compile(
    r"(Google|University|Universit|Institute|Research|Department|Inc\.?|Ltd\.?|Brain|College|"
    r"School|Laborator|Labs|MIT|CSAIL|Corporation|Corp\.?|Academy|Microsoft|Meta|OpenAI|DeepMind)"
)
NAME_RE = re.compile(r"^[A-Z][A-Za-z\.\-']*(?:\s+[A-Z][A-Za-z\.\-']*){1,3}$")


def _names_from_block(text: str) -> tuple[list[str], list[str]]:
    """从"姓名 + 单位 + 邮箱"混排的区块里拆出人名与单位。

    Attention 这类论文的作者是网格排布，PyMuPDF 抽出来是
    "Llion Jones∗ Google Research llion@google.com" 这种一行一个作者，
    所以按"邮箱前的单位关键词"切开，姓名取单位之前的部分。
    """
    clean = RE_EMAIL.sub(" ", text)
    clean = re.sub(r"[∗†‡§¶#\*]", " ", clean)

    names: list[str] = []
    orgs: list[str] = []
    pieces = ORG_RE.split(clean)
    head, tail = pieces[0], pieces[1:]
    if head.strip():
        for cand in re.split(r",| and |;", head):
            cand = re.sub(r"\s+", " ", cand).strip(" .0123456789")
            if NAME_RE.match(cand) and len(cand) <= 40:
                names.append(cand)
    if tail:
        orgs.append(ORG_RE.sub(lambda m: m.group(0), clean).strip()[:200])
    return names, orgs


def _extract_meta(
    parsed: ParsedDoc, page1_blocks: list[Block], body: float, page1_lines: list[Line] | None = None
) -> None:
    """从第 1 页区块里抽标题 / 作者 / 单位 / 摘要。"""
    if not page1_blocks:
        return

    # 标题：第 1 页字号最大的区块，排除 arXiv 水印、页眉、超短行
    cand = [
        b
        for b in page1_blocks
        if not b.text.startswith("arXiv:")
        and not RE_ARXIV.match(b.text)
        and len(b.text.strip()) >= 6
        and b.kind in ("text", "heading")
    ]
    title_blk = None
    if cand:
        title_blk = max(cand, key=lambda b: (b.size, -b.bbox[1]))
        if title_blk.size >= body * 1.1 and len(title_blk.text) < 300:
            parsed.title = title_blk.text.strip()

    # 作者窗口：标题底边 → Abstract 顶边
    abstract_blk = next(
        (b for b in page1_blocks if b.kind == "heading" and b.text.lower().startswith("abstract")), None
    )
    if title_blk:
        top = title_blk.bbox[3] - 2
        bottom = abstract_blk.bbox[1] if abstract_blk else title_blk.bbox[3] + 320

        # 优先用行级数据：作者是按"一人一行/一人一格"排的，块合并后会把两个人粘在一起
        if page1_lines:
            for ln in page1_lines:
                if not (top <= ln.bbox[1] <= bottom):
                    continue
                nm, og = _names_from_block(ln.text)
                parsed.authors.extend(nm)
                parsed.affiliations.extend(og)
        else:
            for b in page1_blocks:
                if not (top <= b.bbox[1] <= bottom):
                    continue
                nm, og = _names_from_block(b.text)
                parsed.authors.extend(nm)
                parsed.affiliations.extend(og)

    # 去重保序
    parsed.authors = list(dict.fromkeys(parsed.authors))[:24]
    parsed.affiliations = list(dict.fromkeys(parsed.affiliations))[:12]

    # 摘要：Abstract 标题后到下一个标题前的连续正文
    for i, b in enumerate(page1_blocks):
        if b.kind == "heading" and b.text.strip().lower().startswith("abstract"):
            buf = []
            for nb in page1_blocks[i + 1 :]:
                if nb.kind == "heading" or nb.text.lower().startswith("introduction"):
                    break
                buf.append(nb.text)
                if sum(len(x) for x in buf) > 2500:
                    break
            parsed.abstract = " ".join(buf).strip()
            break

    # arXiv 号 / DOI 只从首页取：全文搜索会命中参考文献里"被引论文"的编号，
    # 把本文误判成另一篇的同族版本（扫描件 OCR 后尤其容易踩到，页眉水印常被糊掉）。
    head = " ".join(b.text for b in page1_blocks)[:4000]
    m = RE_ARXIV.search(head)
    if m:
        parsed.arxiv_id = m.group(1) + (m.group(2) or "")
    m = RE_DOI.search(head)
    if m:
        parsed.doi = m.group(0).rstrip(".")


def _extract_references(blocks: list[Block]) -> list[dict]:
    """从已标记为 reference 的区块里清洗出参考文献条目。

    出版社差异：有的用 [12]、有的用 [SLA+18]、有的纯作者-年份。
    这里不依赖编号样式，改用"含四位年份 / 含方括号标签"做判定，
    再把单独成块的标签（[SLA+18] 独占一块）粘回下一条正文。
    """
    refs: list[dict] = []
    pending_label = ""

    for b in blocks:
        if b.kind != "reference":
            continue
        txt = b.text.strip()

        if RE_REF_LABEL.match(txt):
            pending_label = txt
            continue

        if len(txt) < 25:
            continue
        if re.fullmatch(r"[\d\s.\-–—]+", txt):  # 纯页码 / 目录行
            continue
        if not re.search(r"(?:19|20)\d{2}", txt) and not txt.startswith("["):
            continue

        if pending_label and not txt.startswith("["):
            txt = f"{pending_label} {txt}"
        pending_label = ""

        idx = len(refs) + 1
        m = RE_REF_ITEM.match(txt)
        if m:
            for g in m.groups():
                if g and g.isdigit():
                    idx = int(g)
                    break

        refs.append(
            {
                "index": idx,
                "text": txt,
                "page": b.page,
                "bbox": [round(v, 1) for v in b.bbox],
                "anchor": b.anchor,
            }
        )
    return refs


# ---------------- 主入口 ----------------
def parse_pdf(path: str, max_pages: int | None = None) -> ParsedDoc:
    """解析整篇 PDF。max_pages 用于长书抽样或快速预览。"""
    doc = pymupdf.open(path)
    parsed = ParsedDoc(page_count=doc.page_count)
    total_chars = 0
    empty_pages = 0
    page1_lines: list[Line] = []
    size_counter: Counter[float] = Counter()
    lines_by_page: dict[int, list[Line]] = {}
    page_widths: dict[int, float] = {}

    parsed_pages = 0
    for pno, page in enumerate(doc, start=1):
        if max_pages and pno > max_pages:
            break
        parsed_pages += 1

        lines = _extract_lines(page, pno)
        page_chars = sum(len(l.text) for l in lines)
        total_chars += page_chars
        if page_chars < 30:
            empty_pages += 1

        if not lines:
            continue

        size_counter_local = {round(ln.size, 1): 0 for ln in lines}
        for ln in lines:
            size_counter_local[round(ln.size, 1)] = size_counter_local.get(round(ln.size, 1), 0) + len(ln.text)
        size_counter.update(size_counter_local)
        two_col = _detect_two_column(lines, page.rect.width)
        blocks = _merge_lines(lines, page.rect.width, two_col)
        blocks = _reading_order(blocks)
        if pno == 1:
            page1_lines = lines
        # 表格重建要用"合并前的原始行"——合并一旦发生，列归属就没了（D-68）
        lines_by_page[pno] = lines
        page_widths[pno] = page.rect.width
        parsed.blocks.extend(blocks)

    doc.close()

    # 分类必须**全文一遍过**：参考文献区是跨页状态，逐页分类会导致
    # 第 2 页之后的参考文献被误判成正文（实测 40 条只认出 14 条）。
    global_body = size_counter.most_common(1)[0][0] if size_counter else 10.0
    # order 必须全局连续：_reading_order 是按页编号的，不重排会让
    # build_section_paths 的映射被后一页覆盖（表现为所有章节路径都指向附录）。
    for i, b in enumerate(parsed.blocks):
        b.order = i
    parsed.blocks = classify(parsed.blocks, global_body)
    parsed.blocks = _rebuild_tables(parsed.blocks, lines_by_page, page_widths)
    parsed.blocks = _merge_number_prefix(parsed.blocks)
    # 表格重建会吃掉网格块，order 必须重新编号，否则章节路径映射对不上
    for i, b in enumerate(parsed.blocks):
        b.order = i

    parsed.char_count = total_chars
    parsed.is_scanned = parsed_pages > 0 and empty_pages >= max(1, int(parsed_pages * 0.6))

    page1 = [b for b in parsed.blocks if b.page == 1]
    body1 = _body_size(
        [Line(1, b.bbox, b.text, b.size, False, "") for b in page1] or [Line(1, (0, 0, 0, 0), "x", 10, False, "")]
    )
    _extract_meta(parsed, page1, body1, page1_lines)
    parsed.references = _extract_references(parsed.blocks)

    # 章节树
    for b in parsed.blocks:
        if b.kind == "heading":
            parsed.sections.append(
                {
                    "level": max(1, b.level),
                    "title": b.text.strip()[:300],
                    "page": b.page,
                    "order": b.order,
                    "bbox": [round(v, 1) for v in b.bbox],
                    "anchor": b.anchor,
                }
            )

    log.info(
        "解析完成 %s：%d 页 / %d 区块 / %d 标题 / %d 参考文献 / 扫描件=%s",
        path.split("/")[-1], parsed.page_count, len(parsed.blocks),
        len(parsed.sections), len(parsed.references), parsed.is_scanned,
    )
    return parsed


def _fallback_ocr_references(blocks: list[Block]) -> list[dict]:
    """OCR 参考区兜底：标题与条目常粘成同一块（"References\\n[1] ..."）。

    `RE_REF_HEAD` 要求整块文本就是 "References"，逐块匹配会把整个参考区漏掉，
    实测扫描件 refs=0 而正文里明明有 [1]–[36]（QA A05）。
    这里改为按 `[n]` 标签切分参考区整段文本，不依赖分块边界。
    """
    start = None
    for i, b in enumerate(blocks):
        first = (b.text.strip().splitlines() or [""])[0].strip()
        if RE_REF_HEAD.match(first):
            start = i
            break
    if start is None:
        return []

    tail = "\n".join(b.text for b in blocks[start:])
    lines = tail.splitlines()
    if lines and RE_REF_HEAD.match(lines[0].strip()):
        tail = "\n".join(lines[1:])

    parts = re.split(r"\[(\d{1,3})\]", tail)
    refs: list[dict] = []
    seen: set[int] = set()
    for j in range(1, len(parts), 2):
        idx = int(parts[j])
        body = re.sub(r"\s+", " ", parts[j + 1] if j + 1 < len(parts) else "").strip()
        body = re.sub(r"^\d{1,2}\s+", "", body).strip()
        if idx in seen or not (20 <= len(body) <= 1000):
            continue
        if not re.search(r"(?:19|20)\d{2}", body) and "arxiv" not in body.lower():
            continue
        seen.add(idx)
        b0 = blocks[start]
        refs.append({
            "index": idx, "text": f"[{idx}] {body}",
            "page": b0.page, "bbox": [round(v, 1) for v in b0.bbox],
            "anchor": b0.anchor,
        })
    if refs:
        for b in blocks[start + 1:]:
            if b.kind == "text":
                b.kind = "reference"
    return refs


def finalize_ocr_structure(parsed: ParsedDoc) -> ParsedDoc:
    """OCR 回填后的结构重建：保留标题层级，补分类 / 章节树 / 参考文献 / 元信息。

    不能直接复用 parse_pdf 里的分类流程：那套逻辑靠字号加粗判标题，
    而 OCR 的层级来自 Markdown 标记（更可靠），所以这里保留既有 heading，
    只对正文块做图表 / 参考文献归类。
    """
    # 1) 参考文献区：从 "References" 标题之后的 [n] 条目整段标记
    in_refs = False
    for b in parsed.blocks:
        t = b.text.strip()
        if b.kind == "heading" and RE_REF_HEAD.match(t):
            in_refs = True
            continue
        if b.kind == "heading" and in_refs and re.match(r"^(appendix|a\.|supplementary)", t.lower()):
            in_refs = False
            continue
        if in_refs and RE_REF_ITEM.match(t):
            b.kind = "reference"
        elif b.kind == "text":
            if RE_FIGURE.match(t):
                b.kind = "figure"
            elif RE_TABLE.match(t):
                b.kind = "table"

    # 2) 章节树
    parsed.sections = []
    for b in parsed.blocks:
        if b.kind == "heading":
            parsed.sections.append(
                {
                    "level": max(1, b.level),
                    "title": b.text.strip()[:300],
                    "page": b.page,
                    "order": b.order,
                    "bbox": [round(v, 1) for v in b.bbox],
                    "anchor": b.anchor,
                }
            )

    # 3) 参考文献（OCR 分块把标题和条目粘在一起时走兜底切分）
    parsed.references = _extract_references(parsed.blocks)
    if not parsed.references:
        parsed.references = _fallback_ocr_references(parsed.blocks)

    # 4) 标题 / 作者 / 摘要：首页区块（OCR 文本前几行就是标题与作者）
    page1 = [b for b in parsed.blocks if b.page == 1]
    if page1:
        try:
            _extract_meta(parsed, page1, 10.0, [])
        except Exception as exc:  # noqa: BLE001 元信息抽不到不能挡住入库
            log.warning("扫描件元信息抽取失败：%s", exc)

    # OCR 输出的标题往往没有 # 标记（模型只给章节加），字号全是 10，
    # 靠字号判标题会落空。这里退一步：取首页前几个块里最像标题的一行。
    if not (parsed.title or "").strip():
        for b in page1[:4]:
            t = b.text.strip()
            if 8 <= len(t) <= 200 and not t.lower().startswith("arxiv") and "@" not in t:
                parsed.title = t
                break

    log.info(
        "扫描件结构重建：%d 区块 / %d 标题 / %d 参考文献",
        len(parsed.blocks), len(parsed.sections), len(parsed.references),
    )
    return parsed


def build_section_paths(blocks: list[Block]) -> dict[int, str]:
    """给每个区块标上 "3.2/3.2.2" 这样的章节路径，供 chunk 使用。"""
    stack: list[tuple[int, str]] = []
    mapping: dict[int, str] = {}
    for b in blocks:
        if b.kind == "heading":
            lvl = max(1, b.level)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, b.text.strip()[:60]))
        mapping[b.order] = "/".join(t for _, t in stack)
    return mapping


def render_page_png(path: str, page_no: int, dpi: int = 130) -> bytes:
    """渲染单页 PNG，供前端阅读器与锚点高亮使用（按需渲染，避免长书 OOM）。"""
    doc = pymupdf.open(path)
    try:
        if not (1 <= page_no <= doc.page_count):
            raise ValueError(f"页码越界：{page_no}/{doc.page_count}")
        pix = doc[page_no - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
