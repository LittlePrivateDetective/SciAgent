"""有明确条件的低秩适配参数计算；不把用户已给数值误当成缺失证据。"""
from __future__ import annotations
import re


def lora_square_answer(question: str, evidence: list[dict]) -> str | None:
    q = question or ""
    if not re.search(r"\bLoRA\b|低秩", q, re.I):
        return None
    if not re.search(r"参数|形状|计算|矩阵", q):
        return None
    dm = re.search(r"d_?\{?model\}?\s*=\s*(\d+)", q, re.I)
    rm = re.search(r"(?<![a-z_])r\s*=\s*(\d+)", q, re.I)
    if not dm or not rm:
        return None
    d, r = int(dm.group(1)), int(rm.group(1))
    if d <= 0 or r <= 0 or r > d:
        return None
    total = 2 * d * r
    cite = ""
    for i, ev in enumerate(evidence, start=1):
        text = ev.get("text") or ""
        if re.search(r"B\s*∈|rank decomposition|W0\s*\+", text, re.I):
            cite = f" [{i}]"
            break
    return (f"依据 LoRA 的矩阵分解定义 ΔW = BA{cite}，并将用户给出的 d_model={d} "
            f"解释为单个 {d}×{d} 权重矩阵：\n"
            f"- A 的形状为 {r}×{d}，B 的形状为 {d}×{r}。\n"
            f"- 可训练参数量 = r×d + d×r = 2×{d}×{r} = **{total}** 个。\n"
            "这里只计算 A 与 B，预训练权重 W0 冻结，不计入可训练参数。")
