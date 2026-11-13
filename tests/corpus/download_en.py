"""扩充测试集下载脚本（测试前置工作，不修改被测项目）。

只下载到 tests/corpus/，不入库、不改配置。
"""
import json
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
# 20 篇英文（含 _incoming 已有的 RAG / RETRO，共 20）
ITEMS = [
    ("1409.1556", "Show and Tell: A Neural Image Caption Generator", 2014, "多模态/图注"),
    ("1409.3215", "Neural Machine Translation by Jointly Learning to Align and Translate", 2014, "注意力前身"),
    ("1511.06434", "Pointer Networks", 2015, "指针网络/经典老文"),
    ("1810.04805", "BERT: Pre-training of Deep Bidirectional Transformers", 2018, "预训练/双栏"),
    ("1909.11942", "ALBERT: A Lite BERT for Self-supervised Learning", 2019, "预训练"),
    ("2002.08909", "REALM: Retrieval-Augmented Language Model Pre-Training", 2020, "RAG"),
    ("2004.12832", "Dense Passage Retrieval for Open-Domain QA", 2020, "RAG"),
    ("2104.07567", "Leveraging Passage Retrieval with Generative Models (FiD)", 2021, "RAG"),
    ("2204.05862", "Training Compute-Optimal Large Language Models (Chinchilla)", 2022, "扩展律"),
    ("2210.03629", "ReAct: Synergizing Reasoning and Acting in Language Models", 2022, "Agent"),
    ("2302.04761", "Toolformer: Language Models Can Teach Themselves to Use Tools", 2023, "工具调用"),
    ("2305.10601", "Tree of Thoughts: Deliberate Problem Solving with LLMs", 2023, "Agent"),
    ("2307.08691", "FlashAttention-2: Faster Attention with Better Parallelism", 2023, "效率优化"),
    ("2309.17453", "Mistral 7B", 2023, "开源 LLM"),
    ("2310.03744", "Qwen-VL: A Frontier Large Vision-Language Model", 2023, "多模态"),
    ("2312.00752", "Mixtral of Experts", 2024, "MoE"),
    ("2402.19473", "DoRA: Weight-Decomposed Low-Rank Adaptation", 2024, "参数高效微调"),
    ("2404.14219", "Phi-3 Technical Report", 2024, "小模型"),
]
BASES = ["https://arxiv.org/pdf/{}.pdf", "https://export.arxiv.org/pdf/{}.pdf"]


def main() -> None:
    log = []
    cli = httpx.Client(timeout=120, follow_redirects=True)
    for aid, title, year, tag in ITEMS:
        dst = OUT / f"EN_{aid}_{title.split(':')[0][:36].replace(' ', '_')}.pdf"
        rec = {"arxiv": aid, "title": title, "year": year, "topic": tag, "file": dst.name}
        if dst.exists() and dst.stat().st_size > 50000:
            rec.update(status="ok_cached", bytes=dst.stat().st_size)
            log.append(rec)
            print("cached", aid, flush=True)
            continue
        ok = False
        for base in BASES:
            try:
                r = cli.get(base.format(aid))
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    dst.write_bytes(r.content)
                    rec.update(status="ok", bytes=len(r.content), source=base.format(aid))
                    ok = True
                    print("ok", aid, len(r.content), flush=True)
                    break
                rec["last_error"] = f"{base}: {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                rec["last_error"] = str(exc)[:120]
        if not ok:
            rec["status"] = "failed"
            print("FAILED", aid, rec.get("last_error"), flush=True)
        log.append(rec)
        time.sleep(1.2)
    (OUT / "download_en.json").write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print("done", sum(1 for r in log if r["status"].startswith("ok")), "/", len(log))


if __name__ == "__main__":
    main()
