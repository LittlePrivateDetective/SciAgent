"""补充语料：补齐 CV / 生成模型 / 强化学习 / 经典序列建模方向。

为什么要补：现有 38 篇几乎全是 LLM/NLP 方向。这带来的不是"内容少"，而是
**问答会露出知识盲区**——问 ResNet 的残差为什么能缓解梯度消失、问 DDPM 的
前向加噪公式、问 DQN 的经验回放，全都会答"文献库中没有检索到相关内容"。
评审最容易在这种时候失去信任：一个"科研智能体"如果只会 LLM 论文，
它的"通用性"就无从谈起。

选择标准：每个方向 2–3 篇**被引量最高、且正文公式/表格丰富**的经典，
优先有明确数字结论的（方便验证数值抽取），避免综述（块多但信息稀）。

用法：
  ./.venv/Scripts/python.exe scripts/seed_corpus.py            # 全部
  ./.venv/Scripts/python.exe scripts/seed_corpus.py --only 3   # 先试 3 篇
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.research import discover  # noqa: E402

# (arXiv 编号, 标题, 方向) —— 方向只用于报告分组，不参与任何逻辑
TARGETS: list[tuple[str, str, str]] = [
    # 生成模型
    ("1312.6114", "Auto-Encoding Variational Bayes", "生成模型"),
    ("1406.2661", "Generative Adversarial Networks", "生成模型"),
    ("2006.11239", "Denoising Diffusion Probabilistic Models", "生成模型"),
    # 视觉
    ("1512.03385", "Deep Residual Learning for Image Recognition", "视觉"),
    ("1610.02391", "Grad-CAM: Visual Explanations from Deep Networks", "视觉"),
    ("1502.03167", "Batch Normalization: Accelerating Deep Network Training", "视觉"),
    # 强化学习
    ("1312.5602", "Playing Atari with Deep Reinforcement Learning", "强化学习"),
    ("1801.01290", "Soft Actor-Critic: Off-Policy Maximum Entropy RL", "强化学习"),
    ("2106.01345", "Decision Transformer: Reinforcement Learning via Sequence Modeling", "强化学习"),
    # 经典序列建模与优化
    ("1409.0473", "Neural Machine Translation by Jointly Learning to Align and Translate", "序列建模"),
    ("1412.6980", "Adam: A Method for Stochastic Optimization", "优化"),
    ("1503.02531", "Distilling the Knowledge in a Neural Network", "蒸馏"),
    # 大模型补充（库里缺的两条主线）
    ("2302.13971", "LLaMA: Open and Efficient Foundation Language Models", "大模型"),
    ("2204.02311", "PaLM: Scaling Language Modeling with Pathways", "大模型"),
    ("2212.08073", "Constitutional AI: Harmlessness from AI Feedback", "对齐"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, default=0, help="只跑前 N 篇（试跑用）")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    todo = TARGETS[: args.only] if args.only else TARGETS
    ok = skip = fail = 0
    t_all = time.time()
    for i, (aid, title, group) in enumerate(todo, 1):
        t0 = time.time()
        with SessionLocal() as db:
            res = discover.import_paper(db, aid, title, force=args.force)
        dt = time.time() - t0
        stage = res.get("stage") or ("ok" if res.get("ok") else "?")
        if res.get("ok"):
            ok += 1
            print(f"[{i}/{len(todo)}] OK    {aid:11s} {group:6s} {dt:5.1f}s "
                  f"chunks={res.get('chunks', '?')} {(res.get('title') or title)[:44]}")
        elif stage == "exists":
            skip += 1
            print(f"[{i}/{len(todo)}] 已有  {aid:11s} {group:6s} {res.get('message','')[:50]}")
        else:
            fail += 1
            print(f"[{i}/{len(todo)}] FAIL  {aid:11s} {group:6s} {stage}: {str(res.get('error'))[:90]}")

    print(f"\n新增 {ok} 篇，已存在 {skip} 篇，失败 {fail} 篇，耗时 {(time.time()-t_all)/60:.1f} 分钟")

    if ok:
        # 新文献进来后引用图谱必须重建，否则「引用关系」页签还是旧的 38 篇
        try:
            from app.rag.citation_graph import invalidate as invalidate_graph
            invalidate_graph()
            print("引用图谱缓存已失效，下次访问会重建")
        except Exception as exc:  # noqa: BLE001
            print(f"图谱缓存失效失败（不影响使用）：{exc}")


if __name__ == "__main__":
    main()
