"""把生成的问答样本切成 train / dev：dev 只用于"微调前后"对比，绝不进训练。

按 chunk 切而不是按行切——同一段证据生成的两条题会一起进 dev，
否则 dev 里会出现"训练时见过这段证据、只是换了个问法"的假提升。
"""
from __future__ import annotations

import json
import random
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


if __name__ == "__main__":
    main()
