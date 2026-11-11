"""把 thinking_ab_result.json 汇总成表 + 三张图，供报告直接引用。

只读结果，不重跑模型——重跑要花钱，图可以反复调。
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "docs" / "qa" / "evidence"
CHART = EV / "thinking_ab"
CJK = ["SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "SimSun"]
_have = {f.name for f in fm.fontManager.ttflist}
plt.rcParams.update({
    "font.family": ["Times New Roman", "DejaVu Serif"] + [n for n in CJK if n in _have],
    "font.size": 9,
    "axes.unicode_minus": False,
    "figure.dpi": 200,
    "axes.spines.top": False,
    "axes.spines.right": False,
})
ON, OFF = "#3f7d52", "#c0743f"      # 开思考 / 关思考
CATS = {"fact": "事实抽取", "number": "数值超参", "compare": "跨文献对比",
        "refuse": "证据不足", "struct": "结构化输出"}


def load() -> dict:
    p = EV / "thinking_ab_result.json"
    if not p.exists():
        raise SystemExit("先跑 tests/thinking_ab.py")
    return json.loads(p.read_text(encoding="utf-8"))


def agg(rows: list[dict], th: bool) -> dict:
    rs = [r for r in rows if r.get("ok") and r["thinking"] is th]
    if not rs:
        return {}
    return {
        "n": len(rs),
        "ms_mean": st.mean(r["elapsed_ms"] for r in rs),
        "ms_p50": st.median(sorted(r["elapsed_ms"] for r in rs)),
        "ms_max": max(r["elapsed_ms"] for r in rs),
        "out": st.mean(r["out_tokens"] for r in rs),
        "reason": st.mean(r["reasoning_tokens"] for r in rs),
        "chars": st.mean(r["answer_chars"] for r in rs),
        "json_ok": sum(1 for r in rs if r["json_ok"]) / len(rs),
        "bad_refs": sum(r["bad_refs"] for r in rs),
        "bad_nums": sum(r["bad_nums"] for r in rs),
    }


def refuse_rate(rows: list[dict], th: bool) -> tuple[int, int]:
    rs = [r for r in rows if r["cat"] == "refuse" and r["thinking"] is th]
    good = sum(1 for r in rs if r.get("refused") and r["bad_nums"] == 0)
    return good, len(rs)


def answer_rate(rows: list[dict], th: bool) -> tuple[int, int]:
    rs = [r for r in rows if r["cat"] in ("fact", "number", "compare") and r["thinking"] is th]
    good = sum(1 for r in rs if r["answer_chars"] >= 40 and not r["refused"])
    return good, len(rs)


def fig_latency(rows: list[dict]) -> None:
    cases = sorted({r["case"] for r in rows})
    on = {r["case"]: r["elapsed_ms"] for r in rows if r["thinking"]}
    off = {r["case"]: r["elapsed_ms"] for r in rows if not r["thinking"]}
    y = np.arange(len(cases))
    fig, ax = plt.subplots(figsize=(7.2, 8.2))
    ax.barh(y - 0.2, [on.get(c, 0) / 1000 for c in cases], 0.38, label="开思考", color=ON)
    ax.barh(y + 0.2, [off.get(c, 0) / 1000 for c in cases], 0.38, label="关思考", color=OFF)
    ax.set_yticks(y)
    ax.set_yticklabels(cases, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("单题耗时（秒）")
    ax.set_title("30 道题逐题耗时：思维链平均吃掉一半以上的时间")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    ax.grid(axis="x", linestyle="--", linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)
    fig.tight_layout()
    CHART.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHART / "01_latency.png", bbox_inches="tight")
    plt.close(fig)


def fig_tokens(rows: list[dict]) -> None:
    cats = ["fact", "number", "compare", "refuse", "struct"]
    fig, ax = plt.subplots(figsize=(7.0, 3.2))
    x = np.arange(len(cats))
    w = 0.36

    def parts(th: bool, cat: str) -> tuple[float, float]:
        rs = [r for r in rows if r["thinking"] is th and r["cat"] == cat and r.get("ok")]
        if not rs:
            return 0, 0
        return (st.mean(r["reasoning_tokens"] for r in rs),
                st.mean(r["out_tokens"] for r in rs) - st.mean(r["reasoning_tokens"] for r in rs))

    # 关思考这一列没有思维链，只画正文；把它拆成两段会在图例里多出一条永远为 0 的项
    for th, off, label, c_re, c_tx in [
        (True, -w / 2, "开思考", "#b9c0cc", ON),
        (False, w / 2, "关思考", None, OFF),
    ]:
        reas = [parts(th, c)[0] for c in cats]
        text = [max(parts(th, c)[1], 0) for c in cats]
        if c_re:
            ax.bar(x + off, reas, w, label=f"{label}·思维链", color=c_re)
            ax.bar(x + off, text, w, bottom=reas, label=f"{label}·正文", color=c_tx)
        else:
            ax.bar(x + off, text, w, label=f"{label}·正文", color=c_tx)
    ax.set_xticks(x)
    ax.set_xticklabels([CATS[c] for c in cats], fontsize=8)
    ax.set_ylabel("平均输出 token")
    ax.set_title("输出 token 构成：开思考时约三分之二花在\"想\"上，正文只占小头")
    ax.legend(frameon=False, fontsize=7, ncol=2)
    ax.grid(axis="y", linestyle="--", linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(CHART / "02_tokens.png", bbox_inches="tight")
    plt.close(fig)


def fig_quality(rows: list[dict]) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(8.0, 3.0))
    labels = ["引用越界", "数字不在证据里"]
    on = [sum(r["bad_refs"] for r in rows if r["thinking"]),
          sum(r["bad_nums"] for r in rows if r["thinking"])]
    off = [sum(r["bad_refs"] for r in rows if not r["thinking"]),
           sum(r["bad_nums"] for r in rows if not r["thinking"])]
    x = np.arange(len(labels))
    axes[0].bar(x - 0.18, on, 0.36, label="开思考", color=ON)
    axes[0].bar(x + 0.18, off, 0.36, label="关思考", color=OFF)
    for i, (a, b) in enumerate(zip(on, off)):
        axes[0].text(i - 0.18, a + 0.25, str(a), ha="center", fontsize=8)
        axes[0].text(i + 0.18, b + 0.25, str(b), ha="center", fontsize=8)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(labels, fontsize=8)
    axes[0].set_ylabel("30 题累计次数")
    axes[0].set_title("幻觉类错误：越低越好")
    axes[0].legend(frameon=False, fontsize=8)

    names = ["证据不足时\n正确拒答", "有答案的题\n真的答了", "JSON\n可解析"]
    onv = [refuse_rate(rows, True)[0] / max(refuse_rate(rows, True)[1], 1),
           answer_rate(rows, True)[0] / max(answer_rate(rows, True)[1], 1),
           agg(rows, True)["json_ok"]]
    offv = [refuse_rate(rows, False)[0] / max(refuse_rate(rows, False)[1], 1),
            answer_rate(rows, False)[0] / max(answer_rate(rows, False)[1], 1),
            agg(rows, False)["json_ok"]]
    x2 = np.arange(len(names))
    axes[1].bar(x2 - 0.18, [v * 100 for v in onv], 0.36, label="开思考", color=ON)
    axes[1].bar(x2 + 0.18, [v * 100 for v in offv], 0.36, label="关思考", color=OFF)
    for i, (a, b) in enumerate(zip(onv, offv)):
        axes[1].text(i - 0.18, a * 100 + 1.5, f"{a*100:.0f}%", ha="center", fontsize=8)
        axes[1].text(i + 0.18, b * 100 + 1.5, f"{b*100:.0f}%", ha="center", fontsize=8)
    axes[1].set_xticks(x2)
    axes[1].set_xticklabels(names, fontsize=8)
    axes[1].set_ylim(0, 112)
    axes[1].set_ylabel("通过率（%）")
    axes[1].set_title("行为正确率：越高越好")
    axes[1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(CHART / "03_quality.png", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    d = load()
    rows = d["rows"]
    a_on, a_off = agg(rows, True), agg(rows, False)
    print(f"模型 {d['model']} · {len(rows)} 条记录 · {d.get('finished_at','')}\n")

    print(f"{'指标':22s}{'开思考':>12s}{'关思考':>12s}{'变化':>10s}")
    for k, label, unit in [("ms_mean", "平均耗时", "ms"), ("ms_p50", "中位耗时", "ms"),
                           ("out", "平均输出 token", ""), ("reason", "其中思维链", ""),
                           ("chars", "平均答案字数", "")]:
        on, off = a_on[k], a_off[k]
        delta = f"{(off / on - 1) * 100:+.0f}%" if on else "-"
        print(f"{label:22s}{on:>10.0f}{unit:<2s}{off:>10.0f}{unit:<2s}{delta:>10s}")
    print(f"{'JSON 可解析率':20s}{a_on['json_ok']*100:>10.0f}%{a_off['json_ok']*100:>11.0f}%")
    print(f"{'引用越界次数':20s}{a_on['bad_refs']:>11d}{a_off['bad_refs']:>11d}")
    print(f"{'数字不在证据里':20s}{a_on['bad_nums']:>11d}{a_off['bad_nums']:>11d}")
    for th, name in [(True, "开思考"), (False, "关思考")]:
        g, n = refuse_rate(rows, th)
        g2, n2 = answer_rate(rows, th)
        print(f"{name}：D 类正确拒答 {g}/{n}，A/B/C 类给出答案 {g2}/{n2}")

    print("\n--- 分类别平均耗时(ms) / 平均正文 token ---")
    for cat in ["fact", "number", "compare", "refuse", "struct"]:
        row = [CATS[cat]]
        for th in (True, False):
            rs = [r for r in rows if r["thinking"] is th and r["cat"] == cat and r.get("ok")]
            if rs:
                row.append(f"{st.mean(r['elapsed_ms'] for r in rs):.0f}/{st.mean(r['out_tokens'] for r in rs):.0f}")
            else:
                row.append("-")
        print(f"  {row[0]:10s} 开 {row[1]:>12s}   关 {row[2]:>12s}")

    fig_latency(rows)
    fig_tokens(rows)
    fig_quality(rows)
    print(f"\n图写到 {CHART}")


if __name__ == "__main__":
    main()
