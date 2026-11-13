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
ax.set_yticklabels([f"{t:.3g}" for t in _ticks], fontsize=6)
ax.set_rlabel_position(20)
ax.set_title("模型对比")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=2,
          frameon=False, fontsize=8)
fig.tight_layout()
fig.savefig("figure_radar.pdf", bbox_inches="tight")
fig.savefig("figure_radar.png", dpi=300, bbox_inches="tight")
print("saved figure_radar.pdf / .png")
