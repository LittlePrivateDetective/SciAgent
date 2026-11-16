"""离线 A/B：线上 API 向量 vs 本地 bge-small-zh，比召回。

不碰线上服务：只读 deploy/data/sciagent.db（read-only），自己算向量自己算余弦。
指标用"文档级 recall@10 + 首个相关块的名次"，因为跨语言检索最难的一步
恰恰是"中文问题能不能找到那篇英文论文"。

用法：
    ml/.venv/Scripts/python.exe scripts/ab_embed.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "deploy" / "data" / "sciagent.db"
# 换模型跑同一套 A/B 时用环境变量覆盖，不用改脚本：
#   AB_MODEL_DIR=D:/models/models/bge-m3 AB_CACHE=ab_bge_m3.npy AB_BATCH=16 python scripts/ab_embed.py
MODEL_DIR = Path(os.environ.get(
    "AB_MODEL_DIR",
    r"D:\models\models\BAAI--bge-small-zh-v1.5\snapshots\master",
))
AB_CACHE = os.environ.get("AB_CACHE", "ab_local_vec.npy")
AB_BATCH = int(os.environ.get("AB_BATCH", "48"))
OUT = ROOT / "data" / "ab_embed.json"

# bge 中文模型检索时要加的指令前缀；不加会掉几个点，官方 README 明确要求
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

# 每题「应该被召回」的文档（用文件名片段匹配）。多跳题给两篇；D1 是开放选题，没有唯一答案，跳过。
GOLD: dict[str, list[str]] = {
    "A1": ["N19-1423"], "A2": ["2106.09685"], "A3": ["1706.03762"], "A4": ["1910.10683"],
    "B1": ["2407.21783", "1910.10683"], "B2": ["2005.14165", "2407.21783"],
    "B3": ["N19-1423", "language_understanding"], "B4": ["2305.14314"],
    "C1": ["2001.08361", "2210.02414", "2407.21783"],
    "C2": ["2106.09685", "2305.14314"], "C3": ["2305.18290", "2203.02155"],
    "D2": ["2305.14314", "2106.09685"], "D3": ["2212.04356"],
    "E1": ["2106.09685"], "E2": ["1910.02054", "2305.13048"], "E3": ["N19-1423"],
    "F1": ["N19-1423"], "F2": ["2407.21783"], "F3": ["新闻推荐增量学习"],
}

QUESTIONS: dict[str, str] = {
    "A1": "BERT 的预训练用了哪两个任务？",
    "A2": "LoRA 论文里秩 r 取多少时效果最好？",
    "A3": "Attention 里缩放点积为什么要除以根号 dk？",
    "A4": "T5 的预训练目标是什么？",
    "B1": "Llama 3 的训练数据规模比 T5 大约多多少倍？",
    "B2": "GPT-3 和 Llama 3 在 few-shot 上的结论有什么不同？",
    "B3": "哪篇最早用双向编码器做预训练，它和 GPT 的单向做法差在哪？",
    "B4": "QLoRA 的显存节省主要来自哪两处？各占多少？",
    "C1": "这几篇对 scaling law 的结论有什么不一致？",
    "C2": "LoRA 和 QLoRA 在“是否损失精度”上的说法有冲突吗？",
    "C3": "关于 RLHF 的必要性，DPO 和 InstructGPT 的立场差异是什么？",
    "D2": "只有一张 6GB 显卡做微调，应该参考哪篇？",
    "D3": "想做翻译质量评测，哪篇给了可复用的方法？",
    "E1": "那个把权重更新拆成两个低秩矩阵相乘的方法叫什么？",
    "E2": "有没有论文讨论过相对位置编码、能处理超长上下文？",
    "E3": "哪篇讲了把一句话里的词随机遮住再让模型猜？",
    "F1": "BERT 论文里 next sentence prediction 具体怎么构造正负样本？",
    "F2": "Llama 3 的 safety 数据是怎么处理的？",
    "F3": "计算机学报那篇新闻推荐论文用了什么增量学习方法？",
}


def load_chunks() -> tuple[list[str], list[str], list[str], np.ndarray]:
    """read-only 打开线上库，取出 (chunk_id, doc_name, text, api 向量矩阵)。"""
    uri = f"file:{DB.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    rows = con.execute(
        "select c.id, d.filename, c.text, c.embedding from chunks c join documents d on d.id=c.doc_id"
    ).fetchall()
    con.close()
    ids = [str(r[0]) for r in rows]
    docs = [r[1] or "" for r in rows]
    texts = [r[2] or "" for r in rows]
    mat = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    return ids, docs, texts, mat.astype(np.float32)


def local_encode(texts: list[str], device: str, batch: int = AB_BATCH, max_len: int = 512) -> np.ndarray:
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModel.from_pretrained(str(MODEL_DIR)).to(device).eval()
    out: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch):
            b = tok(texts[i:i + batch], padding=True, truncation=True,
                    max_length=max_len, return_tensors="pt")
            b = {k: v.to(device) for k, v in b.items()}
            h = model(**b).last_hidden_state[:, 0]          # bge 用 CLS 池化
            h = torch.nn.functional.normalize(h, dim=-1)
            out.append(h.float().cpu().numpy())
    return np.vstack(out).astype(np.float32)


def _env(key: str, default: str = "") -> str:
    env = ROOT / "deploy" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return default


def api_encode(texts: list[str]) -> tuple[np.ndarray, float]:
    """直接调 OpenAI 兼容的 /embeddings（与线上同一模型），不经过 app 包。"""
    import json as _json
    import urllib.request

    base = _env("EMB_BASE_URL").rstrip("/")
    key = _env("EMB_API_KEY") or _env("LLM_API_KEY")
    model = _env("EMB_MODEL", "qwen3.7-text-embedding")
    body = {"model": model, "input": texts, "encoding_format": "float"}
    req = urllib.request.Request(
        base + "/embeddings",
        data=_json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    ms = (time.perf_counter() - t0) * 1000
    vecs = [d["embedding"] for d in data["data"]]
    return np.asarray(vecs, dtype=np.float32), ms


def hit_rank(docs: list[str], sims: np.ndarray, gold: list[str], k: int = 10) -> tuple[bool, int | None]:
    order = np.argsort(-sims)[:k]
    for pos, idx in enumerate(order, start=1):
        if any(g in docs[idx] for g in gold):
            return True, pos
    return False, None


def main() -> None:
    print("载入库内块与线上向量…")
    ids, docs, texts, api_mat = load_chunks()
    print(f"  块数 {len(ids)} · API 维度 {api_mat.shape[1]}")

    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    cache = ROOT / "data" / AB_CACHE
    print(f"本地模型编码 {len(texts)} 块（device={dev}）…")
    t0 = time.perf_counter()
    if cache.exists():
        loc_mat = np.load(cache)
        if loc_mat.shape[0] != len(texts):
            loc_mat = None
    else:
        loc_mat = None
    if loc_mat is None:
        try:
            loc_mat = local_encode(texts, dev)
        except (RuntimeError, MemoryError) as exc:   # 显存被 Ollama 占着时退回 CPU
            print(f"  {dev} 编码失败（{exc}），退回 CPU/256 token")
            loc_mat = local_encode(texts, "cpu", batch=32, max_len=512)
        np.save(cache, loc_mat)
    build_s = time.perf_counter() - t0
    print(f"  完成：{build_s:.1f}s · 本地维度 {loc_mat.shape[1]}")

    keys = [k for k in QUESTIONS if k in GOLD]
    qs = [QUESTIONS[k] for k in keys]
    api_q, api_ms = api_encode(qs)
    api_q /= np.maximum(np.linalg.norm(api_q, axis=1, keepdims=True), 1e-9)

    # 本地查询向量：单条 CPU 编码，模拟云服务器上的真实延迟
    q_loc_texts = [QUERY_PREFIX + q for q in qs]
    t0 = time.perf_counter()
    loc_q = local_encode(q_loc_texts, "cpu")
    local_q_ms = (time.perf_counter() - t0) * 1000 / len(qs)
    loc_q /= np.maximum(np.linalg.norm(loc_q, axis=1, keepdims=True), 1e-9)

    rows = []
    for i, k in enumerate(keys):
        a_hit, a_rank = hit_rank(docs, api_mat @ api_q[i], GOLD[k])
        l_hit, l_rank = hit_rank(docs, loc_mat @ loc_q[i], GOLD[k])
        rows.append({"id": k, "q": QUESTIONS[k], "gold": GOLD[k],
                     "api_hit": a_hit, "api_rank": a_rank,
                     "local_hit": l_hit, "local_rank": l_rank})
        mark = "OK " if (a_hit and l_hit) else ("掉召回" if a_hit and not l_hit else ("改善" if l_hit and not a_hit else "都漏"))
        print(f"  {k:3s} API {'✓' if a_hit else '✗'}({a_rank or '-'})  "
              f"本地 {'✓' if l_hit else '✗'}({l_rank or '-'})  {mark}")

    n = len(rows)
    a_rec = sum(r["api_hit"] for r in rows) / n
    l_rec = sum(r["local_hit"] for r in rows) / n
    a_mrr = sum(1 / r["api_rank"] for r in rows if r["api_rank"]) / n
    l_mrr = sum(1 / r["local_rank"] for r in rows if r["local_rank"]) / n
    print(f"\n召回@10  API {a_rec:.0%} → 本地 {l_rec:.0%}")
    print(f"MRR@10   API {a_mrr:.3f} → 本地 {l_mrr:.3f}")
    print(f"查询向量化延迟  API {api_ms / n:.0f}ms/次 → 本地 {local_q_ms:.0f}ms/次（CPU 单条）")
    OUT.write_text(json.dumps({"rows": rows, "api_recall": a_rec, "local_recall": l_rec,
                               "api_mrr": a_mrr, "local_mrr": l_mrr,
                               "api_ms": api_ms / n, "local_ms": local_q_ms,
                               "loc_dim": int(loc_mat.shape[1]), "build_s": build_s},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
