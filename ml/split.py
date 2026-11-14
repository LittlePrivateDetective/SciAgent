"""把生成的问答样本切成 train / dev：dev 只用于"微调前后"对比，绝不进训练。

按 chunk 切而不是按行切——同一段证据生成的两条题会一起进 dev，
否则 dev 里会出现"训练时见过这段证据、只是换了个问法"的假提升。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "sft"


def main(n_dev: int = 40) -> None:
    src = DATA / "qa.jsonl"
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
    rng = random.Random(20261113)
    chunks = sorted({r["meta"]["chunk_id"] for r in rows if r["meta"].get("source") == "paper_qa"})
    rng.shuffle(chunks)
    dev_chunks = set(chunks[: max(1, n_dev // 2)])       # 每条证据约 1-2 题
    dev = [r for r in rows if r["meta"].get("chunk_id") in dev_chunks]
    train = [r for r in rows if r["meta"].get("chunk_id") not in dev_chunks]

    # dev 里补若干拒答样本（拒答能力是这轮最想验证的）
    deny = [r for r in rows if r["meta"].get("source") == "deny"]
    dev += deny[:8]
    train = [r for r in train if r["meta"].get("source") != "deny"] + deny[8:]

    def dump(path: Path, data: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in data) + "\n",
                        encoding="utf-8")
        print(f"{path.name}: {len(data)} 条")

    dump(DATA / "train.jsonl", train)
    dump(DATA / "dev.jsonl", dev)

    split_trans()


def split_trans(n_dev: int = 8) -> None:
    """翻译样本按「首段原文」分组切分：同段落的不同批次只能落在一侧。

    trans.jsonl 的 meta 只有 source，没有 chunk_id，所以用首段正文做分组键；
    只要两条样本引用了同一段原文，就一定同侧，避免"训练见过这段、dev 只换批次"。
    """
    src = DATA / "trans.jsonl"
    if not src.exists():
        print("[skip] 没有 trans.jsonl")
        return
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        try:
            first = json.loads(r["messages"][1]["content"])[0]
        except Exception:
            first = r["messages"][1]["content"]
        key = re.sub(r"\s+", " ", str(first))[:80]
        groups.setdefault(key, []).append(r)

    keys = sorted(groups)
    rng = random.Random(20261114)
    rng.shuffle(keys)
    n_group = max(1, round(len(keys) * n_dev / max(len(rows), 1)))
    dev_keys = set(keys[:n_group])
    dev = [r for k in dev_keys for r in groups[k]]
    train = [r for k in keys if k not in dev_keys for r in groups[k]]
    for name, data in (("trans_train.jsonl", train), ("trans_dev.jsonl", dev)):
        path = DATA / name
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in data) + "\n",
                        encoding="utf-8")
        print(f"{name}: {len(data)} 条")


if __name__ == "__main__":
    main()
