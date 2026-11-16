"""用本地 bge-m3 为全部 chunk 算一份向量，写入 chunks.embedding_local。

为什么单独一列而不是覆盖 embedding：
  云版用 qwen3.7-text-embedding（1024 维），本地版用 bge-m3（也是 1024 维），
  维度一样但**语义空间不同**，两套向量不能混用。存两列，切换时各读各的，
  云版完全不受影响，也能随时切回去。

为什么要 ml/.venv：
  运行环境的 .venv 没装 torch（纯 CPU 部署不想背 2GB 依赖），
  这个脚本只在建索引时跑一次，用训练环境跑最省事。它只写 SQLite，不依赖 app 包。

用法：
    ml/.venv/Scripts/python.exe scripts/build_local_vectors.py
    # 想换模型：AB_MODEL_DIR=... 同 ab_embed.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "deploy" / "data" / "sciagent.db"
MODEL_DIR = Path(os.environ.get("AB_MODEL_DIR", r"D:\models\models\bge-m3"))
BATCH = int(os.environ.get("AB_BATCH", "16"))
MAX_LEN = int(os.environ.get("AB_MAX_LEN", "512"))


def main() -> None:
    if not (MODEL_DIR / "config.json").exists():
        sys.exit(f"模型目录不对：{MODEL_DIR}")

    con = sqlite3.connect(str(DB), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    # 只取还没算过的：这个脚本可能因为关机/中断跑不完，重跑时应当接着来。
    # 已编码的不会被重新计算，也不会被覆盖。
    rows = con.execute(
        "SELECT id, text FROM chunks "
        "WHERE embedding IS NOT NULL AND embedding_local IS NULL ORDER BY id"
    ).fetchall()
    total = len(rows)
    print(f"待编码 {total} 条", flush=True)
    if not total:
        sys.exit(0)

    import torch
    from transformers import AutoModel, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"加载 {MODEL_DIR.name} 到 {device}", flush=True)
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModel.from_pretrained(str(MODEL_DIR)).to(device).eval()

    t0 = time.perf_counter()
    done = 0
    pending: list[tuple[bytes, str]] = []
    with torch.no_grad():
        for i in range(0, total, BATCH):
            part = rows[i : i + BATCH]
            texts = [(r[1] or "")[:4000] for r in part]
            b = tok(texts, padding=True, truncation=True, max_length=MAX_LEN,
                    return_tensors="pt")
            b = {k: v.to(device) for k, v in b.items()}
            h = model(**b).last_hidden_state[:, 0]          # bge 系列用 CLS 池化
            h = torch.nn.functional.normalize(h, dim=-1)
            vecs = h.float().cpu().numpy().astype(np.float32)

            pending.extend((v.tobytes(), part[j][0]) for j, v in enumerate(vecs))
            done += len(part)
            # 攒够 256 条再提交：SQLite 每次 commit 都要刷盘，8 条一提交时
            # 实测只有 337 条/分钟，比编码本身慢得多。
            if len(pending) >= 256:
                con.executemany("UPDATE chunks SET embedding_local=? WHERE id=?", pending)
                con.commit()
                pending.clear()
            if done % 2000 < BATCH:
                rate = done / max(time.perf_counter() - t0, 1e-6)
                left = (total - done) / max(rate, 1e-6)
                print(f"  {done}/{total}  {rate:.0f} 条/秒  剩余约 {left/60:.1f} 分钟",
                      flush=True)

    if pending:
        con.executemany("UPDATE chunks SET embedding_local=? WHERE id=?", pending)
        con.commit()
    el = time.perf_counter() - t0
    n = con.execute("SELECT COUNT(*) FROM chunks WHERE embedding_local IS NOT NULL").fetchone()[0]
    con.close()
    print(f"完成：{n} 条，用时 {el/60:.1f} 分钟（{total/el:.0f} 条/秒）")


if __name__ == "__main__":
    main()
