"""论文架构拓扑图 → TikZ / Graphviz(DOT) / Mermaid / Matplotlib（官方 1.6.2）。

**为什么不让模型直接写代码。**

让模型直接输出 TikZ，十次里有三四次会漏 `;`、把中文塞进数学模式、或者
`\node` 坐标写重导致节点叠在一起——而这些错误在编译前看不出来，用户拿到的
是一段"看起来很对、编译就炸"的代码。

这里换了个方向：

    模型只产出**图结构**（节点 / 边 / 分组），代码由渲染层生成。

同一个结构渲染四次，四种格式必然描述同一张图；语法由渲染层保证，模型再怎么
胡说也只影响内容，不会产出编译不过的代码。

模型彻底失败时退回一份通用四层架构（标注 source=template），不假装是它生成的。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 节点类型 → 各格式的形状。新增类型必须同步四张表，否则会渲染成默认形状。
_KINDS = ("input", "module", "data", "output", "loss", "tool")
_SHAPE_DOT = {
    "input": ("note", "#eef3fb"),
    "module": ("box", "#f6f7f9"),
    "data": ("cylinder", "#eef7f0"),
    "output": ("folder", "#fbf3ee"),
    "loss": ("hexagon", "#f7eef2"),
    "tool": ("component", "#f3f0fa"),
}
_SHAPE_TIKZ = {
    "input": "io",
    "module": "box",
    "data": "store",
    "output": "out",
    "loss": "loss",
    "tool": "tool",
}

_SYS = (
    "你是学术论文的架构图设计者。为给定的研究思路设计一张**系统架构拓扑图**，"
    "只输出 JSON，不要任何解释文字。\n"
    "JSON 结构：\n"
    '{"title": "图标题(<=20字)", "nodes": ['
    '{"id": "n1", "label": "节点名(<=12字，名词短语)", "kind": "input|module|data|output|loss|tool", '
    '"group": "所属分组名(<=8字)"}], '
    '"edges": [{"from": "n1", "to": "n2", "label": "动作(<=8字，可空)"}]}\n'
    "硬性规则：\n"
    "1. 节点 6–12 个，构成一条从输入到输出的完整数据流，不要出现孤立节点；\n"
    "2. 每个节点必须有 kind 和 group；group 取 2–4 个，代表层（如 数据层/模型层/训练层/输出层）；\n"
    "3. 边只连已定义的节点 id，不写自环；同一对节点之间只连一次；\n"
    "4. label 用中文名词短语，不要写整句，不要写标点；\n"
    "5. 结构要体现论文的真实逻辑（数据怎么来、模型怎么用、损失怎么算、结果怎么出），"
    "不要堆砌通用模块。"
)

# 模型不可用时的兜底：一张最小可用的四层架构，source=template 明示不是模型产出
_TEMPLATE = {
    "title": "研究方案总体架构",
    "nodes": [
        {"id": "n1", "label": "文献与数据", "kind": "input", "group": "数据层"},
        {"id": "n2", "label": "文档解析与分块", "kind": "module", "group": "数据层"},
        {"id": "n3", "label": "向量索引", "kind": "data", "group": "数据层"},
        {"id": "n4", "label": "检索与重排", "kind": "module", "group": "模型层"},
        {"id": "n5", "label": "生成模型", "kind": "module", "group": "模型层"},
        {"id": "n6", "label": "引用校验", "kind": "tool", "group": "评估层"},
        {"id": "n7", "label": "结构化答案", "kind": "output", "group": "输出层"},
    ],
    "edges": [
        {"from": "n1", "to": "n2", "label": "解析"},
        {"from": "n2", "to": "n3", "label": "建索引"},
        {"from": "n3", "to": "n4", "label": "召回"},
        {"from": "n4", "to": "n5", "label": "上下文"},
        {"from": "n5", "to": "n6", "label": "候选答案"},
        {"from": "n6", "to": "n7", "label": "定稿"},
    ],
}


# ------------------------------------------------------------------ 结构清洗
def _clean_label(s: Any, limit: int = 24) -> str:
    t = re.sub(r"[\r\n\t]+", " ", str(s or "")).strip()
    t = re.sub(r"\s{2,}", " ", t)
    return t[:limit]


def sanitize(obj: dict) -> dict:
    """把模型输出的结构收拾成一定能渲染的形态。

    模型常见的四种毛病都在这里兜住：id 重复、边指向不存在的节点、自环、
    节点类型写了白名单外的词。渲染层因此可以假设输入是干净的。
    """
    raw_nodes = obj.get("nodes") if isinstance(obj.get("nodes"), list) else []
    raw_edges = obj.get("edges") if isinstance(obj.get("edges"), list) else []

    nodes: list[dict] = []
    seen_label: set[str] = set()
    id_map: dict[str, str] = {}
    for n in raw_nodes[:14]:
        if not isinstance(n, dict):
            continue
        label = _clean_label(n.get("label"))
        if not label or label in seen_label:
            continue
        seen_label.add(label)
        kind = str(n.get("kind") or "module").strip().lower()
        node = {
            "id": f"n{len(nodes) + 1}",          # 重新编号：不信模型给的 id
            "label": label,
            "kind": kind if kind in _KINDS else "module",
            "group": _clean_label(n.get("group") or "主体", 16) or "主体",
        }
        nodes.append(node)
        # 旧 id → 新 id：模型给的 id 可能重复或乱序，映射在入列时建才不会错位
        old = str(n.get("id") or "").strip()
        if old and old not in id_map:
            id_map[old] = node["id"]
    if len(nodes) < 2:
        return {}

    # 分组按出现顺序排，最多 4 层——再多图就画不下了
    groups: list[str] = []
    for n in nodes:
        if n["group"] not in groups:
            groups.append(n["group"])
    if len(groups) > 4:
        keep = set(groups[:4])
        for n in nodes:
            if n["group"] not in keep:
                n["group"] = groups[3]           # 归并到最后一层，不丢节点
        groups = groups[:4]

    # 模型给的 id 可能重复或乱序，id_map 已在建节点时按"旧 id → 新 id"记好
    edges: list[dict] = []
    seen_edge: set[tuple[str, str]] = set()
    dropped = 0
    valid = {n["id"] for n in nodes}
    for e in raw_edges[:40]:
        if not isinstance(e, dict):
            continue
        src = id_map.get(str(e.get("from") or ""), "")
        dst = id_map.get(str(e.get("to") or ""), "")
        if not src or not dst or src == dst or src not in valid or dst not in valid:
            dropped += 1
            continue
        if (src, dst) in seen_edge:
            dropped += 1
            continue
        seen_edge.add((src, dst))
        edges.append({"from": src, "to": dst, "label": _clean_label(e.get("label"), 16)})
    if not edges:
        return {}

    return {
        "title": _clean_label(obj.get("title") or "论文架构图", 40),
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "dropped_edges": dropped,
    }


def _ask_llm(idea: str, outline: str = "") -> dict | None:
    try:
        from app.core.config import settings
        from app.core.llm import gateway
    except Exception:  # noqa: BLE001 纯工具环境（单测）没有 LLM 依赖
        return None

    ctx = f"研究思路：{idea.strip()}"
    if outline.strip():
        ctx += f"\n\n已有论文大纲（请让架构与之对应）：\n{outline.strip()[:2500]}"
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": ctx + "\n\n请输出 JSON。"},
            ],
            temperature=0.4,
            max_tokens=settings.write_max_tokens,
            model=settings.write_model,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("架构图生成失败，退回模板：%s", exc)
        return None
    return obj or None


# ------------------------------------------------------------------ 布局
def _layout(g: dict) -> dict[str, tuple[float, float]]:
    """按分组分层：同组一行，组内水平均分。四种渲染共用这一套坐标。"""
    by_group: dict[str, list[dict]] = {name: [] for name in g["groups"]}
    for n in g["nodes"]:
        by_group.setdefault(n["group"], []).append(n)
    pos: dict[str, tuple[float, float]] = {}
    dx, dy = 3.4, 2.2
    for r, name in enumerate(g["groups"]):
        row = by_group.get(name, [])
        span = (len(row) - 1) * dx / 2
        for i, n in enumerate(row):
            pos[n["id"]] = (i * dx - span, -r * dy)
    return pos


# ------------------------------------------------------------------ 渲染：Mermaid
_MERMAID_SHAPE = {
    "input": ('(["{l}"])', "体育场"),
    "module": ('["{l}"]', "矩形"),
    "data": ('[("{l}")]', "圆柱"),
    "output": ('[/"{l}"/]', "平行四边形"),
    "loss": ('{{"{l}"}}', "六边形"),
    "tool": ('[["{l}"]]', "子程序"),
}


def render_mermaid(g: dict) -> str:
    # 用 LR 而不是 TB：这类图本质是流水线（输入→编码→检索→输出），边把节点串成一条链，
    # TB 下这条链就是一根竖柱，底部节点要滚动才能看到；LR 把链横过来，一屏能看完整。
    lines = ["flowchart LR"]
    for gi, name in enumerate(g["groups"]):
        lines.append(f'  subgraph G{gi}["{name}"]')
        for n in g["nodes"]:
            if n["group"] != name:
                continue
            tpl = _MERMAID_SHAPE.get(n["kind"], _MERMAID_SHAPE["module"])[0]
            lines.append(f"    {n['id']}{tpl.format(l=n['label'])}")
        lines.append("  end")
    # 试过在每个分组内部加一条不可见连线（A ~~~ B）想把同层节点锁成一行，
    # 但 dagre 仍然会给它分配 rank，结果整张图被压成一根更细的竖柱。
    # Mermaid 这一侧不做布局干预：它只负责"能看"，精确排版交给 TikZ / DOT。
    for e in g["edges"]:
        label = f"|{e['label']}|" if e["label"] else ""
        lines.append(f"  {e['from']} -->{label} {e['to']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 渲染：Graphviz DOT
def render_dot(g: dict) -> str:
    lines = [
        f'// {g["title"]}',
        "digraph architecture {",
        "  rankdir=TB;",
        '  graph [fontname="Helvetica", nodesep=0.30, ranksep=0.55, splines=true];',
        '  node [shape=box, style="rounded,filled", fillcolor="#f6f7f9", '
        'color="#5b6472", fontname="Helvetica", fontsize=11, penwidth=0.9, margin="0.14,0.07"];',
        '  edge [fontname="Helvetica", fontsize=9, color="#5b6472", arrowsize=0.7];',
    ]
    for gi, name in enumerate(g["groups"]):
        lines.append(f'  subgraph cluster_{gi} {{')
        lines.append(f'    label="{name}"; labeljust="l"; fontsize=10; fontcolor="#6b7280";')
        lines.append('    style="rounded,dashed"; color="#c8ccd4";')
        ids = " ".join(n["id"] for n in g["nodes"] if n["group"] == name)
        lines.append(f"    {ids};")
        lines.append("  }")
    for n in g["nodes"]:
        shape, fill = _SHAPE_DOT.get(n["kind"], _SHAPE_DOT["module"])
        lines.append(f'  {n["id"]} [label="{n["label"]}", shape={shape}, fillcolor="{fill}"];')
    for e in g["edges"]:
        if e["label"]:
            lines.append(f'  {e["from"]} -> {e["to"]} [label={e["label"]}];')
        else:
            lines.append(f'  {e["from"]} -> {e["to"]};')
    lines.append("}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 渲染：TikZ
_TIKZ_STYLE = {
    "input": "input",
    "module": "mod",
    "data": "store",
    "output": "out",
    "loss": "loss",
    "tool": "tool",
}


def _tex(s: str) -> str:
    out = str(s)
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                 ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                 ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")):
        out = out.replace(a, b)
    return out


def render_tikz(g: dict) -> str:
    pos = _layout(g)
    lines = [
        "% 需要 \\usepackage{tikz}；中文标签请用 ctex 或 XeLaTeX 编译",
        "\\begin{figure}[htbp]",
        "  \\centering",
        "  \\begin{tikzpicture}[",
        "    font=\\small, >=stealth, thick,",
        "    box/.style={draw=black!45, rounded corners=3pt, fill=gray!6, "
        "minimum width=2.9cm, minimum height=0.95cm, align=center, inner sep=3pt},",
        "    input/.style={box, rounded corners=10pt, fill=blue!5, draw=blue!40},",
        "    store/.style={box, fill=green!5, draw=green!35!black},",
        "    out/.style={box, fill=orange!6, draw=orange!45!black},",
        "    loss/.style={box, fill=red!5, draw=red!35},",
        "    tool/.style={box, fill=violet!5, draw=violet!35},",
        "    mod/.style={box},",
        "    flow/.style={->, draw=black!55, shorten >=1pt},",
        "  ]",
    ]
    for n in g["nodes"]:
        x, y = pos[n["id"]]
        style = _TIKZ_STYLE.get(n["kind"], "mod")
        lines.append(f'    \\node[{style}] ({n["id"]}) at ({x:.2f},{y:.2f}) {{{_tex(n["label"])}}};')
    for e in g["edges"]:
        lab = f" node[midway, font=\\scriptsize, fill=white, inner sep=1pt]{{{_tex(e['label'])}}}" \
            if e["label"] else ""
        lines.append(f'    \\draw[flow] ({e["from"]}) --{lab} ({e["to"]});')
    lines += [
        "  \\end{tikzpicture}",
        f"  \\caption{{{_tex(g['title'])}}}",
        "\\end{figure}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 渲染：Matplotlib
def render_matplotlib(g: dict) -> str:
    pos = _layout(g)
    nodes = [
        {"id": n["id"], "label": n["label"], "kind": n["kind"],
         "x": round(pos[n["id"]][0], 2), "y": round(pos[n["id"]][1], 2)}
        for n in g["nodes"]
    ]
    edges = [{"f": e["from"], "t": e["to"], "label": e["label"]} for e in g["edges"]]
    head = f'''"""由 SciAgent 生成的架构图脚本（官方 1.6.2）。
运行：python architecture.py  →  输出 architecture.pdf / .png
字体：拉丁字符优先 Times New Roman（学术惯例），中文自动回退到系统里的黑体类字体。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import textwrap

# 西文用 Times、中文回退到可用中文字体：混排图里两者都得对，缺一个就出方框
_CJK = ["SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC",
        "WenQuanYi Zen Hei", "Heiti SC", "SimSun"]
_have = {{f.name for f in fm.fontManager.ttflist}}
plt.rcParams.update({{
    "font.family": ["Times New Roman", "DejaVu Serif"] + [n for n in _CJK if n in _have],
    "font.size": 9,
    "figure.dpi": 300,
    "axes.unicode_minus": False,   # 负号用 ASCII，避免中文字体缺 U+2212
}})

NODES = {json.dumps(nodes, ensure_ascii=False, indent=4)}
EDGES = {json.dumps(edges, ensure_ascii=False, indent=4)}

COLORS = {{
    "input": ("#eef3fb", "#4a6fa5"),
    "module": ("#f6f7f9", "#5b6472"),
    "data": ("#eef7f0", "#3f7d52"),
    "output": ("#fbf3ee", "#a5703f"),
    "loss": ("#f7eef2", "#a04a63"),
    "tool": ("#f3f0fa", "#6b5aa5"),
}}

# 画布按坐标范围算，不写死：层里节点多的时候固定宽度会把框挤成一排
_x0 = min(n["x"] for n in NODES) - 2.0
_x1 = max(n["x"] for n in NODES) + 2.0
_y0 = min(n["y"] for n in NODES) - 1.1
_y1 = max(n["y"] for n in NODES) + 1.1
fig, ax = plt.subplots(figsize=(min(9.5, (_x1 - _x0) * 0.62), max(2.4, (_y1 - _y0) * 0.62)))
ax.set_axis_off()
ax.set_xlim(_x0, _x1)
ax.set_ylim(_y0, _y1)

W, H = 2.5, 0.8
box = {{}}
for n in NODES:
    fill, edge = COLORS.get(n["kind"], COLORS["module"])
    ax.add_patch(FancyBboxPatch(
        (n["x"] - W / 2, n["y"] - H / 2), W, H,
        boxstyle="round,pad=0.04,rounding_size=0.10",
        linewidth=0.9, facecolor=fill, edgecolor=edge, zorder=2))
    ax.text(n["x"], n["y"], textwrap.fill(n["label"], 12), ha="center", va="center",
            fontsize=8.5, color="#1f2430", zorder=4)
    box[n["id"]] = (n["x"], n["y"])

for e in EDGES:
    x1, y1 = box[e["f"]]
    x2, y2 = box[e["t"]]
    if abs(y2 - y1) < 1e-6:                    # 同层：横向连
        sx = x1 + (W / 2 if x2 > x1 else -W / 2)
        tx = x2 - (W / 2 if x2 > x1 else -W / 2)
        sy = ty = y1
        lx, ly = (sx + tx) / 2, y1 + H / 2 + 0.16   # 标签抬到框外，否则会贴着框边看不清
    elif y2 < y1:                              # 下一层：从底边出、顶边入
        sx, sy, tx, ty = x1, y1 - H / 2, x2, y2 + H / 2
        lx, ly = (sx + tx) / 2, (sy + ty) / 2
    else:                                      # 回边：反过来
        sx, sy, tx, ty = x1, y1 + H / 2, x2, y2 - H / 2
        lx, ly = (sx + tx) / 2, (sy + ty) / 2
    # 箭头画在方块之上：zorder 低于方块时，箭头头部会被方块盖住，看起来像没有箭头
    ax.add_patch(FancyArrowPatch(
        (sx, sy), (tx, ty), arrowstyle="-|>", mutation_scale=9,
        linewidth=0.9, color="#5b6472", shrinkA=0, shrinkB=0, zorder=3))
    if e["label"]:
        ax.text(lx, ly, e["label"], ha="center", va="center",
                fontsize=6.5, color="#4b5563", zorder=5,
                bbox=dict(boxstyle="round,pad=0.10", fc="white", ec="none", alpha=0.92))

ax.set_title({json.dumps(g["title"], ensure_ascii=False)}, fontsize=11)
fig.tight_layout()
fig.savefig("architecture.pdf", bbox_inches="tight")
fig.savefig("architecture.png", dpi=300, bbox_inches="tight")
print("saved architecture.pdf / .png")
'''
    return head


# ------------------------------------------------------------------ 统一入口
def generate(idea: str, outline: str = "", title: str = "") -> dict:
    idea = (idea or "").strip()
    if len(idea) < 2:
        return {"ok": False, "error": "请先写一句你的研究想法"}

    obj = _ask_llm(idea, outline)
    source = "llm"
    clean = sanitize(obj) if obj else {}
    if not clean:
        clean = sanitize(_TEMPLATE)
        source = "template"
    if not clean:
        return {"ok": False, "error": "架构图生成失败，请重试"}

    if title.strip():
        clean["title"] = _clean_label(title, 40)

    formats = {
        "mermaid": {"label": "Mermaid", "lang": "mermaid", "code": render_mermaid(clean)},
        "dot": {"label": "Graphviz DOT", "lang": "dot", "code": render_dot(clean)},
        "tikz": {"label": "TikZ", "lang": "latex", "code": render_tikz(clean)},
        "matplotlib": {"label": "Matplotlib", "lang": "python",
                       "code": render_matplotlib(clean)},
    }
    return {
        "ok": True,
        "title": clean["title"],
        "source": source,
        "nodes": clean["nodes"],
        "edges": clean["edges"],
        "groups": clean["groups"],
        "formats": formats,
        "checks": {
            "node_count": len(clean["nodes"]),
            "edge_count": len(clean["edges"]),
            "group_count": len(clean["groups"]),
            "dropped_edges": clean.get("dropped_edges", 0),
            "formats": list(formats),
        },
    }
