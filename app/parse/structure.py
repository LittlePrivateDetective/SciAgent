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

# 解析器版本：章节树的判定规则一变，库里已有的 sections 就过期了。
# 云端 data 目录是持久化的（换代码不换数据），所以启动时要靠这个版本号
# 判断"要不要把老文档的章节树按新规则重算一遍"。改动分类/合并逻辑必须同步 +1。
PARSE_VERSION = "2026.11.15.3"

# ---------------- 正则 ----------------
RE_NUM_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+\S")
# 中文期刊（计算机学报/自动化学报）把编号排成 "3. 1"、"3. 2. 1"——点号后面还带空格，
# 上面的正则咬不住，整篇的小节标题就全丢了（只剩 6 个一级标题）。这里单独认一次，
# 交给 _norm_num_text 归一成 "3.1"。
RE_NUM_SPACED = re.compile(r"^\s*(\d{1,2}(?:\s*[.．]\s*\d{1,2}){0,3})\s*[.．]?\s*")
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
    r"(references|reference|bibliography|literature cited|works cited|参考文献)\s*:?\s*$",
    re.I,
)
# 条目编号样式：数字 [12] / "12. " / 作者键 [SLA+18] / [RNSS18]
# 中文全角方括号（［12］）必须一起认：计算机学报全篇用全角，
# 只认半角的话整片参考文献会一条都抽不出来（实测 41 条 → 0 条）。
RE_REF_ITEM = re.compile(r"^\s*[\[［](\d{1,3})[\]］]|^\s*(\d{1,3})\.\s+[A-Z]|^\s*[\[［][A-Z][A-Za-z0-9+.\-]{1,14}[\]］]")
# 单独成块的引用标签（[SLA+18] 这种），需要粘回下一条正文
RE_REF_LABEL = re.compile(r"^\s*[\[［][A-Za-z][A-Za-z0-9+.\-]{0,14}[\]］]\s*$")
# 目录行：点线（"…… 1" / ". . . . . 15" / "····· 7"）
RE_TOC_LEADER = re.compile(r"(?:\.\s*){6,}|(?:…\s*){3,}|(?:·\s*){6,}")
# 目录里单独一行的页码
RE_TOC_PAGENO = re.compile(r"^\d{1,4}$")
# 参考文献区的"起点"：以 [n] / ［n］ 开头的条目行
RE_REF_START = re.compile(r"^\s*[\[［](\d{1,3})[\]］]")
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
    """按 bbox **中心**判栏，不能按右边缘。

    BERT 这类双栏 PDF 里，章节编号 "4" 只占 6pt 宽（x 307–313），而正文栏从 307 开始：
    用右边缘 +3.5% 容差去比，它会被判成左栏，和同一行的标题 "Experiments"（右栏）
    分到两个栏，编号因此被丢掉，章节树里只剩 "Experiments"。
    """
    if not two_col:
        return 0
    mid = width / 2
    tol = width * 0.035
    x0, _, x1, _ = bbox
    if x0 < mid - tol and x1 > mid + tol:
        return -1                      # 跨栏（标题、整宽图表）
    return 0 if (x0 + x1) / 2 < mid else 1


# ---------------- 区块合并 ----------------
def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _join_lines(buf: list[Line]) -> str:
    """按"视觉上是不是同一行"决定拼不拼空格。

    中文的这几种情况必须不带空格：一行被拆成两段（"1 引" + "言"）、
    一个名字被拆成两个（"林" + "威"）；而两个名字之间要留空格（"黄震华" + "林威"）。
    判据是水平间隙相对字号的大小：≤1.2 个字宽就没有真正的空白。
    """
    if not buf:
        return ""
    out = [buf[0].text]
    for prev, cur in zip(buf, buf[1:]):
        h_gap = cur.bbox[0] - prev.bbox[2]
        a, b = prev.text[-1:], cur.text[:1]
        tight = (_is_cjk(a) and _is_cjk(b) and h_gap <= 1.2 * max(cur.size, 1.0))
        out.append(("" if tight else " ") + cur.text)
    return "".join(out)


def _norm_num_text(txt: str) -> str:
    """把章节编号归一成 "3.1 标题"：`3. 1　标题` → `3.1 标题`。

    计算机学报的编号在每个点号后都插一个空格（"3. 2. 1　阶段1:增强数据生成"），
    既进不了 `RE_NUM_HEADING`，层级也算不出来（`count(".")` 之前就被空格打断）。
    归一只动开头那段编号，正文原样返回。
    """
    m = RE_NUM_SPACED.match(txt)
    if not m:
        return txt
    num = re.sub(r"\s*[.．]\s*", ".", m.group(1))
    rest = txt[m.end():].lstrip("\u3000 \t")
    return f"{num} {rest}".rstrip() if rest else num


def _left_edges(lines: list[Line], width: float, two_col: bool) -> dict[int, float]:
    """每一栏的左边距（用长行估：长行一定是段落，不会是缩进的列表项/表格格）。

    章节标题永远贴着栏的左边界排；表格单元格、"90 M speakers" 这类数字碎片
    散落在栏中间。用"是否从栏左起"就能把两者分开——不需要认识表结构，
    也不用为每篇 PDF 调阈值。
    """
    edges: dict[int, Counter] = {}
    for ln in lines:
        if len(ln.text.strip()) < 15:
            continue
        col = _col_of(ln.bbox, width, two_col)
        if col < 0:
            continue
        edges.setdefault(col, Counter())[round(ln.bbox[0])] += len(ln.text)
    # 取"出现最多的左边界"，不能取最小值：脚注、页边注、水印的 x 更靠左，
    # 一旦被当成栏左，所有标题都会被判成"没贴左"而消失。
    return {c: float(cnt.most_common(1)[0][0]) for c, cnt in edges.items()}


def _body_font(lines: list[Line]) -> str:
    """正文字体 = 按字符数加权出现最多的字体名。

    中文期刊的版面语言是"字体即层级"：正文用宋体（FZSSK）、小节标题用黑体（FZHTK），
    两者**字号完全相同**（都是 10.3pt）、都不加粗。所以"同字号就合并"这条规则
    必然把 "3. 1　新闻推荐模型域增量学习问题描述" 并进下面那段正文，
    标题连独立区块都不是，字号/加粗/编号三套判据全部失效。
    """
    counter: Counter[str] = Counter()
    # 只统计"像正文段落"的长行：表格里的数字行会让 NEU-BZ（数字字体）冲到第一，
    # 把"正文字体"误判成数字字体，后面所有"字体是否与正文不同"的判断就全歪了。
    for ln in lines:
        if len(ln.text.strip()) >= 15:
            counter[ln.font] += len(ln.text)
    if not counter:
        for ln in lines:
            counter[ln.font] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else ""


def _plausible_heading_tail(rest: str) -> bool:
    """编号后面的那段文字像不像标题短语。

    `_starts_like_title` 只要求首字是大写字母，于是表格单元格 "0.5 M"、"83 GB"、
    "1.0 T" 全都被认成二级标题——它们是**数量 + 单位**，不是标题。
    真正的英文标题要么带小写字母（"Experiments"），要么是多词短语
    （"LOW-RANK-PARAMETRIZED UPDATE MATRICES"）；单个大写字母/单位缩写一律否掉。
    """
    t = rest.strip(" .\u3000:：")
    if not t:
        return False
    # 中文标题两个字就够（"1 引言"、"2 结论"），英文短语要三个字符以上
    if re.search(r"[\u4e00-\u9fff]", t):
        return len(t) >= 2
    if len(t) < 3:
        return False
    words = [w for w in re.split(r"[\s\-]+", t) if w]
    # 首词是单个大写字母 = 单位/量纲（"B Model (GB)"、"M speakers"、"T Model"、"M tokens"）。
    # 冠词 "A"、"I" 例外："A Survey of …" 是正常标题。
    if words and len(words[0]) == 1 and words[0].isupper() and words[0] not in ("A", "I"):
        return False
    if re.search(r"[a-z]", t):
        return True
    # 全大写单字标题也是有的（ViT 的 "1 INTRODUCTION"、"5.1 BASELINES"），
    # 只要够长就放行；"83 GB"、"0.5 M" 这类数量+单位的短尾巴仍然被挡住。
    return len(words) >= 2 or (t.isupper() and len(t) >= 6)


def _line_is_num_heading(ln: Line, body_font: str, left: float | None = None) -> bool:
    """这一**行**是不是"编号 + 标题"——用于在合并前把它切成独立区块。"""
    txt = _norm_num_text(ln.text.strip())
    if not txt or len(txt) > 60:
        return False
    m = RE_NUM_HEADING.match(txt)
    if not m:
        return False
    rest = txt[len(m.group(1)):]
    if re.match(r"^\d", rest) or not _starts_like_title(rest):
        return False
    if not _plausible_heading_tail(rest):
        return False
    # 标题必须从栏左起排。表格单元格（"7.5 B Model (GB)"、"90 M speakers"）从栏中间开始。
    if left is not None and ln.bbox[0] > left + 1.2 * max(ln.size, 1.0):
        return False
    # 标题不以句号收尾。"…提高了6. 1 个百分点." 这种正文行会因为"编号 + 汉字"被骗进来，
    # 尾部句号是最省事的否定证据。
    if txt.endswith((".", "。")) or txt[-1] in "，、,;：:":
        return False
    # 必须有一条"这是标题"的旁证，否则正文行 "3 个模型" 也会被切成独立块
    # （中文正文偶尔会以"数字 + 空格"起句）。字体不同 / 加粗 / 短句，三条任一。
    return ln.font != body_font or ln.bold or len(txt) <= 24


def _is_toc_page(lines: list[Line]) -> bool:
    """整页是不是"书的目录"。

    目录条目在文本层里就是"编号 + 标题 + 页码"，与正文章节标题同字形同字号，
    分类器没有任何依据区分它们——Understanding Deep Learning 的章节树因此
    前半截全是目录（`1.5.1 Structured outputs`、`1.2 Unsupervised learning`…），
    还因为目录是两栏、行距紧，互相粘成 `1.5.5 1.1 New approach` 这种怪条目。

    判据只看版面，两条任一：
    1. 点线目录 ≥4 行（`1.1 Supervised learning . . . . . 1`）；
    2. 一页里 ≥8 行只有页码数字，且没有长段落——正文页不可能这样。
    正文里偶尔出现一个省略号或一个页码，都够不到阈值。
    """
    if len(lines) < 6:
        return False
    leaders = sum(1 for ln in lines if RE_TOC_LEADER.search(ln.text))
    if leaders >= 4:
        return True
    num_lines = sum(1 for ln in lines if RE_TOC_PAGENO.match(ln.text.strip()))
    longest = max(len(ln.text.strip()) for ln in lines)
    # 没有点线的目录（右对齐页码）：一页几乎全是"编号/页码"这种短行，没有一个长句。
    # 阈值必须卡紧：带公式的正文页会产出大量 "1"、"43" 这种行（公式编号），
    # 实测第 57 页就有 10 行纯数字，但它有 89 字的正文行——不能算目录。
    return num_lines >= 10 and longest <= 45


_RE_CHAPTER_NUM = re.compile(r"^(?:Chapter|CHAPTER|第)\s*(\d{1,3}|[一二三四五六七八九十]{1,3})\s*(?:章)?$")


def _drop_running_headers(blocks: list[Block]) -> list[Block]:
    """去掉页眉/页脚：书里每一页顶部都印着"当前小节名 + 页码"。

    这类页眉在文本层里长得和章节标题一模一样（`1.1 Supervised learning 3`），
    分类器必然把它们全收进章节树——Understanding Deep Learning 的树里
    「1.1 Supervised learning」因此重复了十几次，页码还粘在标题上。

    判据是"重复"：同一段文字（数字抹成 #）出现在 ≥3 个不同页面的顶部，
    那它只能是页眉，不可能是章节标题——真正的标题在最前面出现一次。
    """
    band = 150.0   # 顶部页边距带（pt）：A4/Letter 的书页眉都在 60–120pt

    def key(t: str) -> str:
        return re.sub(r"\d+", "#", t).strip().strip(".")

    pages: dict[str, set[int]] = {}
    for b in blocks:
        t = b.text.strip()
        if b.bbox[1] <= band and 0 < len(t) <= 80:
            pages.setdefault(key(t), set()).add(b.page)

    repeated = {k for k, ps in pages.items() if len(ps) >= 3 and re.search(r"\d", k)}
    # 有的书页眉只有章名、不含页码：收紧到最顶端 90pt 且重复 ≥5 次才算
    pages_top: dict[str, set[int]] = {}
    for b in blocks:
        t = b.text.strip()
        if b.bbox[1] <= 90 and 0 < len(t) <= 40:
            pages_top.setdefault(key(t), set()).add(b.page)
    repeated |= {k for k, ps in pages_top.items() if len(ps) >= 5}

    # 更直接的一条：页眉 = "小节名 + 页码"，所以文字以裸数字结尾，且位于页面顶部。
    # 章节标题不会以数字收尾，正文也不会把数字孤零零地留在行尾顶部。
    drop_ids = {id(b) for b in blocks
                if b.kind == "heading" and b.bbox[1] <= band and re.search(r"\d{1,4}$", b.text.strip())}
    if not repeated and not drop_ids:
        return blocks
    out = [b for b in blocks
           if id(b) not in drop_ids
           and not (b.bbox[1] <= band and len(b.text.strip()) <= 80 and key(b.text) in repeated)]
    dropped = len(blocks) - len(out)
    if dropped:
        log.debug("去掉页眉/页脚 %d 块（%d 种重复文字）", dropped, len(repeated))
    return out


def _merge_chapter_number(blocks: list[Block]) -> list[Block]:
    """`Chapter 1` + `Introduction` → `1 Introduction`（一级标题）。

    书里章标题是两块：小字号的 "Chapter 1" 和大字号的章名。前者不像标题
    （字号小、不加粗），后者无编号会被排成"上一节的子级"，
    于是第一章的标题在树里成了「Acknowledgments → Introduction」的子节点。
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        m = _RE_CHAPTER_NUM.match(b.text.strip())
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if m and nxt is not None and nxt.kind == "heading" and b.page == nxt.page:
            nxt.text = f"{m.group(1)} {nxt.text.strip()}"
            nxt.kind, nxt.level = "heading", 1
            nxt.bbox = (min(b.bbox[0], nxt.bbox[0]), min(b.bbox[1], nxt.bbox[1]),
                        max(b.bbox[2], nxt.bbox[2]), max(b.bbox[3], nxt.bbox[3]))
            i += 1            # 跳过 "Chapter N" 本身
            continue
        out.append(b)
        i += 1
    return out


def _merge_lines(lines: list[Line], width: float, two_col: bool) -> list[Block]:
    """同栏、同字号、垂直相邻的行合并为一个区块。

    关键词：先合并再判标题。这样被切成 5 段的大标题会先粘回一整块，
    再被识别成 heading —— 这就是"标题碎片化"的解法。
    """
    ordered = sorted(lines, key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    blocks: list[Block] = []
    buf: list[Line] = []
    body_font = _body_font(lines)
    edges = _left_edges(lines, width, two_col)

    def flush() -> None:
        if not buf:
            return
        x0 = min(l.bbox[0] for l in buf)
        y0 = min(l.bbox[1] for l in buf)
        x1 = max(l.bbox[2] for l in buf)
        y1 = max(l.bbox[3] for l in buf)
        text = _join_lines(buf)
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
        # 编号标题同理，而且**两个方向都要断**：中文期刊实测既有"标题后面紧跟正文"，
        # 也有"标题被粘到上一段末尾"（"...增量训练学习. 4. 1　知识融合"），
        # 只处理其中一个方向，另一半小节就永远进不了章节树。
        if RE_REF_ITEM.match(ln.text) or _line_is_num_heading(
                ln, body_font, edges.get(_col_of(ln.bbox, width, two_col))):
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
        # 同一行的多段：中文排版常把一行拆成好几段（"1 引"+"言"、一个名字拆成"林"+"威"），
        # 它们的 y 几乎相同、垂直重叠很大。旧判据只看"垂直间隙 ≤ 0.85 行高"，
        # 同行的 gap 是负数（-16px）直接被判成"不同行"，于是每个碎片都成了独立区块。
        # 左右两栏虽然 y 相同，但 col 不同，上一步的 same_col 已经挡住。
        v_overlap = min(prev.bbox[3], ln.bbox[3]) - max(prev.bbox[1], ln.bbox[1])
        ln_h = max(ln.bbox[3] - ln.bbox[1], 1.0)
        same_line = v_overlap > 0.5 * min(line_h, ln_h)
        # 行距阈值：正常行距 < 0.8 行高；超大间距视为另一个区块
        adjacent = same_line or (-1.5 <= gap <= line_h * 0.85)
        # 上一行已收尾（句号/冒号）且新行像新段落 → 断开
        sentence_end = prev.text.rstrip().endswith((".", "。", "?", "！", ":", "："))
        new_para = sentence_end and gap > line_h * 0.35
        # 标题行 + 紧随其后的正文段落：字号常常一样（ACL 模板里 10.9pt 的加粗小标题
        # 与 10.9pt 正文只差一个 bold），旧判据会把它并进正文，标题就此消失
        # （"3.2 Fine-tuning BERT" 就是这样丢的）。加一条：上一行像标题、这一行又更长
        # 且不加粗，就断开。
        heading_like = (prev.bold and len(prev.text.strip()) <= 80
                        and not prev.text.rstrip().endswith((".", "。", "，", ",", ";", "：", ":")))
        # "编号 + 与正文不同的字体"= 中文期刊的小节标题（黑体 vs 宋体），
        # 它与正文同字号、不加粗，只有字体这一条线索。命中就把它切成独立区块，
        # 且不把后面的正文吸进来——否则 "3. 1　新闻推荐…" 永远进不了章节树。
        # 例外：同字体的极短后续行是标题自己的折行（"1 引" + "言"），必须留着合并，
        # 否则一级标题会被截成"1 引"。
        if _line_is_num_heading(prev, body_font, edges.get(_col_of(prev.bbox, width, two_col))) \
                and not _line_is_num_heading(ln, body_font, edges.get(_col_of(ln.bbox, width, two_col))):
            tail = ln.text.strip()
            # "1 引" + "言"：标题被字间距拆成两截，两截都不是正文字体。
            # 不能比字体名（"1 " 是数字字体 NEU-HZ，"言" 是黑体 FZHTK），
            # 只能比"是不是正文字体"。
            wrapped_head = (ln.font != body_font and len(tail) <= 12
                            and not tail.endswith(("。", ".", "，", ",", "；", ";", "：", ":")))
            if not wrapped_head:
                flush()
                buf.append(ln)
                continue
        # 加粗行之后一旦不加粗就断开：ACL 模板的小标题与正文同为 10.9pt，
        # 只差一个 bold，不断开的话"3.2 Fine-tuning BERT"会被并进正文而消失。
        # 作者名同样是加粗短行，但它们在"题目—摘要"区间里，已由版面规则排除。
        if heading_like and not ln.bold:
            flush()
            buf.append(ln)
            continue
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


def _starts_like_title(s: str) -> bool:
    """标题正文的第一个"实字"符不符合标题习惯。

    中文标题（"1　引言"、"3.2　混合检索"）首字是汉字，`isupper()` 永远为假——
    原来的实现因此把中文论文的章节全部判成正文，章节树里只剩论文题目一项。
    英文仍要求首字母大写，避免把 "3.1 4.2 的结果" 这类正文碎片认成标题。
    额外的 `\\u3000`（全角空格）也要剥：中文排版里编号与标题之间用的就是它。
    """
    s = s.lstrip(". \t\u3000")
    if not s:
        return False
    ch = s[0]
    return ch.isupper() or ("\u4e00" <= ch <= "\u9fff")


def _is_heading(block: Block, body: float, left: float | None = None,
                siblings: int = 0) -> tuple[bool, int]:
    # 先归一编号（"3. 1　标题" → "3.1 标题"）：后面所有正则与层级计数都依赖它。
    # 判定成立时把归一结果写回 block.text，章节树里就不会出现"3. 1"这种断点。
    txt = _norm_num_text(block.text.strip())
    if not txt or len(txt) > 220:
        return False, 0
    # arXiv 水印 / 页眉 / 纯数字行一律不是标题
    if txt.startswith("arXiv:") or RE_ARXIV.match(txt) or "/" in txt[:6]:
        return False, 0
    if sum(c.isdigit() for c in txt) > len(txt) * 0.4:
        return False, 0
    if any(c in txt for c in NOT_HEADING_CHARS):
        return False, 0
    # 公式碎片与半句正文：真正的章节标题不会带全角逗号/顿号/分号，
    # 也不会出现求和、根号、不等号这类符号。中文期刊的公式常被拆成
    # "S( )"、"K ∑"、"务序列(D1，C1，Y1)，…" 这样的短块，字号又比正文大，
    # 不拦就会被当成小标题塞进章节树。
    if any(c in txt for c in "，、；"):
        return False, 0
    if any(c in txt for c in "∑∫√≤≥≈⊙∂±×÷≠"):
        return False, 0
    # 至少要有两个实字（汉字或字母数字）："S( )" 这种只剩一个 S
    if sum(1 for c in txt if c.isalnum() or _is_cjk(c)) < 2:
        return False, 0
    # 单个词、全大写、≤8 字符：BERT 附录里的表格列名（MNLI / QQP / RTE / WNLI）
    # 就是这种形态，加粗且字号偏大，字号规则必中。真正的章节标题不会是这种裸缩写。
    bare = txt.strip(" .:：")
    if " " not in bare and len(bare) <= 8 and bare.isupper() and bare.lower() not in (
            "abstract", "references", "appendix", "contents"):
        return False, 0
    # 版权/内容警告之类的声明行（GPT-4 技术报告的 "Content Warning: ..."）
    if re.match(r"^(content |trigger )?warning\b", txt, re.I):
        return False, 0
    # 邮箱行、以冒号收尾的标签行（"Encoder:" / "时间复杂度为："）都不是标题
    if "@" in txt or txt.rstrip().endswith((":", "：")):
        return False, 0
    # 空括号只会出现在公式碎片里："exp ( )"、"M T (a( )"
    if re.search(r"\(\s*\)", txt):
        return False, 0
    # 以运算符结尾的是被切断的半行公式（"…时间复杂度为：O (T ((m + 1) s +"）
    if txt[-1] in "+-=×÷/·(（[{":
        return False, 0
    # 私有区字符是 PDF 字体替换出来的占位符（"13. \ue00f"），肉眼看不见但会进章节树
    if re.search(r"[\ue000-\uf8ff]", txt):
        return False, 0
    # 纯人名行：只在第 1 页作者区（标题下方、摘要上方）判定。
    # 不能全局用，否则 "Model Architecture" 这种两词标题会被误杀。
    if block.page == 1 and (ORG_RE.search(txt) or (block.bbox[1] < 300 and (
            NAME_RE.match(txt) or ("," in txt and not RE_NUM_HEADING.match(txt))))):
        return False, 0
    # 短且带数字却不是编号开头 → 表格数据行（"WSJ 23 F1"）
    if len(txt) < 30 and any(c.isdigit() for c in txt) and not RE_NUM_HEADING.match(txt):
        return False, 0

    low = txt.lower().rstrip(" :")
    # 参考文献标题单独处理
    if RE_REF_HEAD.match(txt):
        return True, 1

    m = RE_NUM_HEADING.match(txt)
    # 标题独占一行、贴着栏左起排：与它共享基线还有别的文字块 → 表格行；
    # 从栏中间起排 → 也是表格单元格（"7.5 B Model (GB)"、"90 M speakers"）。
    at_left = left is None or block.bbox[0] <= left + 1.2 * max(block.size, 1.0)
    if m and len(txt) < 120:
        depth = m.group(1).count(".") + 1
        # 编号 + 字号偏大 或 加粗 → 判标题；且编号后的文字必须像短语（首字母大写、不含小数点续写）
        rest = txt[len(m.group(1)) :].lstrip(" .\u3000\t")
        looks_like_title = (_starts_like_title(rest) and not re.match(r"^\d", rest)
                            and _plausible_heading_tail(rest))
        if looks_like_title and at_left and siblings < 2 and not txt.endswith((".", "。", "，", ",")) \
                and block.size >= body * 0.98:
            return True, min(depth, 4)

    # 附录标题必须有"标题的样子"：正文里"Appendix A Notation"这种交叉引用行
    # 也会以 Appendix 开头，不加这条就会在正文中间凭空多出一节。
    if RE_APPENDIX.match(txt) and len(txt) < 100 and (block.bold or block.size >= body * 1.05):
        return True, 1

    # 加粗 + 短句 + 不带句号 → 二级标题。
    # Attention 这类论文的 "3.1 Encoder and Decoder Stacks" 字号与正文几乎一致，
    # 唯一可靠的信号是加粗，只能靠 bold 兜住。
    # 但表格里的表头/首列也全是加粗，所以上面那两条版面判据（贴栏左 + 独占一行）
    # 同样要过，否则 "4 -bit NormalFloat Quantization"（表格首列）会变成一级标题。
    if block.bold and block.size >= body * 0.95 and len(txt) < 90 and not txt.endswith(".") \
            and at_left and siblings < 2:
        if m:
            rest = txt[len(m.group(1)) :].lstrip(" .\u3000\t")
            if _starts_like_title(rest) and _plausible_heading_tail(rest):
                return True, min(m.group(1).count(".") + 1, 4)
        if _starts_like_title(txt):
            return True, 2

    # 以数字开头、尾巴却是"数量 + 单位"（"90 M speakers"、"8 A100-80GB GPUs"）：
    # 后面几套只靠字号/白名单的规则也会放行它们，这里做一次统一否决。
    if m:
        tail = txt[len(m.group(1)) :].lstrip(" .\u3000\t")
        if not _plausible_heading_tail(tail):
            return False, 0

    # 白名单关键词 + 短句 + 字号不小
    if low in L1_WORDS and block.size >= body * 1.02:
        return True, 1

    # 纯字号驱动（大标题无编号，例如论文题目、章节大字）
    if block.size >= body * 1.35 and len(txt) < 80 and not txt.endswith(".") \
            and at_left and siblings < 2:
        return True, 1
    if block.size >= body * 1.12 and len(txt) < 60 and _starts_like_title(txt) \
            and not txt.endswith(".") and at_left and siblings < 2:
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
    ref_head_found = False
    # 第 1 页"题目之下、摘要之上"是作者与单位区。中文期刊把姓名排成大字号，
    # 字号规则必然把它们判成一级标题——章节树里就会冒出"薄琳 1） 庞亮 2）"。
    # 位置是这里唯一可靠的判据，所以按版面区间排除，而不是靠字数猜。
    page1 = [b for b in blocks if b.page == 1]
    # 题目 = 第 1 页字号最大的块，但要排除 arXiv 水印：Attention 的水印是 20pt，
    # 比标题（17.2pt）还大，选错之后"题目—摘要"区间整个反了，
    # 作者区排除失效，九个作者全被当成章节标题。
    title_pool = [b for b in page1
                  if not b.text.startswith("arXiv:") and not RE_ARXIV.match(b.text)]
    title_blk = max(title_pool, key=lambda b: b.size) if title_pool else None
    abs_blk = next((b for b in page1 if _abstract_prefix(b.text)), None)
    author_band: tuple[float, float] | None = None
    if title_blk and abs_blk and abs_blk.bbox[1] > title_blk.bbox[3]:
        author_band = (title_blk.bbox[3] - 2, abs_blk.bbox[1] - 1)
    # 第 1 页一个长段落都没有 → 整页是封面 + 作者列表（BLOOM 就长这样：
    # 标题下面是"BigScience Workshop / Dataset / Tokenization"这种贡献分组，
    # 加粗、字号大、没有摘要块可以界定区间）。这种页面不产出任何章节。
    # 只有"确实存在一个大字号的题目"时，才把第 1 页当封面处理。
    # 否则（例如只有参考文献标题的页面、或者一段没有段落的样例）会连正常标题一起吞掉。
    title_like = bool(title_blk and title_blk.size >= body * 1.25)
    cover_page = title_like and not any(len(b.text.strip()) >= 300 for b in page1)

    # 每页每栏的左边距（取众数，不取最小值：脚注/水印比正文更靠左）：
    # 编号标题必须贴着栏左排，否则就是表格单元格里的数字碎片
    left_edges: dict[tuple[int, int], float] = {}
    edge_cnt: dict[tuple[int, int], Counter] = {}
    for b in blocks:
        if len(b.text.strip()) < 15 or b.col < 0:
            continue
        edge_cnt.setdefault((b.page, b.col), Counter())[round(b.bbox[0])] += len(b.text)
    for key, cnt in edge_cnt.items():
        left_edges[key] = float(cnt.most_common(1)[0][0])

    # 每块"同一基线上并排着几个别的块"：表格行的直接证据
    by_page: dict[int, list[Block]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b)
    siblings: dict[int, int] = {}
    for b in blocks:
        n = 0
        for o in by_page.get(b.page, ()):  # noqa: PLC0206 页内块数量级为百，可接受
            if o is b or o.col != b.col:
                continue
            ov = min(b.bbox[3], o.bbox[3]) - max(b.bbox[1], o.bbox[1])
            h = min(b.bbox[3] - b.bbox[1], o.bbox[3] - o.bbox[1])
            if h > 0 and ov / h > 0.5 and (o.bbox[0] > b.bbox[2] - 1 or o.bbox[2] < b.bbox[0] + 1):
                n += 1
        siblings[id(b)] = n

    for b in blocks:
        txt = b.text.strip()

        if cover_page and b.page == 1:
            continue
        # 第 1 页只认"编号章节"和"摘要"：其余都是封面信息（标题、作者、
        # "BigScience Workshop / Dataset / Tokenization" 这类贡献分组标签）。
        # 它们加粗、字号大，形态上与章节标题无法区分，只能靠版面位置否掉。
        # 但"封面区"到摘要为止：单栏论文的第 1 页往往连正文一起排，
        # "Introduction" 就在摘要下面，整页否掉会把这节的标题直接抹掉。
        if title_like and b.page == 1 and not _RE_LEAD_NUM.match(txt) and not re.match(
                r"^(abstract|摘\s*要)\b", txt, re.I):
            if abs_blk is None or b.bbox[1] < abs_blk.bbox[1]:
                continue

        # 论文题目本身不是章节：章节树应当从"1 引言"开始。
        # title_blk 只覆盖"最大的那一块"；英文标题常被拆成两行两块，
        # 所以按"第 1 页、摘要上方、字号接近题目"整片排除。
        # 只有"题目 + 摘要"都在时才敢认定为题目区：单测里那段只有参考文献的
        # 页面没有摘要，此时"最大块"就是参考文献标题，不能被当成题目跳过。
        # 题目块本身永远不是章节：题目字号通常明显大于正文（BLOOM 没有摘要块，
        # 原来的"题目—摘要区间"规则兜不住，只能直接按字号认定题目）。
        if title_like and b is title_blk:
            continue
        if author_band and (b is title_blk or (
                b.page == 1 and b.bbox[1] < abs_blk.bbox[1]
                and b.size >= title_blk.size * 0.8)):
            continue
        # 作者区排除按版面区间做，但带编号的块绝不是作者名：
        # 单栏论文的 "1 Introduction" 常常正好落在这条区间里被误杀（LayerNorm 实测）。
        if author_band and b.page == 1 and author_band[0] <= b.bbox[1] <= author_band[1] \
                and not _RE_LEAD_NUM.match(txt):
            continue

        if RE_REF_HEAD.match(txt):
            in_refs = True
            ref_head_found = True
            b.kind, b.level = "heading", 1
            continue

        ok, lvl = _is_heading(b, body, left_edges.get((b.page, b.col)), siblings.get(id(b), 0))
        if ok:
            b.text = _norm_num_text(b.text.strip())
            b.kind, b.level = "heading", lvl
            # 只认明确的参考区结束标题；跨栏续行可能因加粗/字号被误判为
            # 普通 heading，不能因此把后半篇参考文献全部截断。
            if in_refs and re.match(r"^(appendix|supplementary|a\s+appendix|作者简介|征稿启事)", txt.lower()):
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
            # 短续行也保留到篇级合并阶段；这里丢掉就再也无法拼回上一条。
            if not re.fullmatch(r"[\d\W]{0,8}", txt):
                b.kind = "reference"
            continue

        if _looks_formula(b):
            b.kind = "formula"
            continue
    # 没有任何 "References/参考文献" 标题时，退回"连续 ［n］ 条目区"识别。
    # 必须先判标题：绝大多数英文论文走上面的正规路径，不能被这条猜测规则干扰。
    if not ref_head_found:
        blocks = _infer_reference_section(blocks, body)
    # 页眉要在"排层级"之前去掉：页眉被当成标题参与层级计算，会把后面所有小节压低一级。
    blocks = _drop_running_headers(blocks)
    blocks = _normalize_headings(_demote_table_rows(blocks))
    blocks = _join_wrapped_heading(blocks)
    return _merge_chapter_number(blocks)


# 这些短标题本身就是完整章节名，绝不能被当成上一行标题的折行尾巴
_SHORT_HEAD_OK = ("参考文献", "附录", "致谢", "摘要", "作者简介", "结束语", "引言")


def _join_wrapped_heading(blocks: list[Block]) -> list[Block]:
    """把"被栏/页折断的标题尾巴"粘回标题本体。

    中文期刊的大标题常常排到一栏的末尾就断行，剩下的两三个字落到下一栏开头
    （实测："5 基于代理模型的渐进式深度集成架" + "构搜索"）。区块合并是**按页**做的，
    跨页/跨栏的两半永远合不到一起，只能在整篇分类完之后按阅读顺序补一次。
    判据放得很紧：上一标题带编号且够长、尾巴是 ≤4 个纯汉字、不是"参考文献"这类完整标题。
    """
    out = list(blocks)
    for i in range(1, len(out)):
        prev, cur = out[i - 1], out[i]
        if prev.kind != "heading" or cur.kind != "heading":
            continue
        tail = cur.text.strip()
        if not tail or len(tail) > 4 or tail in _SHORT_HEAD_OK:
            continue
        if not all(_is_cjk(c) for c in tail):
            continue
        if cur.page not in (prev.page, prev.page + 1):
            continue
        if not _RE_LEAD_NUM.match(prev.text.strip()) or len(prev.text.strip()) < 8:
            continue
        prev.text = prev.text.rstrip() + tail
        cur.text = ""          # 标记为已消费，由下面的过滤丢掉
    return [b for b in out if b.text.strip()]


_RE_BIO_STOP = re.compile(r"^(作者简介|收稿日期|责任编辑|附录|附\s*录|Abstract in English)", re.I)


def _infer_reference_section(blocks: list[Block], body: float) -> list[Block]:
    """识别"没有标题的参考文献区"，并在树里补一个「参考文献」节点。

    计算机学报这类中文期刊**不印"参考文献"四个字**（或只印在页脚/图片里），
    文献区直接从 ［1］ 开始、整片字号比正文小 2–3pt。旧逻辑靠标题正则进场，
    这里进不去 → 全篇 refs=0、章节树里也没有参考文献这一节，
    连带"引文匹配/引用关系图"整条链都空着。

    判据只看版面：连续 ≥4 个以 ［n］/ [n] 开头、字号明显小于正文的块。
    真实正文里不会连续出现这种行（正文引用写的是"文献［22-29］"，
    方括号不在行首）。
    """
    out: list[Block] = [b for b in blocks]
    inserted = False
    i = 0
    while i < len(out):
        b = out[i]
        t = b.text.strip()
        if not (b.kind == "text" and RE_REF_START.match(t) and b.size <= body * 0.92):
            i += 1
            continue
        run: list[Block] = []
        starters = 0
        j = i
        while j < len(out):
            nb = out[j]
            nt = nb.text.strip()
            if nb.kind == "heading" or nb.size > body * 0.95 or _RE_BIO_STOP.match(nt):
                break
            if RE_REF_START.match(nt):
                starters += 1
            run.append(nb)
            j += 1
        if starters < 4:
            i += 1
            continue
        x0, y0, x1, _ = b.bbox
        head = Block(
            page=b.page, order=b.order, col=b.col, text="参考文献",
            bbox=(x0, y0 - 14, x1, y0 - 1), kind="heading", level=1,
            size=round(b.size * 1.35, 1), bold=True,
        )
        for nb in run:
            nb.kind = "reference"
        # 一篇文献只有一个参考文献区：区间被正文块打断成两段时，第二段只标类型、
        # 不再造第二个标题（否则章节树里会出现两个「参考文献」）。
        if not inserted:
            out.insert(i, head)
            inserted = True
            log.debug("识别到无标题参考文献区：%d 条（第 %d 页）", starters, b.page)
        i += len(run) + 1
    return out


_RE_LEAD_NUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+\S")


def _normalize_headings(blocks: list[Block]) -> list[Block]:
    """章节树规范化：层级对齐编号、丢掉"像句子"的伪标题。

    单个 PDF 的正则修不完这类问题——39 篇里每篇的排版都有自己的怪癖：
    BERT 的编号单独占一行、GPT 的附录标题带引号、DeepSeek 的表格列名被加粗。
    与其为每篇打补丁，不如在分类之后统一做一遍"树该长什么样"的规整：

    1. 有编号的标题，层级 = 编号段数（"3.5.2" → L3）；
    2. 无编号的标题，层级 = 上一个编号标题的层级 + 1（附录里的 A/B/C 同级）；
    3. 丢掉"像句子"的伪标题：超过 110 字符、或含句号、或词数 ≥ 14；
    4. 第一层必须是 L1（"Abstract" 被排成 L2 时提回 L1）。
    """
    prev_level = 0
    prev_num_depth = 0
    for b in blocks:
        if b.kind != "heading":
            continue
        txt = b.text.strip()
        m = _RE_LEAD_NUM.match(txt)
        if m:
            depth = min(m.group(1).count(".") + 1, 4)
            b.level = depth
            prev_num_depth = depth
        elif re.match(r"^[A-Z]\.?\s+\S", txt) or re.match(r"^(Appendix|References|Acknowledg)", txt, re.I):
            b.level = 1
            prev_num_depth = 0
        else:
            b.level = min(prev_num_depth + 1, 4) if prev_num_depth else max(1, min(b.level, 2))
        # 像句子的不要
        words = len(txt.split())
        if len(txt) > 110 or "。" in txt or words >= 14:
            b.kind, b.level = "text", 0
            continue
        if b.level == 1:
            prev_num_depth = 0
        prev_level = b.level
    # 首个标题不该是 L2/L3（前面没有父级）
    for b in blocks:
        if b.kind == "heading":
            if b.level > 1:
                b.level = 1
            break
    _demote_name_runs(blocks)
    return blocks


# 标题里常见的词。人名判定用它做反证：只要命中一个，就说明这串词是章节名不是人名。
_TITLE_WORDS = {
    "of", "the", "and", "for", "with", "in", "on", "to", "a", "an", "from", "our",
    "using", "based", "via", "towards", "under", "over", "between", "beyond",
    "method", "methods", "model", "models", "approach", "analysis", "results",
    "experiment", "experiments", "learning", "work", "related", "introduction",
    "conclusion", "background", "discussion", "evaluation", "data", "system",
    "framework", "training", "overview", "appendix", "references", "abstract",
    "setup", "details", "design", "implementation", "performance", "comparison",
    "summary", "future", "limitations", "motivation", "preliminaries", "dataset",
    "datasets", "architecture", "ablation", "scaling", "optimization", "inference",
    "prompt", "prompts", "study", "studies", "review", "survey", "notes", "main",
    "results", "discussion", "related", "methodology", "empirical", "practice",
}


def _looks_person_name(txt: str) -> bool:
    """"Gordon Chi"、"Virginia Adams" 这种 2–4 个首字母大写的英文词。"""
    words = [w for w in re.split(r"[\s]+", txt.strip()) if w]
    if not (2 <= len(words) <= 4):
        return False
    if len(txt) > 32:
        return False
    for w in words:
        if w.lower() in _TITLE_WORDS:
            return False
        if not re.fullmatch(r"[A-Z][a-z'\-]{1,15}|[A-Z]\.", w):
            return False
    return True


def _demote_name_runs(blocks: list[Block]) -> None:
    """连续 ≥4 个"编号 + 人名"的标题 → 这是附录里的作者/贡献者名单，不是章节。

    实测 Phi-3 的附录把 60 多位贡献者排成 "1. Gordon Chi / 2. Virginia Adams / …"，
    编号正则一视同仁，章节树里就整片都是人名。单个 "1 Gordon Chi" 无从判断，
    但**一串**就非常明确：真正的章节标题不会连着四个人名。
    只按连续段判、并要求每个词都不是标题常用词，避免误伤
    "Introduction / Related Work / Method" 这类正常的两词标题。
    """
    run: list[Block] = []

    def flush() -> None:
        if len(run) >= 4:
            for b in run:
                b.kind, b.level = "text", 0
        run.clear()

    for b in blocks:
        if b.kind != "heading":
            continue
        # 去掉编号要看 group 边界，不能用正则 sub：`\s+\S` 会把 "1 Gordon" 的 G 一起吃掉，
        # 剩下 "ordon Chi"，人名判据直接失效。
        ntxt = _norm_num_text(b.text.strip())
        m = RE_NUM_HEADING.match(ntxt)
        name = (ntxt[len(m.group(1)):] if m else ntxt).strip(" .:：")
        if _looks_person_name(name):
            run.append(b)
        else:
            flush()
    flush()


def _demote_table_rows(blocks: list[Block]) -> list[Block]:
    """同一行并排出现 ≥3 个短标题块 → 这是被拆开的表格行（BERT 附录的 MNLI/QQP/…）。

    判据只看版式：同一页、基线相差 <3pt、彼此不重叠、文本都不超过 24 字符。
    真实章节标题不会一行并排三个。
    """
    # 注意这里取的是**所有**短块，而不是只看已判成标题的：
    # "MNLI / QQP / RTE" 先被"全大写"规则否掉，剩下的 "CoLA"（混合大小写）
    # 就会孤立成标题。按整行一起看，才能把它们一起降级。
    heads = [b for b in blocks if len(b.text.strip()) <= 24]
    by_page: dict[int, list[Block]] = {}
    for b in heads:
        by_page.setdefault(b.page, []).append(b)
    for group in by_page.values():
        group.sort(key=lambda b: b.bbox[0])
        cluster: list[Block] = []
        for b in group:
            if cluster and abs(b.bbox[1] - cluster[-1].bbox[1]) < 3 and b.bbox[0] > cluster[-1].bbox[2] - 1:
                cluster.append(b)
            else:
                if len(cluster) >= 3:
                    for c in cluster:
                        if c.kind == "heading":
                            c.kind, c.level = "text", 0
                cluster = [b]
        if len(cluster) >= 3:
            for c in cluster:
                if c.kind == "heading":
                    c.kind, c.level = "text", 0
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
    r"School|Laborator|Labs|MIT|CSAIL|Corporation|Corp\.?|Academy|Microsoft|Meta|OpenAI|DeepMind|Anthropic|Technological)"
)
NAME_RE = re.compile(r"^[^\W\d_][\w.\-']*(?:\s+[^\W\d_][\w.\-']*){1,3}$")


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
    # ACL 等模板用作者后的机构编号分隔网格作者，而不是逗号：
    # “Shih-Yang Liu 1 2 Chien-Yi Wang 1 …”。先利用原始编号切分。
    for cand in re.split(r"(?:\s*[*†‡§¶#]?\s*\d+)+\s*", text):
        cand = re.sub(r"\s+", " ", cand).strip(" .*†‡§¶#0123456789")
        if NAME_RE.match(cand) and len(cand) <= 40 and not ORG_RE.search(cand):
            names.append(cand)
    pieces = ORG_RE.split(clean)
    head, tail = pieces[0], pieces[1:]
    if head.strip():
        for cand in re.split(r",| and |;|&", head):
            cand = re.sub(r"\s+", " ", cand).strip(" .0123456789")
            if NAME_RE.match(cand) and len(cand) <= 40 and cand not in names and not ORG_RE.search(cand):
                names.append(cand)
        # 中文期刊作者常为“罗文,王厚峰”或“张三、李四”。
        for cand in re.split(r"[,，、;；]", head):
            cand = re.sub(r"[\d*†‡§¶#]+", "", cand).strip()
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cand):
                names.append(cand)
    if tail:
        orgs.append(ORG_RE.sub(lambda m: m.group(0), clean).strip()[:200])
    return names, orgs


def _abstract_prefix(text: str) -> re.Match | None:
    # 中文期刊常把单位右括号与“摘 要”合并在同一块，不能只认块首。
    return re.search(r"(?:^|\s)(?:abstract|摘\s*要)\s*[:：]?\s*", text or "", re.I)


def _extract_meta(
    parsed: ParsedDoc, page1_blocks: list[Block], body: float, page1_lines: list[Line] | None = None
) -> None:
    """从第 1 页区块里抽标题 / 作者 / 单位 / 摘要。"""
    if not page1_blocks:
        return
    cand = [b for b in page1_blocks if not b.text.startswith("arXiv:")
            and not RE_ARXIV.match(b.text) and len(b.text.strip()) >= 6
            and b.kind in ("text", "heading")]
    title_blk = None
    if cand:
        title_blk = max(cand, key=lambda b: (b.size, -b.bbox[1]))
        if title_blk.size >= body * 1.1 and len(title_blk.text) < 300:
            # 同字号、紧邻的首页块属于多行/多段标题；按视觉行和 x 排序，
            # 解决 ALBERT 同 y 双块逆序及 VGG 第二行丢失。
            y0 = title_blk.bbox[1]
            parts = [b for b in cand if abs(b.size - title_blk.size) <= 0.4
                     and y0 - 3 <= b.bbox[1] <= y0 + 65 and not RE_ARXIV.match(b.text)]
            parts.sort(key=lambda b: (round(b.bbox[1] / 5), b.bbox[0]))
            parsed.title = " ".join(b.text.strip() for b in parts).strip()
            if page1_lines:
                # 块合并后 x/y 会包住多行，无法表达同行左右顺序；标题优先
                # 回原始行排序，保证 ALBERT 同行两段与下一行的视觉顺序。
                tlines = [ln for ln in page1_lines if abs(ln.size - title_blk.size) <= 0.4
                          and y0 - 3 <= ln.bbox[1] <= y0 + 65 and not RE_ARXIV.match(ln.text)]
                tlines.sort(key=lambda ln: (round(ln.bbox[1] / 5), ln.bbox[0]))
                if tlines:
                    parsed.title = " ".join(ln.text.strip() for ln in tlines).strip()

    abstract_blk = next((b for b in page1_blocks if _abstract_prefix(b.text)), None)
    if title_blk:
        top = title_blk.bbox[3] - 2
        bottom = abstract_blk.bbox[1] if abstract_blk else title_blk.bbox[3] + 320
        source = page1_lines if page1_lines else page1_blocks
        for b in source:
            if not (top <= b.bbox[1] <= bottom):
                continue
            nm, og = _names_from_block(b.text)
            parsed.authors.extend(nm)
            parsed.affiliations.extend(og)
        if not parsed.authors:
            # 技术报告常只有机构作者（如 Phi-3 / Microsoft），如实保留机构而非留空。
            for b in page1_blocks:
                t = b.text.strip()
                if top <= b.bbox[1] <= bottom and len(t) <= 60 and ORG_RE.fullmatch(t):
                    parsed.authors.append(t)
                    break
    parsed.authors = list(dict.fromkeys(parsed.authors))[:24]
    parsed.affiliations = list(dict.fromkeys(parsed.affiliations))[:12]

    for i, b in enumerate(page1_blocks):
        m = _abstract_prefix(b.text)
        if not m:
            continue
        buf = []
        inline = b.text[m.end():].strip()
        if inline:
            buf.append(inline)
        for nb in page1_blocks[i + 1:]:
            t = nb.text.strip()
            if nb.kind == "heading" or re.match(r"^(?:\d+\.?\s+)?introduction\b", t, re.I) \
                    or re.match(r"^(?:关键词|关键字|中图分类号)\s*[:：]", t):
                break
            if RE_ARXIV.match(t) or not t:
                continue
            buf.append(t)
            if sum(len(x) for x in buf) > 2500:
                break
        parsed.abstract = " ".join(buf).strip()
        break

    if not parsed.abstract:
        # 少数 arXiv 论文（如 RETRO）没有 Abstract 标签。只在明确的
        # Introduction 之前取最后一段长正文；作者/机构/邮箱块不参与。
        intro_idx = next((i for i, b in enumerate(page1_blocks)
                          if re.match(r"^(?:\d+\.?\s*)+introduction\b", b.text.strip(), re.I)), None)
        if intro_idx is not None:
            candidates = []
            for b in page1_blocks[:intro_idx]:
                t = b.text.strip()
                if len(t) >= 200 and b.kind == "text" and not RE_EMAIL.search(t) \
                        and not ORG_RE.search(t) and not RE_ARXIV.match(t):
                    candidates.append(t)
            if candidates:
                parsed.abstract = candidates[-1][:2500]

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
    last_numbered = False
    suppress_unnumbered = False
    seen_numeric: set[int] = set()

    for b in blocks:
        if b.kind != "reference":
            continue
        # 全角方括号统一成半角：［12］ 与 [12] 在数据库里应当是同一个东西，
        # 否则前端按编号去重、和后文引用 [12] 对不上。
        txt = b.text.strip().replace("［", "[").replace("］", "]")
        if not txt or re.fullmatch(r"[\d\s.\-–—]+", txt):
            continue
        # 中文期刊末尾的作者介绍不属于参考文献，不能粘进最后一条。
        if re.search(r"(?:主要研|通信作者|征稿启事|E-mail|（\d{4}[—–-]）)", txt, re.I):
            # 双栏阅读顺序中，左栏作者简介可能排在右栏最后的参考文献之前；
            # 只屏蔽无编号尾噪声，遇到后续合法 [n] 条目仍要恢复。
            suppress_unnumbered = True
            last_numbered = False
            continue
        if RE_REF_LABEL.match(txt):
            pending_label = txt
            continue

        m = RE_REF_ITEM.match(txt)
        numeric_idx = None
        if m:
            for g in m.groups():
                if g and g.isdigit():
                    numeric_idx = int(g)
                    break

        if suppress_unnumbered and numeric_idx is None:
            continue
        if numeric_idx is not None:
            suppress_unnumbered = False

        # 数字编号条目的续行没有自己的编号。先拼接再清洗，避免把每个
        # 跨栏/跨页碎片都当成新文献（中文信息学报实测会膨胀到 2 倍）。
        if refs and last_numbered and numeric_idx is None and not pending_label:
            prev = refs[-1]
            old = prev["text"].rstrip()
            prev["text"] = old[:-1] + txt if old.endswith("-") else old + " " + txt
            prev["bbox"] = [min(prev["bbox"][0], round(b.bbox[0], 1)),
                            min(prev["bbox"][1], round(b.bbox[1], 1)),
                            max(prev["bbox"][2], round(b.bbox[2], 1)),
                            max(prev["bbox"][3], round(b.bbox[3], 1))]
            continue

        if len(txt) < 25 and numeric_idx is None:
            continue
        # 有编号的短首行（如“[123] ZHANG T, KISHORE”）必须保留并等待续行；
        # 无编号块才需要年份/标签证据来排除页眉和尾噪声。
        if numeric_idx is None and not re.search(r"(?:19|20)\d{2}", txt) and not txt.startswith("["):
            continue
        if pending_label and not txt.startswith("["):
            txt = f"{pending_label} {txt}"
        pending_label = ""
        if numeric_idx is not None:
            if numeric_idx in seen_numeric:
                last_numbered = False
                continue
            seen_numeric.add(numeric_idx)
            idx = numeric_idx
        else:
            idx = len(refs) + 1
        refs.append({"index": idx, "text": txt, "page": b.page,
                     "bbox": [round(v, 1) for v in b.bbox], "anchor": b.anchor})
        last_numbered = numeric_idx is not None
    return refs


# ---------------- 主入口 ----------------
def _parse_pdf_impl(path: str, max_pages: int | None = None) -> ParsedDoc:
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
    mupdf_warning_pages: list[int] = []
    toc_pages: list[int] = []
    for pno, page in enumerate(doc, start=1):
        if max_pages and pno > max_pages:
            break
        parsed_pages += 1

        pymupdf.TOOLS.reset_mupdf_warnings()
        lines = _extract_lines(page, pno)
        if pymupdf.TOOLS.mupdf_warnings().strip():
            mupdf_warning_pages.append(pno)
        page_chars = sum(len(l.text) for l in lines)
        total_chars += page_chars
        if page_chars < 30:
            empty_pages += 1

        if not lines:
            continue

        # 目录页整页丢掉：书的目录本身就是一长串"编号 + 标题 + 页码"，
        # 与章节标题在字形/字号上完全一样，分类器无从区分，结果就是章节树里
        # 前半截全是目录条目（还因为两栏排版互相粘成"1.5.5 1.1 New approach"）。
        # 判据只看版面：整页满是点线（"1.1 Supervised learning . . . . 1"）
        # 或大量纯页码行——正文页两条都不会有。
        if _is_toc_page(lines):
            toc_pages.append(pno)
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
    if toc_pages:
        log.info("跳过目录页 %d 页（页码 %s）", len(toc_pages), ",".join(map(str, toc_pages[:20])))
    if mupdf_warning_pages:
        log.warning("MuPDF 内容流兼容警告：%d 页（页码 %s），已继续解析；逐条底层输出已抑制",
                    len(mupdf_warning_pages), ",".join(map(str, mupdf_warning_pages[:30])))

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
    # 目录页被整页丢弃后，树里留一个"目录"节点：读者仍然能一键跳到目录，
    # 但不会再出现"目录里的每一章都变成一个章节"。
    if toc_pages:
        parsed.sections.insert(0, {
            "level": 1, "title": "目录", "page": toc_pages[0], "order": -1,
            "bbox": [], "anchor": "",
        })

    log.info(
        "解析完成 %s：%d 页 / %d 区块 / %d 标题 / %d 参考文献 / 扫描件=%s",
        path.split("/")[-1], parsed.page_count, len(parsed.blocks),
        len(parsed.sections), len(parsed.references), parsed.is_scanned,
    )
    return parsed


def parse_pdf(path: str, max_pages: int | None = None) -> ParsedDoc:
    """公共入口：底层兼容警告按篇汇总，调用异常不吞掉。"""
    old_errors = pymupdf.TOOLS.mupdf_display_errors(False)
    old_warnings = pymupdf.TOOLS.mupdf_display_warnings(False)
    try:
        return _parse_pdf_impl(path, max_pages=max_pages)
    finally:
        pymupdf.TOOLS.mupdf_display_errors(bool(old_errors))
        pymupdf.TOOLS.mupdf_display_warnings(bool(old_warnings))


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
