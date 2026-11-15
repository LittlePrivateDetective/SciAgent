"""20 题真实评测：跑 /api/chat/stream，拆出各阶段耗时并给失败归因。

关注点（用户优先级）：
1. 速度——首字延迟（TTFT）、检索耗时、生成耗时、总耗时；
2. 质量——按"单跳 / 多跳 / 对比 / 选题 / 换说法 / 跨语言"六类看失败分布。

用法：
    python scripts/bench_chat.py                # 全跑，落盘 data/bench_chat.json
    python scripts/bench_chat.py --q 5          # 只跑第 5 题（调试用）
    python scripts/bench_chat.py --mode fast    # 指定 mode
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
OUT = ROOT / "data" / "bench_chat.json"

# (编号, 类型, 问题, 期望命中的关键点——只用于人工复核时对照，不参与打分)
QUESTIONS: list[tuple[str, str, str, str]] = [
    ("A1", "单跳", "BERT 的预训练用了哪两个任务？", "Masked LM / NSP"),
    ("A2", "单跳", "LoRA 论文里秩 r 取多少时效果最好？", "r=1 或 4/8，说明小秩即可"),
    ("A3", "单跳", "Attention 里缩放点积为什么要除以根号 dk？", "点积变大导致 softmax 梯度小"),
    ("A4", "单跳", "T5 的预训练目标是什么？", "span corruption / 填空"),
    ("B1", "多跳", "Llama 3 的训练数据规模比 T5 大约多多少倍？", "15T vs 1T，约 15 倍"),
    ("B2", "多跳", "GPT-3 和 Llama 3 在 few-shot 上的结论有什么不同？", "需跨两篇"),
    ("B3", "多跳", "哪篇最早用双向编码器做预训练，它和 GPT 的单向做法差在哪？", "BERT vs GPT-1"),
    ("B4", "多跳", "QLoRA 的显存节省主要来自哪两处？各占多少？", "4bit NormalFloat + 双重量化/分页优化器"),
    ("C1", "对比", "这几篇对 scaling law 的结论有什么不一致？", "Chinchilla vs Kaplan 关于数据/参数配比"),
    ("C2", "对比", "LoRA 和 QLoRA 在“是否损失精度”上的说法有冲突吗？", "需对比两篇结论"),
    ("C3", "对比", "关于 RLHF 的必要性，DPO 和 InstructGPT 的立场差异是什么？", "DPO 主张免 RL"),
    ("D1", "选题", "我想做中文长文档的检索问答，这几篇里哪几篇最值得先读？", "需给出多篇+理由"),
    ("D2", "选题", "只有一张 6GB 显卡做微调，应该参考哪篇？", "QLoRA / LoRA"),
    ("D3", "选题", "想做翻译质量评测，哪篇给了可复用的方法？", "Whisper / 评测章节"),
    ("E1", "换说法", "那个把权重更新拆成两个低秩矩阵相乘的方法叫什么？", "LoRA"),
    ("E2", "换说法", "有没有论文讨论过相对位置编码、能处理超长上下文？", "Transformer-XL / RWKV"),
    ("E3", "换说法", "哪篇讲了把一句话里的词随机遮住再让模型猜？", "BERT MLM"),
    ("F1", "跨语言", "BERT 论文里 next sentence prediction 具体怎么构造正负样本？", "英文原文 50% 配对"),
    ("F2", "跨语言", "Llama 3 的 safety 数据是怎么处理的？", "英文原文 safety"),
    ("F3", "跨语言", "计算机学报那篇新闻推荐论文用了什么增量学习方法？", "中文文献，知识回放/最优传输"),
]

REFUSAL_HINTS = ("未提及", "没有提到", "无法回答", "没有检索到", "文献中没有", "证据不足")


def run_one(idx: int, kind: str, q: str, expect: str, mode: str = "qa") -> dict:
    payload = {"question": q, "mode": mode, "no_cache": True}
    t0 = time.time()
    first_token: float | None = None
    retrieve_done: float | None = None
    retrieve_ms: int | None = None
    hits = 0
    prof: dict = {}
    answer_parts: list[str] = []
    done_meta: dict = {}
    err = ""

    with httpx.Client(timeout=300.0, trust_env=False) as c:
        with c.stream("POST", f"{BASE}/api/chat/stream", json=payload) as r:
            if r.status_code != 200:
                return {"idx": idx, "kind": kind, "q": q, "error": f"HTTP {r.status_code}"}
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                name = ev.get("event")
                now = time.time() - t0
                if name == "thinking":
                    if ev.get("stage") == "retrieve" and ev.get("state") == "done":
                        retrieve_done = now
                        retrieve_ms = ev.get("latency_ms")
                        hits = ev.get("hits") or 0
                        prof = ev.get("profile") or {}
                elif name == "token":
                    if first_token is None:
                        first_token = now
                    answer_parts.append(ev.get("text") or "")
                elif name == "error":
                    err = ev.get("text") or "error"
                elif name == "done":
                    done_meta = ev
                    break

    answer = "".join(answer_parts).strip() or (done_meta.get("answer") or "")
    total = time.time() - t0
    ttft = first_token if first_token is not None else total
    gen = total - (retrieve_done or 0)
    cited = len({p for p in __import__("re").findall(r"\[(\d{1,2})\]", answer)})
    refusal = any(h in answer for h in REFUSAL_HINTS)
    return {
        "idx": idx, "kind": kind, "q": q, "expect": expect,
        "ttft_s": round(ttft, 2),
        "retrieve_s": round(retrieve_done, 2) if retrieve_done else None,
        "retrieve_ms": retrieve_ms,
        "gen_s": round(gen, 2),
        "total_s": round(total, 2),
        "hits": hits or len(done_meta.get("evidence") or []),
        "chars": len(answer),
        "cites": cited,
        "profile": prof,
        "refusal": refusal,
        "error": err,
        "answer": answer[:1200],
    }


def main() -> None:
    only = None
    mode = "qa"
    for i, a in enumerate(sys.argv):
        if a == "--q" and i + 1 < len(sys.argv):
            only = int(sys.argv[i + 1])
        if a == "--mode" and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1]

    rows: list[dict] = []
    # 预热：首问要加载检索索引/模型，不能计入统计
    try:
        run_one(0, "预热", "TensorFlow 是什么？", "", mode=mode)
    except Exception as exc:  # noqa: BLE001
        print(f"预热失败：{exc}")

    for i, (idx, kind, q, expect) in enumerate(QUESTIONS, start=1):
        if only and i != only:
            continue
        r = run_one(i, kind, q, expect, mode=mode)
        rows.append(r)
        print(f"[{i:2d}/{len(QUESTIONS)}] {idx} {kind:4s} "
              f"TTFT {r.get('ttft_s')}s 检索 {r.get('retrieve_ms')}ms 生成 {r.get('gen_s')}s "
              f"总 {r.get('total_s')}s 命中 {r.get('hits')} "
              f"拆解 {r.get('profile')} "
              f"{'!! ' + r['error'] if r.get('error') else ''}")

    if rows:
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = [r for r in rows if not r.get("refusal") and not r.get("error")]
        speeds = sorted(r["total_s"] for r in rows)
        ttf = sorted(r["ttft_s"] for r in rows)
        print(f"\n完成 {len(rows)} 题 → {OUT}")
        print(f"总耗时 中位 {speeds[len(speeds)//2]:.2f}s / 最慢 {speeds[-1]:.2f}s")
        print(f"首字   中位 {ttf[len(ttf)//2]:.2f}s / 最慢 {ttf[-1]:.2f}s")
        print(f"有实质回答 {len(ok)}/{len(rows)}")


if __name__ == "__main__":
    main()
