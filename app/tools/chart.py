"""CSV 实验表格 → 学术图表代码 + Figure Caption（官方 1.6.3）。

为什么需要：论文里的表格要贴进综述或 LaTeX 时，手工重敲一遍数值就是
"抄错一个小数点"的经典事故。这里做的是**无损数值搬运**：
解析出的每个数值原样出现在生成代码里，可逐格核对。

产出的是**代码**而不是图片。投稿场景下作者要的是能塞进自己模板的
TikZ / Matplotlib 源文件，PNG 反而改不动：

    kind      bar / line / radar / table
    format    mermaid / tikz / matplotlib / latex / markdown

Figure Caption 分两层：
  · 规则层（caption_rule）——纯统计，不联网也一定有结果；
  · 模型层（caption_llm）——把数值摘要交给小模型写成学术图注，并提炼趋势。
模型只看到统计摘要，写完还要把图注里的数字逐个回查表格（check_numbers），
查不到的数字会被列出来——图注可以写得漂亮，但编不出表格里没有的数。
"""
from __future__ import annotations

import csv
import io
import json

import re
from typing import Any

_NUM = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*%?\s*$")
# 图注校验时从文字里捞数字
_RE_ANY_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")

KINDS = ("bar", "line", "radar", "table")
_KIND_ALIAS = {
    "bar": "bar", "column": "bar", "柱": "bar", "柱状": "bar", "柱状图": "bar",
    "line": "line", "折线": "line", "折线图": "line", "curve": "line",
    "radar": "radar", "雷达": "radar", "雷达图": "radar", "spider": "radar",
    "table": "table", "表格": "table", "三线表": "table",
}
FORMATS = {
    "bar": ("mermaid", "tikz", "matplotlib"),
    "line": ("mermaid", "tikz", "matplotlib"),
    "radar": ("tikz", "matplotlib"),
    "table": ("latex", "markdown"),
}
_FMT_ALIAS = {"pgfplots": "tikz", "tex": "latex", "md": "markdown", "py": "matplotlib"}


# ------------------------------------------------------------------ 解析
def parse_csv(raw: str) -> dict:
    rows = list(csv.reader(io.StringIO((raw or "").strip())))
    if len(rows) < 2:
        raise ValueError("CSV 至少需要表头 + 1 行数据")
    header = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    if not body:
        raise ValueError("CSV 没有数据行")
    labels = [(r[0].strip() if r else "") or f"row{i + 1}" for i, r in enumerate(body)]
    series: dict[str, list[tuple[str, str]]] = {}
    for j, name in enumerate(header[1:], start=1):
        series[name or f"series{j}"] = [
            (labels[i], (r[j].strip() if j < len(r) else "")) for i, r in enumerate(body)
        ]
    # 保留原始数值串（含 65M、28.4 这类带单位的写法）作为"字面值"
    numeric = {name: [v for _, v in pairs] for name, pairs in series.items()}
    return {"header": header, "labels": labels, "series": series, "numeric": numeric}


def _num(v: str) -> float:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", v or "")
    return float(m.group()) if m else 0.0


def _g(v: str) -> str:
    """数值串 → 代码里的字面量。

    纯数值原样返回：表格里写的是 41.0，生成的代码里也必须还是 41.0。
    这是"逐格可核对"的前提——写成 41 虽然数值相等，核对时却对不上原文。
    """
    s = (v or "").strip()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
        return s
    return f"{_num(s):.6g}"


def _tex_esc(s: str) -> str:
    """LaTeX 特殊字符转义。不转义就会在 & 处整篇编译失败。"""
    out = str(s)
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                 ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
                 ("^", r"\textasciicircum{}")):
        out = out.replace(a, b)
    return out


def _pgf_label(s: str) -> str:
    """pgfplots 的 symbolic 坐标：含逗号必须用花括号包住，否则会被当作两项。"""
    t = _tex_esc(s)
    return "{" + t + "}" if "," in t else t


def _md_esc(s: str) -> str:
    return str(s).replace("|", r"\|").replace("\n", " ")


# ------------------------------------------------------------------ 图 1：Mermaid
def to_mermaid(raw: str, title: str = "实验结果", kind: str = "bar") -> dict:
    p = parse_csv(raw)
    if not p["series"]:
        raise ValueError("CSV 没有数值列")
    allvals = [_num(v) for vals in p["numeric"].values() for v in vals]
    ymax = max(allvals + [1.0]) * 1.15
    ymin = min(allvals + [0.0])
    lo = min(0.0, ymin)
    lines = [
        "xychart-beta",
        f'    title "{title}"',
        # xychart-beta 的 x 轴标签必须是 JSON 字符串数组
        "    x-axis " + json.dumps(p["labels"], ensure_ascii=False),
        f'    y-axis "value" {lo:.4g} --> {ymax:.4g}',
    ]
    for name, vals in p["numeric"].items():
        clean = [_g(v) for v in vals]
        lines.append(f"    {kind} [{', '.join(clean)}]  %% {name}")
    # 原始字面量（含 65M/28.4 原文）放在注释里，保证无损可核对
    lines.append("    %% raw values: " + json.dumps(p["numeric"], ensure_ascii=False))
    return {"code": "\n".join(lines), "format": "mermaid", "kind": kind, "parsed": p}


# ------------------------------------------------------------------ 图 2：TikZ
def to_tikz(raw: str, title: str = "实验结果", kind: str = "bar") -> dict:
    p = parse_csv(raw)
    if kind == "radar":
        return _tikz_radar(p, title)

    is_bar = kind == "bar"
    lines = [
        "% 需要 \\usepackage{pgfplots} 与 \\pgfplotsset{compat=1.18}",
        "\\begin{figure}[htbp]",
        "  \\centering",
        "  \\begin{tikzpicture}",
        "    \\begin{axis}[",
        "      width=0.9\\linewidth, height=5.2cm,",
        ("      ybar, bar width=10pt, nodes near coords, nodes near coords style={font=\\tiny},"
         if is_bar else "      grid=both, mark=*, mark size=1.6pt, line width=0.9pt,"),
        "      enlargelimits=0.12, ymin=0,",
        "      legend style={at={(0.5,-0.16)},anchor=north,legend columns=-1,draw=none,font=\\small},",
        "      ylabel={value}, xlabel={}, tick label style={font=\\small},",
        f"      symbolic xCoords={{{','.join(_pgf_label(lbl) for lbl in p['labels'])}}},",
        "      xtick=data, x tick label style={rotate=0,font=\\small}, grid=major,",
        f"      title={{{_tex_esc(title)}}},",
        "    ]",
    ]
    for name in p["numeric"]:
        vals = ", ".join(f"({_pgf_label(lbl)},{_g(v)})" for lbl, v in p["series"][name])
        lines.append(f"      \\addplot coordinates {{{vals}}};  % {_tex_esc(name)}")
        lines.append(f"      \\addlegendentry{{{_tex_esc(name)}}}")
    lines += [
        "    \\end{axis}",
        "  \\end{tikzpicture}",
        f"  % raw values: {json.dumps(p['numeric'], ensure_ascii=False)}",
        "  \\caption{待补图注}",
        "\\end{figure}",
    ]
    return {"code": "\n".join(lines), "format": "tikz", "kind": kind, "parsed": p}


def _tikz_radar(p: dict, title: str) -> dict:
    """极坐标雷达：pgfplots 的 polaraxis，一圈即全部指标轴。"""
    n = len(p["labels"])
    if n < 3:
        raise ValueError("雷达图至少需要 3 个指标（3 行数据），当前只有 %d 行" % n)
    step = 360.0 / n
    angles = [i * step for i in range(n)]
    vmax = max([_num(v) for vals in p["numeric"].values() for v in vals] + [1.0]) * 1.1
    ticks = ", ".join(f"{a:.4g}" for a in angles)
    ticklabels = ", ".join(_tex_esc(l) for l in p["labels"])
    lines = [
        "% 需要 \\usepackage{pgfplots} 与 \\pgfplotsset{compat=1.18}",
        "\\begin{figure}[htbp]",
        "  \\centering",
        "  \\begin{tikzpicture}",
        "    \\begin{polaraxis}[",
        "      width=6.6cm, height=6.6cm,",
        f"      xtick={{{ticks}}}, xticklabels={{{ticklabels}}},",
        f"      ymin=0, ymax={vmax:.4g}, yticklabel style={{font=\\tiny}},",
        "      grid=both, tick label style={font=\\small},",
        "      legend style={at={(1.18,0.5)},anchor=west,draw=none,font=\\small},",
        f"      title={{{_tex_esc(title)}}},",
        "    ]",
    ]
    for name in p["numeric"]:
        coords = " ".join(f"({a:.4g},{_g(v)})" for a, v in
                          zip(angles, p["numeric"][name]))
        lines.append(f"      \\addplot+[mark=*, thick, fill opacity=0.12] coordinates {{{coords}}};")
        lines.append(f"      \\addlegendentry{{{_tex_esc(name)}}}")
    lines += [
        "    \\end{polaraxis}",
        "  \\end{tikzpicture}",
        f"  % raw values: {json.dumps(p['numeric'], ensure_ascii=False)}",
        "  \\caption{待补图注}",
        "\\end{figure}",
    ]
    return {"code": "\n".join(lines), "format": "tikz", "kind": "radar", "parsed": p}


# ------------------------------------------------------------------ 图 3：Matplotlib
_MPL_HEAD = '''"""由 SciAgent 从 CSV 生成 —— 每个数值与源表逐格一致，可直接改样式。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

# 西文 Times + 中文回退：投稿模板一般要求 Times 系，中文标题也不能变方框
_CJK = ["SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC",
        "WenQuanYi Zen Hei", "Heiti SC", "SimSun"]
_have = {f.name for f in fm.fontManager.ttflist}
plt.rcParams.update({
    "font.family": ["Times New Roman", "DejaVu Serif"] + [n for n in _CJK if n in _have],
    "font.size": 9,
    "axes.linewidth": 0.8,
    "axes.unicode_minus": False,
    "figure.dpi": 300,
})

labels = %(labels)s
series = %(series)s
'''


def _series_repr(p: dict) -> tuple[str, str]:
    labels = "[" + ", ".join(json.dumps(l, ensure_ascii=False) for l in p["labels"]) + "]"
    items = ", ".join(
        f"{json.dumps(name, ensure_ascii=False)}: "
        f"[{', '.join(_g(v) for v in p['numeric'][name])}]"
        for name in p["numeric"]
    )
    return labels, "{" + items + "}"


def to_matplotlib(raw: str, title: str = "实验结果", kind: str = "bar") -> dict:
    p = parse_csv(raw)
    labels, series = _series_repr(p)
    head = _MPL_HEAD % {"labels": labels, "series": series}
    t = title.replace('"', "'")

    if kind == "radar":
        n = len(p["labels"])
        if n < 3:
            raise ValueError("雷达图至少需要 3 个指标（3 行数据），当前只有 %d 行" % n)
        body = f'''
# 雷达图：每条轴一个指标，多个序列叠在同一极坐标里比较。
# 注意：雷达图要求各序列**同量纲**；若某列是参数量、某列是 BLEU，请先归一化再画，
# 否则小量纲的序列会被压成一个三角形（这里不擅自改你的数据）。
N = len(labels)
angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
closed = angles + angles[:1]
vmax = max(max(v) for v in series.values()) * 1.15

fig = plt.figure(figsize=(3.6, 3.4))
ax = fig.add_subplot(111, polar=True)
for name, vals in series.items():
    v = list(vals) + [vals[0]]
    ax.plot(closed, v, linewidth=1.2, label=name)
    ax.fill(closed, v, alpha=0.10)
ax.set_xticks(angles)
ax.set_xticklabels(labels)
ax.set_ylim(0, vmax)
# 径向刻度只留 3 档并挪到两轴之间：默认的 6 档会沿轴挤成一串重叠的数字
_ticks = [vmax * k for k in (0.33, 0.66, 0.99)]
ax.set_yticks(_ticks)
ax.set_yticklabels([f"{{t:.3g}}" for t in _ticks], fontsize=6)
ax.set_rlabel_position(20)
ax.set_title("{t}")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=2,
          frameon=False, fontsize=8)
fig.tight_layout()
fig.savefig("figure_radar.pdf", bbox_inches="tight")
fig.savefig("figure_radar.png", dpi=300, bbox_inches="tight")
print("saved figure_radar.pdf / .png")
'''
        return {"code": head + body, "format": "matplotlib", "kind": "radar", "parsed": p}

    if kind == "bar":
        body = f'''
x = np.arange(len(labels))
w = 0.8 / len(series)
fig, ax = plt.subplots(figsize=(3.5, 2.5))
for i, (name, vals) in enumerate(series.items()):
    bars = ax.bar(x + i * w - 0.4 + w / 2, vals, w * 0.92, label=name)
    ax.bar_label(bars, fmt="%.4g", fontsize=6, padding=1.5)   # 柱顶标数值，读者不用估读
ax.set_xticks(x)
ax.set_xticklabels(labels)
ax.set_ylabel("value")
ax.set_title("{t}")
ax.legend(frameon=False, fontsize=8)
ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.6)
ax.set_axisbelow(True)
fig.tight_layout()
fig.savefig("figure_bar.pdf", bbox_inches="tight")
fig.savefig("figure_bar.png", dpi=300, bbox_inches="tight")
print("saved figure_bar.pdf / .png")
'''
    else:
        body = f'''
x = np.arange(len(labels))
fig, ax = plt.subplots(figsize=(3.5, 2.5))
for name, vals in series.items():
    ax.plot(x, vals, marker="o", markersize=3, linewidth=1.2, label=name)
ax.set_xticks(x)
ax.set_xticklabels(labels)
ax.set_ylabel("value")
ax.set_title("{t}")
ax.legend(frameon=False, fontsize=8)
ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
fig.tight_layout()
fig.savefig("figure_line.pdf", bbox_inches="tight")
fig.savefig("figure_line.png", dpi=300, bbox_inches="tight")
print("saved figure_line.pdf / .png")
'''
    return {"code": head + body, "format": "matplotlib", "kind": kind, "parsed": p}


# ------------------------------------------------------------------ 表：LaTeX / Markdown
def to_latex_table(raw: str, title: str = "实验结果", kind: str = "table") -> dict:
    p = parse_csv(raw)
    align = "l" + "r" * (len(p["header"]) - 1)
    colspec = " ".join(align)
    lines = [
        "% 需要 \\usepackage{booktabs}；三线表是学术期刊最常用的表格形式",
        "\\begin{table}[htbp]",
        "  \\centering",
        f"  \\caption{{{'待补表注'}}}",
        f"  \\label{{tab:{re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-').lower() or 'result'}}}",
        f"  \\begin{{tabular}}{{{colspec}}}",
        "    \\toprule",
        "    " + " & ".join(_tex_esc(h) for h in p["header"]) + r" \\",
        "    \\midrule",
    ]
    for i, lbl in enumerate(p["labels"]):
        cells = [lbl] + [p["numeric"][name][i] for name in p["numeric"]]
        lines.append("    " + " & ".join(_tex_esc(c) for c in cells) + r" \\")
    lines += [
        "    \\bottomrule",
        "  \\end{tabular}",
        f"  % raw values: {json.dumps(p['numeric'], ensure_ascii=False)}",
        "\\end{table}",
    ]
    return {"code": "\n".join(lines), "format": "latex", "kind": "table", "parsed": p}


def to_markdown_table(raw: str, title: str = "实验结果", kind: str = "table") -> dict:
    p = parse_csv(raw)
    head = "| " + " | ".join(_md_esc(h) for h in p["header"]) + " |"
    sep = "|" + "|".join(["---"] + ["---:"] * (len(p["header"]) - 1)) + "|"
    rows = []
    for i, lbl in enumerate(p["labels"]):
        cells = [_md_esc(lbl)] + [_md_esc(p["numeric"][name][i]) for name in p["numeric"]]
        rows.append("| " + " | ".join(cells) + " |")
    # 表头行 + 分隔行 + 数据行，Markdown 表格要求三者齐全
    code = "\n".join([f"**{title}**", "", head, sep] + rows)
    return {"code": code, "format": "markdown", "kind": "table", "parsed": p}


# ------------------------------------------------------------------ Figure Caption
def _stats(p: dict) -> dict:
    """把表格压成"能写进图注的事实"：每列极值、极值所在标签、变化幅度。"""
    out = {}
    for name, vals in p["numeric"].items():
        nums = [_num(v) for v in vals]
        if not nums:
            continue
        imax = max(range(len(nums)), key=lambda i: nums[i])
        imin = min(range(len(nums)), key=lambda i: nums[i])
        first, last = nums[0], nums[-1]
        delta = (last - first) / abs(first) * 100 if first else 0.0
        out[name] = {
            "max": nums[imax], "max_at": p["labels"][imax],
            "min": nums[imin], "min_at": p["labels"][imin],
            "first": first, "last": last, "delta_pct": round(delta, 1),
            "span": round(nums[imax] - nums[imin], 4),
            "raw_max": vals[imax],
        }
    return out


def _trend_words(st: dict, labels: list[str]) -> tuple[str, str]:
    """一句趋势判断（中/英）。兜底用，同时作为模型写图注时的事实约束。

    刻意**不跨序列比大小**：同一张表里 BLEU（28–41）和参数量（65–175）量纲不同，
    说"参数量比 BLEU 高 326%"在学术语境里是错的。只描述两件事——
    谁的变化幅度最大、谁在哪一列取得自己的峰值。
    """
    if not st:
        return "", ""
    key = max(st, key=lambda k: abs(st[k]["delta_pct"]))
    s = st[key]
    up = s["delta_pct"] >= 0
    zh_parts = [
        f"{key} 由 {labels[0]} 的 {s['first']:g} "
        f"{'升至' if up else '降至'} {labels[-1]} 的 {s['last']:g}"
        f"（{s['delta_pct']:+.1f}%）"
    ]
    en_parts = [
        f"{key} {'rises' if up else 'falls'} from {s['first']:g} ({labels[0]}) "
        f"to {s['last']:g} ({labels[-1]}, {s['delta_pct']:+.1f}%)"
    ]

    pk = max(st, key=lambda k: st[k]["max"])
    p = st[pk]
    if pk != key:
        zh_parts.append(f"{pk} 在 {p['max_at']} 取得峰值 {p['raw_max']}")
        en_parts.append(f"{pk} peaks at {p['raw_max']} on {p['max_at']}")
    return "；".join(zh_parts) + "。", "; ".join(en_parts) + "."


def caption_rule(raw: str, title: str = "实验结果", kind: str = "bar") -> dict:
    """规则版图注：纯统计，不依赖模型，断网也有结果。"""
    p = parse_csv(raw)
    st = _stats(p)
    kind_cn = {"bar": "柱状图", "line": "折线图", "radar": "雷达图", "table": "表格"}[kind]
    kind_en = {"bar": "Bar chart", "line": "Line chart", "radar": "Radar chart",
               "table": "Table"}[kind]
    zh_t, en_t = _trend_words(st, p["labels"])
    axes = "、".join(p["labels"][:6]) + ("等" if len(p["labels"]) > 6 else "")
    zh = (f"图 1　{title}。该{kind_cn}比较了 {len(p['numeric'])} 组结果在 {len(p['labels'])} 个"
          f"条件（{axes}）下的表现。{zh_t}")
    en = (f"Figure 1. {title}. The {kind_en.lower()} compares {len(p['numeric'])} settings "
          f"over {len(p['labels'])} conditions ({', '.join(p['labels'][:6])}"
          f"{'...' if len(p['labels']) > 6 else ''}). {en_t}")
    return {"zh": zh, "en": en, "source": "rule", "stats": st, "labels": p["labels"]}


_CAP_SYS = (
    "你是学术论文的图注撰写者。只输出 JSON，不要任何解释文字，字段为：\n"
    '{"en": "英文 Figure caption", "zh": "中文图注", "trend_en": "一句话核心趋势(英文)", '
    '"trend_zh": "一句话核心趋势(中文)"}\n'
    "硬性规则：\n"
    "1. 图注以 Figure 1. / 图 1  开头，先说明图展示什么（横轴、纵轴、比较对象），再给核心结论；\n"
    "2. 英文 55–90 词，中文 90–160 字，学术书面语，不用第一人称、不用口语词；\n"
    "3. 出现的每个数值必须与【原始数据】里的写法**完全一致**（含小数位），"
    "不得四舍五入、不得插值、不得推算未给出的数字；没有数字可写就只做定性描述；\n"
    "4. 结论要具体（谁高、高多少、随什么变化），不要写“效果显著”这类空话。"
)


def caption_llm(raw: str, title: str = "实验结果", kind: str = "bar") -> dict | None:
    """模型版图注。失败返回 None，由调用方退回规则版——这条链不能因为模型挂了就断。"""
    try:
        from app.core.config import settings
        from app.core.llm import LLMError, gateway
    except Exception:  # noqa: BLE001 纯工具环境（单测）允许没有 LLM 依赖
        return None

    base = caption_rule(raw, title, kind)
    st = base["stats"]
    # 把**完整表格**给模型，而不是只给极值统计。只给统计时模型会自己"插值"，
    # 实测曾把 BERT 的 34.5 写成 35.2——逐格摊开以后它只需要抄，不需要算。
    p = parse_csv(raw)
    header = ", ".join(["条件"] + list(p["numeric"].keys()))
    rows = "\n".join(
        ", ".join([p["labels"][i]] + [vals[i] for vals in p["numeric"].values()])
        for i in range(len(p["labels"]))
    )
    table = "\n".join(
        f"- {name}: 峰值 {v['raw_max']} @ {v['max_at']}; 谷值 {v['min']} @ {v['min_at']}; "
        f"首末变化 {v['delta_pct']}%"
        for name, v in st.items()
    )
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": _CAP_SYS},
                {
                    "role": "user",
                    "content": (
                        f"图表类型：{kind}\n标题：{title}\n"
                        f"【原始数据】逐格照抄，禁止改写任何一个数字\n{header}\n{rows}\n\n"
                        f"【统计摘要】\n{table}\n"
                        f"参考趋势判断（可改写，但数字须与上表一致）：{base['zh']}\n"
                        "请输出 JSON。"
                    ),
                },
            ],
            temperature=0.3,
            max_tokens=settings.write_max_tokens,
            model=settings.write_model,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("图注生成失败，退回规则版：%s", exc)
        return None

    zh = str(obj.get("zh") or "").strip()
    en = str(obj.get("en") or "").strip()
    if not zh and not en:
        return None
    return {
        "zh": zh or base["zh"],
        "en": en or base["en"],
        "trend_zh": str(obj.get("trend_zh") or "").strip(),
        "trend_en": str(obj.get("trend_en") or "").strip(),
        "source": "llm",
        "stats": st,
        "checks": check_numbers(zh + " " + en, raw),
    }


def check_numbers(caption: str, raw: str) -> dict:
    """图注里的数字回查原表——这是"不许编数"的机械化兑现。

    只记不改：模型写出表外数字时把它列出来，人工一眼就能看到。
    数字密集的图注本来就该逐个核对，这比让模型"保证不要编"可靠。
    """
    try:
        p = parse_csv(raw)
    except ValueError:
        return {"verified": 0, "foreign": [], "checked": 0}
    pool = {_num(v) for vals in p["numeric"].values() for v in vals}
    # 百分比与差值常是算子给的，允许出现；年份/编号同理
    foreign: list[str] = []
    checked = 0
    for m in _RE_ANY_NUM.finditer(caption or ""):
        tok = m.group()
        try:
            val = float(tok)
        except ValueError:
            continue
        checked += 1
        if any(abs(val - x) <= max(0.011, abs(x) * 0.005) for x in pool):
            continue
        head = caption[max(0, m.start() - 8):m.start()]
        tail = caption[m.end():m.end() + 2]
        # 紧邻前一个字符是字母/数字 → 这是型号编号（GPT-3、BLEU-4、ResNet-50），不是数据
        prev = caption[m.start() - 1] if m.start() > 0 else ""
        if prev.isalpha() or prev.isdigit():
            continue
        if 1900 <= val <= 2100:                      # 年份
            continue
        if "%" in tail or "%" in head:               # 百分比是算子算出来的，允许
            continue
        if any(w in head for w in ("图", "表", "第", "Figure", "Fig.", "Table")):
            continue                                  # 图号 / 表号 / 序号
        foreign.append(tok)
    return {"verified": checked - len(foreign), "foreign": foreign[:8], "checked": checked}


# ------------------------------------------------------------------ 统一入口
def convert(raw: str, fmt: str = "mermaid", title: str = "实验结果",
            kind: str = "bar", caption: bool = True) -> dict:
    kind = _KIND_ALIAS.get((kind or "bar").strip().lower(), "bar")
    fmt = _FMT_ALIAS.get((fmt or "").strip().lower(), (fmt or "").strip().lower())

    # 格式与类型不匹配时给出可用组合，而不是让用户猜
    if fmt not in {f for fs in FORMATS.values() for f in fs}:
        raise ValueError(f"不支持的格式 {fmt}，可用：mermaid / tikz / matplotlib / latex / markdown")
    if fmt not in FORMATS[kind]:
        raise ValueError(
            f"{kind} 不支持 {fmt}，可用格式：{' / '.join(FORMATS[kind])}"
        )

    if fmt == "mermaid":
        out = to_mermaid(raw, title, "line" if kind == "line" else "bar")
    elif fmt == "tikz":
        out = to_tikz(raw, title, kind)
    elif fmt == "matplotlib":
        out = to_matplotlib(raw, title, kind)
    elif fmt == "latex":
        out = to_latex_table(raw, title)
    else:
        out = to_markdown_table(raw, title)

    p = out.pop("parsed")
    out["rows"] = len(p["labels"])
    out["columns"] = list(p["series"].keys())
    out["raw_values"] = p["numeric"]
    out["labels"] = p["labels"]
    out["title"] = title
    if caption:
        cap = caption_llm(raw, title, kind) or caption_rule(raw, title, kind)
        cap.pop("stats", None)      # 统计明细只用于模型提示词，前端不需要
        cap.pop("labels", None)
        out["caption"] = cap
    return out
