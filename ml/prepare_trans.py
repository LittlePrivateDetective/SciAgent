"""把库里已缓存的中英译文变成"翻译 SFT 样本"。

为什么格式要对齐线上**本地路由**的样子：
`LLM_ROUTE=local` 时 `bilingual._translate_batch` 会用精简提示词 `_SYS_LITE`，
输入是 JSON 数组、输出是 `{"out": [...]}`。如果训练时按"一段原文→一段译文"喂，
上线时却是"JSON 数组进出"，3B 会当场不会做——格式比内容更容易训错。

所以样本长这样：
    system    = _SYS_LITE（与代码同源）
    user      = ["para1", "para2"]
    assistant = {"out": ["译文1", "译文2"]}

只收"经过校验"的译文：数字齐全、无整段英文残留、长度合理（这几条正是
`tests/trans_bench.py` 里量过的指标，门槛与那边一致）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag.bilingual import _SYS_LITE, _degenerate, keep_verbatim  # noqa: E402
from app.rag.chunker import strip_carry  # noqa: E402

RE_NUM = re.compile(r"\d+(?:[.,]\d+)*%?")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
OUT = ROOT / "data" / "sft" / "trans.jsonl"


def main() -> None:
    con = sqlite3.connect(ROOT / "data" / "sciagent.db")
    con.row_factory = sqlite3.Row
    # 逐篇按阅读顺序推进，才能正确剥掉分块重叠
    rows = con.execute(
        "select c.id, c.doc_id, c.page, c.idx, c.kind, c.text, t.zh "
        "from chunks c join translations t on t.chunk_id = c.id "
        "where t.model != 'verbatim' order by c.doc_id, c.page, c.idx"
    ).fetchall()
    con.close()

    pairs: list[tuple[str, str]] = []
    prev: dict[str, str] = {}
    for r in rows:
        src = strip_carry(prev.get(r["doc_id"], ""), r["text"] or "").strip()
        prev[r["doc_id"]] = r["text"] or ""
        zh = (r["zh"] or "").strip()
        if not src or not zh or len(src) < 80 or keep_verbatim(r["kind"], src):
            continue
        if _degenerate(src, zh):
            continue
        # 校验：数字齐全、中文占比够（不是把原文抄回来）
        nums = set(RE_NUM.findall(src.replace(" ", "")))
        if any(n not in zh for n in nums):
            continue
        if len(RE_CJK.findall(zh)) < 0.15 * len(zh):
            continue
        pairs.append((src, zh))

    # 两段一组：与线上 trans_batch=4 的量级接近，但 3B 上下文小，用 2 段更稳
    samples = []
    for i in range(0, len(pairs) - 1, 2):
        (s1, z1), (s2, z2) = pairs[i], pairs[i + 1]
        samples.append({
            "messages": [
                {"role": "system", "content": _SYS_LITE},
                {"role": "user", "content": json.dumps([s1, s2], ensure_ascii=False)},
                {"role": "assistant", "content": json.dumps({"out": [z1, z2]}, ensure_ascii=False)},
            ],
            "meta": {"source": "translation"},
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"翻译样本 {len(samples)} 条（可用译文 {len(pairs)} 段）→ {OUT}")


if __name__ == "__main__":
    main()
