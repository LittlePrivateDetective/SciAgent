"""重排候选裁剪实验（第二轮）：只裁正文候选，表格补位块必须保住。

第一轮发现 24 → 12 纯截断在 20 题上完全一致，但那是"碰巧"：
`_ensure_table_candidates` 是把表格 append 到池子末尾的，按名次截断会先切掉它们。
这个脚本比较三种策略的最终 top-8 是否一致，以及实际送进重排的条数。

  base : 现状，候选 24 条纯截断
  keep : 候选 16 条，但把所有 table 块强制并入
  keep12: 候选 12 条 + 表格强制并入
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))
logging.disable(logging.INFO)

import app.rag.hybrid as H  # noqa: E402

TOP_K = 8


def pick(hits: list[H.Hit], n: int, keep_tables: bool) -> list[H.Hit]:
    head = hits[:n]
    if not keep_tables:
        return head
    seen = {h.chunk_id for h in head}
    extra = [h for h in hits if h.kind == "table" and h.chunk_id not in seen]
    return head + extra


def one(q: str) -> dict:
    hits = H.search(q, None, top_k=40)
    if not hits:
        return {"q": q}
    hits = H._drop_shadowed_scans(hits, None)
    hits = H._ensure_table_candidates(q, hits, None)
    out = {"q": q, "pool": len(hits), "tables": sum(1 for h in hits if h.kind == "table")}
    for tag, n, keep in (("base", 24, False), ("keep", 16, True), ("keep12", 12, True)):
        send = pick(hits, n, keep)
        r = H._api_rerank(q, send, TOP_K)
        out[tag] = [h.chunk_id for h in (r or [])[:TOP_K]]
        out[tag + "_n"] = len(send)
    return out


def main() -> None:
    rows = json.loads((ROOT / "data" / "bench_chat.json").read_text(encoding="utf-8"))
    res = []
    for r in rows:
        if r.get("error"):
            continue
        o = one(r["q"])
        o["kind"], o["idx"] = r["kind"], r["idx"]
        res.append(o)
        flag = "同" if o.get("keep") and o["base"] == o["keep"] else "变"
        print(f"  {r['idx']:2d} {r['kind']:4s} pool={o['pool']:2d} tab={o['tables']} "
              f"送{ o.get('base_n')}→{o.get('keep_n')} {flag}", flush=True)

    (ROOT / "data" / "bench_budget.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 与现状（候选24）对比 ===")
    for tag, name in (("keep", "16+保表"), ("keep12", "12+保表")):
        same = sum(1 for o in res if o.get(tag) and o[tag] == o["base"])
        print(f"  {name}: 完全一致 {same}/{len(res)} 题")
    avg_send = sum(o.get("keep_n") or 0 for o in res) / max(len(res), 1)
    avg_base = sum(o.get("base_n") or 0 for o in res) / max(len(res), 1)
    print(f"  平均送入重排条数：{avg_base:.1f} → {avg_send:.1f}")
    diff = [o for o in res if o.get("keep") and o["base"] != o["keep"]]
    for o in diff:
        print(f"    {o['idx']:2d} {o['kind']} 有差异")


if __name__ == "__main__":
    main()
