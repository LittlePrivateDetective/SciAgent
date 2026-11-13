"""Qwen2.5-3B-Instruct 的 QLoRA 微调（单卡 6GB 显存可跑）。

为什么是 QLoRA 而不是全参：
RTX 2060 只有 6GB。3B 全参 fp16 光权重就 6.2GB，加梯度/优化器要 25GB+；
4bit 量化后权重约 2GB，LoRA 只训 20M 量级参数，激活靠梯度检查点压到 1.5GB 左右，
总占用 4.5GB 上下——刚好卡在能跑与 OOM 之间，所以 batch 只能是 1 + 累计 8。

关键取舍（答辩要能讲）：
1. **只对 assistant 段算 loss**：把 system/证据 部分 mask 成 -100，否则模型会去学"复述证据"；
2. **fp16 而不是 bf16**：Turing（2060）不支持 bf16，硬上会得到 NaN loss；
3. **max_len 1024**：证据块长，但 1280 就开始 OOM；超出部分从**证据中间**截断而不是尾部，
   因为答案通常落在证据末尾。

用法（在 ml/.venv 里跑，需要 torch/peft/bitsandbytes）：
    ml/.venv/Scripts/python.exe ml/train_lora.py --dry-run          # 只检查数据与显存
    ml/.venv/Scripts/python.exe ml/train_lora.py --max-steps 20     # 冒烟
    ml/.venv/Scripts/python.exe ml/train_lora.py                    # 正式（约 4–6 小时）
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "sft"
OUT = DATA / "out"
BASE = "Qwen/Qwen2.5-3B-Instruct"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--data", nargs="+", default=[str(DATA / "qa.jsonl"), str(DATA / "trans.jsonl")])
    ap.add_argument("--out", default=str(OUT / "lora-3b"))
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--save-steps", type=int, default=50, help="每多少步存一次 adapter")
    ap.add_argument("--log-steps", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = []
    for p in args.data:
        path = Path(p)
        if path.exists():
            part = load_jsonl(path)
            rows += part
            print(f"  {path.name}: {len(part)} 条")
        else:
            print(f"  [skip] 没有 {path}")
    if not rows:
        raise SystemExit("没有训练数据：先跑 ml/prepare_sft.py")
    print(f"合计 {len(rows)} 条")

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                              Trainer, TrainingArguments)

    if not torch.cuda.is_available():
        raise SystemExit("CUDA 不可用：QLoRA 必须有 GPU")
    free, total = torch.cuda.mem_get_info()
    print(f"GPU {torch.cuda.get_device_name(0)} 空闲 {free/2**30:.1f}GB / 共 {total/2**30:.1f}GB")

    tok = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def encode(sample: dict) -> dict:
        msgs = sample["messages"]
        prompt = tok.apply_chat_template(msgs[:-1], tokenize=False, add_generation_prompt=True)
        answer = msgs[-1]["content"] + tok.eos_token
        p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
        a_ids = tok(answer, add_special_tokens=False)["input_ids"]
        ids = p_ids + a_ids
        labels = [-100] * len(p_ids) + a_ids
        # 超长时从提示中部截断：保留开头（角色/证据头）和结尾（问题）
        if len(ids) > args.max_len:
            over = len(ids) - args.max_len
            cut = min(over, max(0, len(p_ids) // 2))
            ids = p_ids[: len(p_ids) // 2 - cut] + p_ids[len(p_ids) // 2 :] + a_ids
            labels = labels[: len(p_ids) // 2 - cut] + labels[len(p_ids) // 2 :]
            ids, labels = ids[: args.max_len], labels[: args.max_len]
        return {"input_ids": ids, "labels": labels, "attention_mask": [1] * len(ids)}

    ds = Dataset.from_list([encode(r) for r in rows])
    print(f"编码完成：{len(ds)} 条，平均 {sum(len(x) for x in ds['input_ids']) / len(ds):.0f} token")
    if args.dry_run:
        longest = max(len(x) for x in ds["input_ids"])
        print(f"最长 {longest} token；样例：{tok.decode(ds[0]['input_ids'][-40:])!r}")
        return

    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base, quantization_config=bnb, device_map={"": 0}, torch_dtype=torch.float16,
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    lora = LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    def collate(batch: list[dict]) -> dict:
        maxlen = max(len(b["input_ids"]) for b in batch)
        pad = tok.pad_token_id
        input_ids, labels, attn = [], [], []
        for b in batch:
            n = maxlen - len(b["input_ids"])
            input_ids.append(b["input_ids"] + [pad] * n)
            labels.append(b["labels"] + [-100] * n)
            attn.append(b["attention_mask"] + [0] * n)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "attention_mask": torch.tensor(attn, dtype=torch.long),
        }

    Path(args.out).mkdir(parents=True, exist_ok=True)
    targs = TrainingArguments(
        output_dir=args.out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=args.log_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=2,
        max_steps=args.max_steps,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        fp16=True,                      # 2060 不支持 bf16
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=0,
    )
    trainer = Trainer(model=model, args=targs, train_dataset=ds, data_collator=collate)
    trainer.train()

    # 训练日志单独落一份 JSON：Trainer 的 loss 只打在进度条上（重定向到文件时
    # 会被 \\r 覆盖得看不见），而"loss 有没有降"是判断这轮训练有没有意义的第一依据，
    # 不能只靠翻日志。这一段也顺带解决了冒烟时 trainer_state.json 缺失的问题
    # ——保存过程中断时，至少 loss 曲线还在。
    hist = [h for h in trainer.state.log_history if "loss" in h]
    log_path = Path(args.out) / "train_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(trainer.state.log_history, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    if hist:
        first = sum(h["loss"] for h in hist[:3]) / min(3, len(hist))
        last = sum(h["loss"] for h in hist[-3:]) / min(3, len(hist))
        print(f"loss 首 {first:.4f} → 末 {last:.4f}（{len(hist)} 个记录点，明细 {log_path}）")

    trainer.save_model(args.out)
    tok.save_pretrained(args.out)
    print(f"LoRA 已保存 → {args.out}")


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
