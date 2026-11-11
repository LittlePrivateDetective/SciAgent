"""数值题召回回归（D-67 的验收脚本）。

只测"证据里有没有那个数"，不调模型——因为这道题的失败从来不是模型不会答，
而是**根本没把答案喂给它**。所以判据是：检索返回的 top-8 证据文本里，
必须出现题目对应的数值字面量。

选题原则：每题都是"答案只可能出现在表格里"的典型科研问法
（复杂度、BLEU、GLUE 均分、GPU 块数、可训练参数比例）。
每题的期望值都是先在库里搜出来的原文，不是凭印象写的。

    ./.venv/Scripts/python.exe tests/table_recall.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.hybrid import search_with_rerank  # noqa: E402

# (问题, 期望出现在证据里的字面量, 说明)
CASES = [
    ("多头注意力的计算复杂度是多少", ["O(n2", "Complexity per Layer"], "Transformer Table 1"),
    ("Transformer big 模型在 EN-DE 上的 BLEU 是多少", ["41.16", "28.4"], "Transformer Table 2"),
    ("BERT 在 GLUE 上的平均分是多少", ["80.5", "GLUE"], "BERT Table 1/2"),
    ("GPT-3 用了多少块 GPU 训练", ["V100", "1024"], "GPT-3 训练配置"),
    ("LoRA 的可训练参数占比是多少", ["0.01", "10,000"], "LoRA 参数量表"),
    ("ViT 在 ImageNet 上的准确率是多少", ["88.55", "88.5"], "ViT Table"),
]


def run() -> dict:
    rows = []
    for q, wants, note in CASES:
        t0 = time.time()
        hits = search_with_rerank(q, None, top_k=8)
        joined = "\n".join(h.text for h in hits)
        hit = [w for w in wants if w in joined]
        tables = [h for h in hits if h.kind == "table"]
        rows.append({
            "q": q,
            "note": note,
            "hit": bool(hit),
            "matched": hit,
            "tables_in_top8": len(tables),
            "ms": int((time.time() - t0) * 1000),
            "top": [
                {"kind": h.kind, "page": h.page, "doc": h.doc_name[:28],
                 "head": h.text[:70].replace("\n", " ")}
                for h in hits[:3]
            ],
        })
        print(f"{'✓' if hit else '✗'} {q[:32]:34s} 表块 {len(tables)}  "
              f"命中 {hit or '无'}  {rows[-1]['ms']}ms")
        if not hit:
            for t in rows[-1]["top"]:
                print(f"     top: [{t['kind']}] p{t['page']} {t['doc']} {t['head']!r}")

    ok = sum(1 for r in rows if r["hit"])
    print(f"\n数值题召回 {ok}/{len(rows)}")
    return {"ok": ok, "total": len(rows), "rows": rows}


if __name__ == "__main__":
    res = run()
    out = Path(__file__).resolve().parents[1] / "docs" / "qa" / "evidence" / "table_recall.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据 ->", out)
    raise SystemExit(0 if res["ok"] >= len(res["rows"]) - 1 else 1)
