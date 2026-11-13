"""把 LoRA adapter 合并进底座，产出可独立加载的 fp16 safetensors 目录。

为什么单独一个脚本（而不是在 train_lora.py 里顺手 merge）：
- 训练时模型是 4bit 量化的，`merge_and_unload()` 合并到量化模型上会保留量化布局，
  导出的权重既不能直接转 GGUF，也不好被别人加载；
- 合并必须在 CPU 上以 fp16 进行：3B fp16 约 6.2GB，6GB 显存合并不下，
  但训练结束后系统内存足够（实测 15.9GB 机器可跑）。

用法（ml/.venv）：
    ml/.venv/Scripts/python.exe ml/merge_lora.py \
        --lora data/sft/out/lora-3b --out data/sft/out/merged-3b
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = ROOT / "ml" / "models" / "Qwen2.5-3B-Instruct"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=str(DEFAULT_BASE))
    ap.add_argument("--lora", required=True, help="train_lora.py 的输出目录")
    ap.add_argument("--out", required=True, help="合并后的 fp16 safetensors 目录")
    ap.add_argument("--max-shard", default="2GB")
    args = ap.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    lora = Path(args.lora)
    if not (lora / "adapter_config.json").exists():
        raise SystemExit(f"不是 LoRA 目录（缺 adapter_config.json）：{lora}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"底座 {args.base}")
    print(f"adapter {lora}")
    tok = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)

    # 关键：fp16 + CPU + low_cpu_mem_usage，避免 6GB 显存被合并过程打爆
    model = AutoModelForCausalLM.from_pretrained(
        args.base, torch_dtype=torch.float16, device_map="cpu", low_cpu_mem_usage=True,
    )
    model = PeftModel.from_pretrained(model, str(lora))
    model = model.merge_and_unload()
    model.eval()

    model.save_pretrained(str(out), safe_serialization=True, max_shard_size=args.max_shard)
    tok.save_pretrained(str(out))

    total = sum(p.numel() for p in model.parameters())
    shards = sorted(p.name for p in out.glob("*.safetensors"))
    size_gb = sum(p.stat().st_size for p in out.glob("*.safetensors")) / 2**30
    summary = {
        "base": str(args.base),
        "lora": str(lora),
        "out": str(out),
        "params": total,
        "dtype": "float16",
        "shards": shards,
        "weights_gb": round(size_gb, 2),
    }
    (out / "merge_info.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"合并完成 → {out}")


if __name__ == "__main__":
    main()
