"""由 SciAgent 从 CSV 生成 —— 每个数值与源表逐格一致，可直接改样式。"""
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

labels = ["Transformer", "BERT", "GPT-3"]
series = {"BLEU": [28.4, 34.5, 41.0], "Params": [65, 110, 175]}

x = np.arange(len(labels))
w = 0.8 / len(series)
fig, ax = plt.subplots(figsize=(3.5, 2.5))
for i, (name, vals) in enumerate(series.items()):
    bars = ax.bar(x + i * w - 0.4 + w / 2, vals, w * 0.92, label=name)
    ax.bar_label(bars, fmt="%.4g", fontsize=6, padding=1.5)   # 柱顶标数值，读者不用估读
ax.set_xticks(x)
ax.set_xticklabels(labels)
ax.set_ylabel("value")
ax.set_title("模型对比")
ax.legend(frameon=False, fontsize=8)
ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.6)
ax.set_axisbelow(True)
fig.tight_layout()
fig.savefig("figure_bar.pdf", bbox_inches="tight")
fig.savefig("figure_bar.png", dpi=300, bbox_inches="tight")
print("saved figure_bar.pdf / .png")
