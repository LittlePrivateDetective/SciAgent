"""把库里的论文变成微调数据（SFT）。

思路：**不凭空造数据，让强模型在"真实证据"上产出问答对，再用可验证的规则筛掉不合格的。**
每条样本都由三部分组成，与线上推理时的输入格式完全一致：

    system   = SYSTEM_CONTRACT（与 app/agents/prompts.py 同源）
    user     = 【证据】[1] p12 · 章节 · 正文 … 【问题】…
    assistant= 带 [编号] 引用的中文答案

三条硬约束（不满足就丢，宁缺毋滥）：

1. 答案必须出现 [n] 引用，且编号不超过证据条数；
2. 答案里的数字要能在证据里找到（防止模型自己算一个填上——这是线上最严重的错法）；
3. 答案不能是元话语（"根据提供的证据…""受证据限制…"），也不能是"可能/大概"。

用法（在**应用 venv** 里跑，要用到 app 的解析与提示词；训练本身在 ml/.venv）：
    .venv/Scripts/python.exe ml/prepare_sft.py --n 20 --out data/sft/qa.jsonl   # 冒烟
    .venv/Scripts/python.exe ml/prepare_sft.py --n 800                          # 正式
    .venv/Scripts/python.exe ml/prepare_sft.py --deny 80                        # 拒答样本
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.prompts import SYSTEM_CONTRACT  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.llm import LLMError, gateway  # noqa: E402
from app.rag.bilingual import detect_lang  # noqa: E402
from app.rag.chunker import strip_carry  # noqa: E402

GEN_MODEL = "qwen3.8-max"          # 造数据用强模型；不参与后面被评测的对比
OUT = ROOT / "data" / "sft"

GEN_SYS = """你是科研问答数据构造器。给定若干段真实论文证据，请针对**其中一段**造一道
"必须靠这段证据才能答"的问题，并写出符合规范的答案。

严格输出 JSON：
{"item": {"ev": 1, "q": "...", "a": "..."}}

规则：
1. q 是中文问题，具体、可核查（问方法/数值/条件/结论，不要问"这篇论文讲了什么"）；
2. a 用简体中文，第一句直接给结论；引用写成 [1]/[2]，编号是证据序号，最多 3 条；
3. a 里出现的数字、单位必须与证据原文一致，不得换算或四舍五入；
4. a 不得出现"根据提供的证据""受证据限制""可能/大概/或许"这类措辞；
5. 长度 60–260 字，不要复制原文整段。"""

RE_CITE = re.compile(r"\[\d{1,2}\]")
RE_NUM = re.compile(r"\d+(?:\.\d+)?%?")
RE_META = re.compile(r"根据(提供的)?证据|受证据限制|由于篇幅|综上所?述|可能|大概|或许|应该")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")


def load_chunks(max_per_doc: int = 40) -> list[dict]:
    con = sqlite3.connect(ROOT / "data" / "sciagent.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id, doc_id, page, idx, kind, text, section_path from chunks "
        "where kind='text' order by doc_id, page, idx"
    ).fetchall()
    titles = {r["id"]: r["title"] for r in con.execute("select id, title from documents")}
    con.close()

    out: list[dict] = []
    prev_by_doc: dict[str, str] = {}
    count: dict[str, int] = {}
    for r in rows:
        src = strip_carry(prev_by_doc.get(r["doc_id"], ""), r["text"] or "").strip()
        prev_by_doc[r["doc_id"]] = r["text"] or ""
        if not src or len(src) < 260 or len(src) > 1100:
            continue
        if detect_lang(src) != "en":        # 先用英文论文造中文问答；中文那篇留作中译英
            continue
        if count.get(r["doc_id"], 0) >= max_per_doc:
            continue
        letters = sum(1 for ch in src if ch.isalpha())
        if letters / len(src) < 0.62:       # 公式/表格碎片不要
            continue
        count[r["doc_id"]] = count.get(r["doc_id"], 0) + 1
        out.append({
            "chunk_id": r["id"], "doc_id": r["doc_id"], "page": r["page"],
            "section": (r["section_path"] or "").split(">")[-1].strip(),
            "title": (titles.get(r["doc_id"]) or "")[:60], "text": src,
        })
    return out


def evidence_block(items: list[dict]) -> str:
    """按线上 ANSWER_PROMPT 的形状拼证据，但只带 1 条主证据 + 2 条同页邻证。"""
    lines = []
    for i, it in enumerate(items, 1):
        head = f"[{i}] p{it['page']}"
        if it["section"]:
            head += f" · {it['section'][:40]}"
        lines.append(f"{head}\n{it['text'][:900]}")
    return "\n\n".join(lines)


def valid(q: str, a: str, ev_items: list[dict]) -> tuple[bool, str]:
    if not (20 <= len(q) <= 120):
        return False, "问题长度"
    if len(RE_CJK.findall(q)) < 8:
        return False, "问题不是中文"
    if not (50 <= len(a) <= 400):
        return False, "答案长度"
    cites = RE_CITE.findall(a)
    if not cites:
        return False, "无引用"
    if any(int(c.strip("[]")) > len(ev_items) for c in cites):
        return False, "引用越界"
    if RE_META.search(a):
        return False, "元话语/含糊"
    ev_text = " ".join(it["text"] for it in ev_items)
    nums = set(RE_NUM.findall(ev_text))
    for n in set(RE_NUM.findall(a)):
        if n not in nums:
            return False, f"数字不在证据里：{n}"
    return True, ""


def gen_batch(batch: list[dict], rng: random.Random, retries: int = 2) -> list[dict]:
    """一次生成一批：每条主证据配 2 条同页邻证，让模型自己挑一条出题。"""
    samples = []
    items = []
    for c in batch:
        ev = [c]
        items.append(ev)
    prompt_input = []
    for i, ev in enumerate(items, 1):
        prompt_input.append(f"### 证据组 {i}\n{evidence_block(ev)}")
    user = "\n\n".join(prompt_input) + (
        "\n\n请对每一组各造一道题，输出 JSON 数组："
        '{"items":[{"group":1,"q":"...","a":"..."}, ...]}，组数必须与输入一致。'
    )
    msgs = [{"role": "system", "content": GEN_SYS}, {"role": "user", "content": user}]
    for attempt in range(retries + 1):
        try:
            obj = gateway.chat_json(msgs, temperature=0.6, max_tokens=4000, model=GEN_MODEL)
            arr = obj.get("items") or obj.get("data") or []
            if isinstance(arr, list) and len(arr) == len(batch):
                for it, ev in zip(arr, items):
                    q = str(it.get("q") or "").strip()
                    a = str(it.get("a") or "").strip()
                    ok, why = valid(q, a, ev)
                    if ok:
                        samples.append({
                            "messages": [
                                {"role": "system", "content": SYSTEM_CONTRACT.format(role="科研写作助手")},
                                {"role": "user", "content": f"【证据】\n{evidence_block(ev)}\n\n【问题】\n{q}"},
                                {"role": "assistant", "content": a},
                            ],
                            "meta": {"chunk_id": ev[0]["chunk_id"], "doc": ev[0]["title"],
                                     "page": ev[0]["page"], "source": "paper_qa"},
                        })
                return samples
        except LLMError as exc:
            print(f"  [warn] {exc}", flush=True)
        time.sleep(1.0 + attempt)
    return samples


def gen_deny(n: int, chunks: list[dict], rng: random.Random) -> list[dict]:
    """拒答样本：证据里确实没有答案时，要明确说"未提及"，而不是编。

    用模板而不是模型生成——这类样本的价值在"措辞一致"，模板比模型稳。
    """
    topics = [
        "这篇论文在 ImageNet 上的 top-1 准确率是多少？",
        "作者用的优化器学习率衰减策略具体是余弦退火还是线性衰减？",
        "这篇论文的训练集包含多少种语言？",
        "作者有没有报告在 TPU v4 上的吞吐数据？",
        "论文里提到的推理延迟（毫秒）是多少？",
        "作者是否比较过 MoE 与稠密模型在长文本任务上的差异？",
        "实验用的 batch size 和梯度累积步数分别是多少？",
        "这篇论文有没有给出人工评测的标注者一致性系数？",
    ]
    out = []
    for i in range(n):
        c = rng.choice(chunks)
        ev = [c]
        q = topics[i % len(topics)]
        a = "文献中未提及。[1] 只给出了方法描述与实验设置，没有提供该问题的数据或结论。"
        out.append({
            "messages": [
                {"role": "system", "content": SYSTEM_CONTRACT.format(role="科研写作助手")},
                {"role": "user", "content": f"【证据】\n{evidence_block(ev)}\n\n【问题】\n{q}"},
                {"role": "assistant", "content": a},
            ],
            "meta": {"chunk_id": c["chunk_id"], "doc": c["title"], "page": c["page"],
                     "source": "deny"},
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800, help="问答样本数")
    ap.add_argument("--deny", type=int, default=0, help="额外生成多少条拒答样本")
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--out", default=str(OUT / "qa.jsonl"))
    ap.add_argument("--seed", type=int, default=20261113)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    chunks = load_chunks()
    rng.shuffle(chunks)
    print(f"可用正文块 {len(chunks)} 个（{len({c['doc_id'] for c in chunks})} 篇）", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done, kept = 0, 0
    t0 = time.time()
    with out_path.open("w", encoding="utf-8") as f:
        for i in range(0, min(args.n * 2, len(chunks)), args.batch):
            if kept >= args.n:
                break
            batch = chunks[i : i + args.batch]
            samples = gen_batch(batch, rng)
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
            kept += len(samples)
            done += len(batch)
            print(f"  {done} 块 → {kept} 条（{time.time() - t0:.0f}s）", flush=True)

        if args.deny:
            for s in gen_deny(args.deny, chunks, rng):
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
            kept += args.deny

    dur = time.time() - t0
    print(f"完成：{kept} 条 → {out_path}（{dur:.0f}s，{kept / max(dur, 1) * 60:.1f} 条/分钟）")


if __name__ == "__main__":
    main()
