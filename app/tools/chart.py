"""CSV 实验表格 → Mermaid / TikZ(pgfplots) 学术图表代码。

为什么需要：论文里的表格要贴进综述或 LaTeX 时，手工重敲一遍数值就是
"抄错一个小数点"的经典事故。这里做的是**无损数值搬运**：
解析出的每个数值原样出现在生成代码里，可逐格核对。
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

_NUM = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*%?\s*$")


def parse_csv(raw: str) -> dict:
    rows = list(csv.reader(io.StringIO(raw.strip())))
    if len(rows) < 2:
        raise ValueError("CSV 至少需要表头 + 1 行数据")
    header = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    labels = [r[0].strip() for r in body]
    series: dict[str, list[tuple[str, str]]] = {}
    for j, name in enumerate(header[1:], start=1):
        series[name or f"series{j}"] = [
            (labels[i], (r[j].strip() if j < len(r) else "")) for i, r in enumerate(body)
        ]
    # 保留原始数值串（含 65M、28.4 这类带单位的写法）作为"字面值"
    numeric = {}
    for name, pairs in series.items():
        numeric[name] = [v for _, v in pairs]
    return {"header": header, "labels": labels, "series": series, "numeric": numeric}


def _num(v: str) -> float:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", v)
    return float(m.group()) if m else 0.0


def to_mermaid(raw: str, title: str = "实验结果") -> dict:
    p = parse_csv(raw)
    if not p["series"]:
        raise ValueError("CSV 没有数值列")
    allvals = [_num(v) for vals in p["numeric"].values() for v in vals if _num(v)]
    ymax = max(allvals + [1.0]) * 1.2
    # xychart-beta 的 x 轴标签必须是 JSON 字符串数组
    import json
    lines = [
        "xychart-beta",
        f'    title "{title}"',
        "    x-axis " + json.dumps(p["labels"], ensure_ascii=False),
        f'    y-axis "value" 0 --> {ymax:.4g}',
    ]
    for name, vals in p["numeric"].items():
        clean = [f"{_num(v):.6g}" for v in vals]
        lines.append(f"    bar [{', '.join(clean)}]  %% {name}")
    # 原始字面量（含 65M/28.4 原文）放在注释里，保证无损可核对
    lines.append("    %% raw values: " + json.dumps(p["numeric"], ensure_ascii=False))
    return {"code": "\n".join(lines), "format": "mermaid", "parsed": p}


def to_tikz(raw: str, title: str = "实验结果") -> dict:
    p = parse_csv(raw)
    import json
    lines = [
        "\\begin{figure}[htbp]",
        "  \\centering",
        "  \\begin{tikzpicture}",
        "    \\begin{axis}[",
        "      ybar, enlargelimits=0.15, legend style={at={(0.5,-0.18)},anchor=north,legend columns=-1},",
        f"      ylabel={{value}}, symbolic xCoords={{{','.join(p['labels'])}}},",
        "      xtick=data, nodes near coords, grid=major,",
        f"      title={{{title}}},",
        "    ]",
    ]
    for name in p["numeric"]:
        vals = ", ".join(f"({json.dumps(lbl, ensure_ascii=False)},{_num(v):.6g})"
                         for lbl, v in p["series"][name])
        lines.append(f"      \\addplot coordinates {{{vals}}};  % {name}")
        lines.append(f"      \\addlegendentry{{{name}}}")
    lines += [
        "    \\end{axis}",
        "  \\end{tikzpicture}",
        f"  % raw values: {json.dumps(p['numeric'], ensure_ascii=False)}",
        "\\end{figure}",
    ]
    return {"code": "\n".join(lines), "format": "tikz", "parsed": p}


def convert(raw: str, fmt: str = "mermaid", title: str = "实验结果") -> dict:
    fmt = (fmt or "mermaid").lower()
    if fmt not in {"mermaid", "tikz", "pgfplots"}:
        raise ValueError(f"不支持的格式 {fmt}，可用 mermaid / tikz")
    out = to_tikz(raw, title) if fmt in {"tikz", "pgfplots"} else to_mermaid(raw, title)
    p = out.pop("parsed")
    out["rows"] = len(p["labels"])
    out["columns"] = list(p["series"].keys())
    out["raw_values"] = p["numeric"]
    return out
