"""翻译能力对比：base 3B vs 微调 3B（同一批 dev 翻译题）。

指标全部可判定，不请模型当裁判：
- JSON 可解析率：输出能否 parse 成 {"out": [...]}
- 条数匹配率：out 长度 == 输入段落数
- 数字保真率：原文数字全部出现在译文中
- 中文占比：译文里 CJK 字符 / 总字符（英文残留会拉低）
- 英文残留率：连续英文单词占比 > 35% 视为残留
- 速度：字/秒

用法（ml/.venv）：
    ml/.venv/Scripts/python.exe ml/eval_trans_ft.py --lora data/sft/out/lora-3b-v2
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "sft"

RE_NUM = re.compile(r"\d+(?:[.,]\d+)*%?")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_EN = re.compile(r"[A-Za-z]{2,}")


def load(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_out(text: str) -> list[str] | None:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        d = json.loads(t)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", t)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except Exception:
            return None
    out = d.get("out") if isinstance(d, dict) else None
    return out if isinstance(out, list) else None


def score(text: str, srcs: list[str]) -> dict:
    out = parse_out(text)
    joined = "".join(str(x) for x in out) if out else (text or "")
    joined_ns = re.sub(r"\s+", "", joined)
    src_ns = re.sub(r"\s+", "", "".join(srcs))
    nums_src = RE_NUM.findall(src_ns)
    nums_hit = sum(1 for n in nums_src if n in joined_ns)
    cjk = len(RE_CJK.findall(joined))
    en = len(RE_EN.findall(joined))
    total = max(len(joined_ns), 1)
    return {
        "json_ok": out is not None,
        "count_ok": bool(out is not None and len(out) == len(srcs)),
        "num_ok": (nums_hit == len(nums_src)) if nums_src else True,
        "nums_src": len(nums_src),
        "nums_hit": nums_hit,
        "cjk_ratio": round(cjk / total, 3),
        "en_residue": bool(en / max(cjk + en, 1) > 0.35),
        "chars": len(joined),
    }


def gen(model, tok, messages: list[dict], max_new: int = 600) -> tuple[str, float]:
    import torch
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ids = tok(prompt, return_tensors="pt").to(model.device)
    t0 = time.time()
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new, do_sample=False,
                             pad_token_id=tok.pad_token_id or tok.eos_token_id)
    dt = time.time() - t0
    text = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
    return text.strip(), len(text) / max(dt, 1e-6)


def summarize(rows: list[dict], tag: str) -> dict:
    n = len(rows)
    return {
        "model": tag, "n": n,
        "JSON可解析率": round(sum(r["json_ok"] for r in rows) / n, 3),
        "条数匹配率": round(sum(r["count_ok"] for r in rows) / n, 3),
        "数字保真率": round(sum(r["num_ok"] for r in rows) / n, 3),
        "中文占比": round(sum(r["cjk_ratio"] for r in rows) / n, 3),
        "英文残留率": round(sum(r["en_residue"] for r in rows) / n, 3),
        "平均字数": round(sum(r["chars"] for r in rows) / n),
        "字/秒": round(sum(r["speed"] for r in rows) / n, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=str(DATA / "trans_dev.jsonl"))
    ap.add_argument("--base", default=str(ROOT / "ml" / "models" / "Qwen2.5-3B-Instruct"))
    ap.add_argument("--lora", default="", help="LoRA 目录；留空只评 base")
    ap.add_argument("--max-new", type=int, default=600)
    ap.add_argument("--out", default=str(DATA / "eval_trans.json"))
    args = ap.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    rows = load(Path(args.dev))
    if not rows:
        raise SystemExit(f"没有翻译 dev 数据：{args.dev}")

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16)
    tok = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def run(tag: str, fn) -> list[dict]:
        out = []
        for i, s in enumerate(rows, 1):
            srcs = json.loads(s["messages"][1]["content"])
            text, speed = fn(s["messages"][:-1])
            r = score(text, srcs)
            r["speed"] = speed
            r["src"] = srcs[0][:60]
            r["a"] = text
            out.append(r)
            print(f"  [{tag}] {i}/{len(rows)} json={r['json_ok']} count={r['count_ok']} "
                  f"num={r['nums_hit']}/{r['nums_src']} {r['chars']}字", flush=True)
        return out

    results = {}
    base = AutoModelForCausalLM.from_pretrained(args.base, quantization_config=bnb,
                                                device_map={"": 0}, torch_dtype=torch.float16)
    results["base"] = run("base", lambda m: gen(base, tok, m, args.max_new))
    if args.lora:
        ft = PeftModel.from_pretrained(base, args.lora)
        results["ft"] = run("ft", lambda m: gen(ft, tok, m, args.max_new))

    table = [summarize(v, k) for k, v in results.items()]
    Path(args.out).write_text(json.dumps({"table": table, "rows": results}, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    print("\n| 模型 | " + " | ".join(k for k in table[0] if k != "model") + " |")
    for t in table:
        print(f"| {t['model']} | " + " | ".join(str(t[k]) for k in t if k != "model") + " |")
    print(f"\n明细 → {args.out}")


if __name__ == "__main__":
    main()
