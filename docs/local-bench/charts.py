"""把 bench.py 的结果画成四张对比图（中文标注）。

只读 results/ 下最新的一份 json，不重新跑模型——图形可以反复调，
但模型调用是花钱和花时间的。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "charts"
CJK = ["SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", "SimSun"]
_have = {f.name for f in fm.fontManager.ttflist}
plt.rcParams.update({
    "font.family": ["Times New Roman", "DejaVu Serif"] + [n for n in CJK if n in _have],
    "font.size": 10,
    "axes.unicode_minus": False,
    "figure.dpi": 200,
    "axes.spines.top": False,
    "axes.spines.right": False,
})
COLORS = {"local": "#d1694a", "cloud-fast": "#2f5bd7", "cloud-main": "#3f7d52"}
SHORT = {"local": "本地 3B", "cloud-fast": "云端 qwen-flash", "cloud-main": "云端 deepseek"}
CATS = {"qa": "中文问答", "en2zh": "英译中", "zh2en": "中译英", "json": "结构化 JSON", "long": "长文本"}


def load_latest() -> dict:
    files = sorted(glob.glob(str(ROOT / "results" / "bench_*.json")))
    if not files:
        raise SystemExit("results/ 下没有结果文件，先跑 bench.py")
    return json.loads(Path(files[-1]).read_text(encoding="utf-8"))


def fig_latency(d: dict) -> None:
    models = [m for m in d["models"] if any(r["model"] == m for r in d["rows"])]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4))

    # 左：TTFT（交互手感）
    for i, m in enumerate(models):
        vals = [r["ttft_ms"] for r in d["rows"] if r["model"] == m]
        axes[0].bar(i, np.mean(vals), color=COLORS[m], width=0.6)
        axes[0].text(i, np.mean(vals) + 40, f"{np.mean(vals):.0f}", ha="center", fontsize=9)
    axes[0].set_xticks(range(len(models)))
    axes[0].set_xticklabels([SHORT[m] for m in models], fontsize=9)
    axes[0].set_ylabel("首字延迟 TTFT（ms）")
    axes[0].set_title("交互手感：从发送到第一个字")

    # 右：按类别的平均总耗时
    width = 0.8 / max(1, len(models))
    cats = list(CATS)
    for i, m in enumerate(models):
        means = []
        for c in cats:
            vals = [r["total_ms"] for r in d["rows"] if r["model"] == m and r["cat"] == c]
            means.append(np.mean(vals) if vals else 0)
        axes[1].bar(np.arange(len(cats)) + i * width - 0.4 + width / 2, means,
                    width * 0.92, label=SHORT[m], color=COLORS[m])
    axes[1].set_xticks(range(len(cats)))
    axes[1].set_xticklabels([CATS[c] for c in cats], fontsize=9)
    axes[1].set_ylabel("平均总耗时（ms）")
    axes[1].set_title("单组任务完成时间")
    axes[1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "01_latency.png", bbox_inches="tight")
    plt.close(fig)


def fig_throughput(d: dict) -> None:
    models = [m for m in d["models"] if any(r["model"] == m for r in d["rows"])]
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    cats = list(CATS)
    width = 0.8 / max(1, len(models))
    for i, m in enumerate(models):
        means = []
        for c in cats:
            vals = [r["tok_s"] for r in d["rows"] if r["model"] == m and r["cat"] == c and r["tok_s"] > 0]
            means.append(np.mean(vals) if vals else 0)
        ax.bar(np.arange(len(cats)) + i * width - 0.4 + width / 2, means,
               width * 0.92, label=SHORT[m], color=COLORS[m])
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels([CATS[c] for c in cats], fontsize=9)
    ax.set_ylabel("生成速度（token/s）")
    ax.set_title("生成吞吐：同样的字数要等多久")
    ax.legend(frameon=False, fontsize=8, ncol=3)
    fig.tight_layout()
    fig.savefig(OUT / "02_throughput.png", bbox_inches="tight")
    plt.close(fig)


def fig_quality(d: dict) -> None:
    models = [m for m in d["models"] if any(r["model"] == m for r in d["rows"])]
    rows = []
    for m in models:
        rs = [r for r in d["rows"] if r["model"] == m]
        js = [r for r in rs if r["cat"] == "json"]
        lg = [r for r in rs if r.get("lang_ok") is not None]
        rows.append({
            "m": m,
            "empty": sum(1 for r in rs if r["empty"]) / max(1, len(rs)),
            "json_fail": sum(1 for r in js if not r.get("json_ok")) / max(1, len(js)),
            "lang_fail": sum(1 for r in lg if not r.get("lang_ok")) / max(1, len(lg)),
            "repeat": np.mean([r["repeat_ratio"] for r in rs]),
        })

    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    labels = ["空输出", "JSON\n解析失败", "语种\n不符", "复读\n占比"]
    keys = ["empty", "json_fail", "lang_fail", "repeat"]
    width = 0.8 / max(1, len(rows))
    for i, r in enumerate(rows):
        vals = [r[k] * 100 for k in keys]
        bars = ax.bar(np.arange(len(keys)) + i * width - 0.4 + width / 2, vals,
                      width * 0.92, label=SHORT[r["m"]], color=COLORS[r["m"]])
        for b, v in zip(bars, vals):
            if v > 0:
                ax.text(b.get_x() + b.get_width() / 2, v + 0.8, f"{v:.0f}", ha="center", fontsize=7.5)
    ax.set_xticks(range(len(keys)))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("占比（%）")
    top = max([r[k] * 100 for r in rows for k in keys] + [1])
    ax.set_ylim(0, top * 1.32 + 1)      # 留出数值标签的空间，否则 12% 那根柱子会顶到标题
    ax.set_title("质量故障率：越低越好（复读按最长重复片段占比）")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "03_quality.png", bbox_inches="tight")
    plt.close(fig)


def fig_prefill() -> None:
    """固定输出长度，只看输入变长带来的首字延迟。

    40 组基准里的"总耗时"混了 prefill 与 decode，答案长短差三倍，画不出干净的趋势；
    这里读 prefill_*.json —— 输出锁死 16 token，输入从 400 涨到 5900。
    """
    files = sorted(glob.glob(str(ROOT / "results" / "prefill_*.json")))
    if not files:
        return
    d = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    fig, ax = plt.subplots(figsize=(6.8, 3.3))
    for m in ("local", "cloud-fast"):
        rs = [r for r in d["rows"] if r["model"] == m and r["ttft_ms"]]
        if not rs:
            continue
        xs = [r["in_tokens"] for r in rs]
        ys = [r["ttft_ms"] for r in rs]
        ax.plot(xs, ys, "o-", color=COLORS[m], label=SHORT[m], linewidth=1.5, markersize=5)
        ax.annotate(f"{ys[-1]:.0f}ms", xy=(xs[-1], ys[-1]), xytext=(6, -12),
                    textcoords="offset points", fontsize=8, color=COLORS[m])
    # 第一档含模型加载（冷启动），单独标出来，免得读者以为 400 token 比 800 token 还慢
    ax.annotate("首档含模型加载", xy=(d["rows"][0]["in_tokens"], d["rows"][0]["ttft_ms"]),
                xytext=(10, 18), textcoords="offset points", fontsize=7.5, color="#8a93a3",
                arrowprops=dict(arrowstyle="-", color="#b9c0cc", linewidth=0.8))
    ax.set_xlabel("输入 token 数（输出固定 16 token）")
    ax.set_ylabel("首字延迟（ms）")
    ax.set_title("长上下文：本地预填成本随输入线性上涨，云端基本恒定")
    ax.legend(frameon=False, fontsize=8)
    ax.grid(linestyle="--", linewidth=0.5, alpha=0.5)
    fig.tight_layout()
    fig.savefig(OUT / "04_prefill.png", bbox_inches="tight")
    plt.close(fig)


def fig_concurrency() -> None:
    """并发 4 路 vs 串行：本地单卡的加速比几乎为 1，这是它最硬的短板。"""
    files = sorted(glob.glob(str(ROOT / "results" / "concurrency_*.json")))
    if not files:
        return
    d = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    models = [m for m in d["models"] if d["models"][m].get("wall_baseline_s")]
    if not models:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.3))
    x = np.arange(len(models))
    w = 0.36

    seq = [d["models"][m]["wall_baseline_s"] for m in models]
    par = [d["models"][m]["wall_s"] for m in models]
    axes[0].bar(x - w / 2, seq, w, label="串行", color="#b9c0cc")
    axes[0].bar(x + w / 2, par, w, label=f"并发 {d['workers']}", color="#2f5bd7")
    for i, m in enumerate(models):
        sp = d["models"][m]["speedup"]
        axes[0].text(i + w / 2, par[i] + 0.25, f"{sp:.2f}×", ha="center", fontsize=9)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels([SHORT.get(m, m) for m in models], fontsize=9)
    axes[0].set_ylabel(f"{d['requests']} 个请求的总墙钟时间（s）")
    axes[0].set_title("同样 8 个问题：并发能省多少时间")
    axes[0].legend(frameon=False, fontsize=8)

    p50 = [d["models"][m]["req_p50_ms"] for m in models]
    bars = axes[1].bar(x, p50, 0.55, color=[COLORS.get(m, "#888") for m in models])
    for b, v in zip(bars, p50):
        axes[1].text(b.get_x() + b.get_width() / 2, v + 60, f"{v:.0f}ms", ha="center", fontsize=9)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([SHORT.get(m, m) for m in models], fontsize=9)
    axes[1].set_ylabel("并发下单个请求的 p50 延迟（ms）")
    axes[1].set_title("排队代价：并发时用户实际等到多久")
    fig.tight_layout()
    fig.savefig(OUT / "05_concurrency.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_latest()
    fig_latency(data)
    fig_throughput(data)
    fig_quality(data)
    fig_prefill()
    fig_concurrency()
    print("charts:", [p.name for p in sorted(OUT.glob("*.png"))])
    print("summary:", json.dumps(data.get("summary", {}), ensure_ascii=False, indent=1))
