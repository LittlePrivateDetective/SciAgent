"""微调前后对比：同一个 dev 集，base 3B / 微调 3B（可选：云端大模型）各跑一遍。

不请裁判打分，只用**可判定的硬指标**——这些指标在项目里都已经被别处定义过，
不另立一套口径：

| 指标 | 判据 | 为什么重要 |
| --- | --- | --- |
| 引用率 | 答案里出现 `[n]` | 线上要求"每句结论都能回溯到证据" |
| 引用越界 | `[n]` 的 n 大于证据条数 | 编号幻觉，用户点不到 |
| 数字保真 | 答案里的数字都能在证据里找到 | 编数值比含糊更严重（ANSWER_PROMPT 里写死的红线） |
| 元话语率 | 出现"根据提供的证据/受证据限制" | 线上会直接判失败 |
| 拒答正确率 | deny 样本里说"未提及"且不编造 | 防幻觉的最后一道 |
| 速度 | tok/s | 本地部署能不能用 |

用法（在 ml/.venv 里跑）：
    ml/.venv/Scripts/python.exe ml/eval_ft.py --lora data/sft/out/lora-3b
    ml/.venv/Scripts/python.exe ml/eval_ft.py --lora ... --cloud   # 加一列云端大模型
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "sft"

RE_CITE = re.compile(r"\[\d{1,2}\]")
RE_NUM = re.compile(r"\d+(?:\.\d+)?%?")
RE_META = re.compile(r"根据(提供的)?证据|受证据限制|由于篇幅|综上所?述|作为(一个)?(AI|语言模型)")


def load_dev(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def evidence_of(sample: dict) -> tuple[str, int]:
    """从 user 消息里取出证据正文与证据条数（[n] 的最大合法值）。"""
    user = sample["messages"][1]["content"]
    body = user.split("【问题】")[0]
    n = len(re.findall(r"^\[\d+\] ", body, flags=re.M)) or 1
    return body, n


def score(answer: str, ev_body: str, ev_n: int, is_deny: bool) -> dict:
    a = (answer or "").strip()
    cites = [int(c.strip("[]")) for c in RE_CITE.findall(a)]
    nums_ev = set(RE_NUM.findall(ev_body.replace(" ", "")))
    nums_a = set(RE_NUM.findall(a.replace(" ", "")))
    bad_nums = [x for x in nums_a if x not in nums_ev]
    return {
        "empty": not a,
        "has_cite": bool(cites),
        "cite_out": any(n > ev_n for n in cites),
        "bad_nums": bad_nums,
        "meta": bool(RE_META.search(a)),
        "chars": len(a),
        "deny_ok": (("未提及" in a) and not cites) if is_deny else None,
    }


def gen(model, tok, messages: list[dict], max_new: int = 320) -> tuple[str, float]:
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
    if not n:
        return {}
    deny = [r for r in rows if r["deny"]]
    return {
        "model": tag, "n": n,
        "引用率": round(sum(r["has_cite"] for r in rows) / n, 3),
        "引用越界率": round(sum(r["cite_out"] for r in rows) / n, 3),
        "数字出错率": round(sum(1 for r in rows if r["bad_nums"]) / n, 3),
        "元话语率": round(sum(r["meta"] for r in rows) / n, 3),
        "空答案率": round(sum(r["empty"] for r in rows) / n, 3),
        "平均字数": round(sum(r["chars"] for r in rows) / n),
        "拒答正确率": round(sum(r["deny_ok"] for r in deny) / len(deny), 3) if deny else None,
        "字/秒": round(sum(r["speed"] for r in rows) / n, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=str(DATA / "dev.jsonl"))
    ap.add_argument("--base", default=str(ROOT / "ml" / "models" / "Qwen2.5-3B-Instruct"))
    ap.add_argument("--lora", default="", help="LoRA 权重目录；留空只评 base")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-new", type=int, default=320)
    ap.add_argument("--out", default=str(DATA / "eval_result.json"))
    args = ap.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    rows = load_dev(Path(args.dev))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit(f"没有 dev 数据：{args.dev}")

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16)
    tok = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def run(tag: str, model) -> list[dict]:
        out = []
        for i, s in enumerate(rows, 1):
            ev_body, ev_n = evidence_of(s)
            text, speed = gen(model, tok, s["messages"][:-1], args.max_new)
            r = score(text, ev_body, ev_n, s["deny"])
            r["speed"] = speed
            r["q"] = s["messages"][1]["content"].split("【问题】")[-1].strip()[:60]
            r["a"] = text
            out.append(r)
            print(f"  [{tag}] {i}/{len(rows)} {r['chars']}字 {speed:.0f}字/s", flush=True)
        return out

    results = {}
    base = AutoModelForCausalLM.from_pretrained(args.base, quantization_config=bnb,
                                                device_map={"": 0}, torch_dtype=torch.float16)
    results["base"] = run("base", base)
    if args.lora:
        ft = PeftModel.from_pretrained(base, args.lora)
        results["ft"] = run("ft", ft)

    table = [summarize(v, k) for k, v in results.items()]
    payload = {"table": table, "rows": results}
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n| 模型 | " + " | ".join(k for k in table[0] if k != "model") + " |")
    for t in table:
        print(f"| {t['model']} | " + " | ".join(str(t[k]) for k in t if k != "model") + " |")
    print(f"\n明细 → {args.out}")


if __name__ == "__main__":
    main()
