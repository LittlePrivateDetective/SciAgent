"""重排预算实验：不动向量，只改「送几条进重排」和「能不能跳过重排」。

要回答两件事：
1. 候选 24 → 16 / 12，最终 top-8 的块变不变？变了是变好还是变坏？
2. 召回第 1 名领先很多时直接跳过重排，会不会把正确答案丢掉？

输出 data/bench_rerank.json，控制台打印对比表。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))
logging.disable(logging.INFO)

import app.rag.hybrid as H  # noqa: E402

TOP_K = 8


def one(q: str) -> dict:
    prof: dict = {}
    # 候选池固定 40，和线上一致；后面只改"送几条进重排"
    hits = H.search(q, None, top_k=40, profile=prof)
    if not hits:
        return {"q": q, "empty": True}
    hits = H._drop_shadowed_scans(hits, None)
    hits = H._ensure_table_candidates(q, hits, None)

    rec_top = [h.chunk_id for h in hits[:TOP_K]]
    # 召回阶段的领先度：BM25 词法分，用它判断"第一名是否压倒性领先"
    sp = [h.sparse_score for h in hits[:3]]
    lead = (sp[0] / sp[1]) if len(sp) > 1 and sp[1] > 0 else 9.9

    out = {"q": q, "cand": len(hits), "lead": round(lead, 2), "recall_top": rec_top}
    for n in (24, 16, 12):
        m = min(n, len(hits))
        r = H._api_rerank(q, hits, m)
        if not r:
            out[str(n)] = None
            out[f"{n}_ms"] = None
            continue
        final = r[:TOP_K]
        out[str(n)] = [h.chunk_id for h in final]
        # 重排把哪些块的顺序改了
        out[f"{n}_top1_same"] = bool(final) and final[0].chunk_id == rec_top[0]
    return out


def jac(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 1.0


def main() -> None:
    rows = json.loads((ROOT / "data" / "bench_chat.json").read_text(encoding="utf-8"))
    res = []
    for r in rows:
        if r.get("error"):
            continue
        t0 = time.perf_counter()
        o = one(r["q"])
        o["kind"] = r["kind"]
        o["idx"] = r["idx"]
        o["sec"] = round(time.perf_counter() - t0, 1)
        res.append(o)
        print(f"  {r['idx']:2d} {r['kind']:4s} lead={o.get('lead')} "
              f"top1同={o.get('24_top1_same')}", flush=True)

    (ROOT / "data" / "bench_rerank.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 候选数缩减对最终 top8 的影响（以 24 为基准）===")
    for n in (16, 12):
        same = sum(1 for o in res if o.get(str(n)) and o.get(str(n)) == o["24"])
        avg = sum(jac(o[str(n)], o["24"]) for o in res if o.get(str(n))) / max(
            len([o for o in res if o.get(str(n))]), 1)
        print(f"  24 → {n}: 完全一致 {same}/{len(res)} 题，平均重合度 {avg:.3f}")

    print("\n=== 重排有没有改变第 1 名 ===")
    changed = [o for o in res if o.get("24") and not o.get("24_top1_same")]
    print(f"  改变了 {len(changed)}/{len(res)} 题")
    for o in changed:
        print(f"    {o['idx']:2d} {o['kind']} lead={o['lead']}")


if __name__ == "__main__":
    main()
