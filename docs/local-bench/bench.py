"""本地 vs 云端模型基准：40 组固定题，三路模型，流式测量。

测的不是"谁更聪明"，而是三个工程问题：

1. **快不快**——首字延迟（TTFT）决定交互手感，总耗时 × 组数决定批量任务能否跑完；
2. **能不能用**——JSON 类任务能不能解析、翻译有没有漏成英文、会不会复读；
3. **顶不顶得住**——6GB 显存的消费卡跑 3B 模型时的显存峰值、prompt 变长后的退化。

用法：
    python bench.py --models local,cloud-fast          # 常用组合
    python bench.py --models all --limit 5             # 冒烟
    python bench.py --models local --repeat 2          # 重复跑，看稳定性
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
# 项目根目录：优先环境变量 SCIAGENT_HOME，否则按"本目录在 docs/local-bench 下"推断。
# 不写死盘符，仓库换台机器 clone 下来也能跑。
PROJ = Path(os.environ.get("SCIAGENT_HOME") or ROOT.parents[1])
sys.path.insert(0, str(ROOT))
from prompts import all_cases  # noqa: E402

MODELS = {
    "local": {
        "kind": "ollama",
        "model": "qwen2.5:3b",
        "label": "本地 qwen2.5:3b（RTX 2060 6GB）",
        "price_in": 0.0, "price_out": 0.0,
    },
    "cloud-fast": {
        "kind": "openai",
        "model": "qwen-flash",
        "label": "云端 qwen-flash",
        # 阿里云百炼 qwen-flash 公开价：输入 0.15 元/百万 token，输出 1.5 元/百万
        "price_in": 0.15 / 1_000_000, "price_out": 1.5 / 1_000_000,
    },
    "cloud-main": {
        "kind": "openai",
        "model": "deepseek-v4.1-flash",
        "label": "云端 deepseek-v4.1-flash（带思维链）",
        "price_in": 2.0 / 1_000_000, "price_out": 8.0 / 1_000_000,
    },
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LAT = re.compile(r"[A-Za-z]")


# ------------------------------------------------------------------ 配置
def load_env() -> dict:
    """只读项目的 .env，不在测试目录里复制任何密钥。"""
    env: dict[str, str] = {}
    f = PROJ / ".env"
    if not f.exists():
        return env
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


# ------------------------------------------------------------------ 显存采样
class GpuSampler(threading.Thread):
    """跑本地模型时后台每 0.5s 记一次显存，取峰值与均值。

    nvidia-smi 单次调用约 80ms，采样间隔放 0.5s 不会明显干扰推理。
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[int] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip().splitlines()
                if out:
                    self.samples.append(int(out[0]))
            except Exception:
                pass
            self._stop.wait(0.5)

    def stop(self) -> dict:
        self._stop.set()
        self.join(timeout=3)
        if not self.samples:
            return {}
        return {
            "samples": len(self.samples),
            "peak_mib": max(self.samples),
            "mean_mib": round(sum(self.samples) / len(self.samples)),
            "min_mib": min(self.samples),
        }


# ------------------------------------------------------------------ 单次调用
def call_ollama(prompt: str, max_tokens: int, base: str) -> dict:
    payload = {
        "model": MODELS["local"]["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }
    t0 = time.perf_counter()
    ttft = None
    chunks: list[str] = []
    final: dict = {}
    with httpx.stream("POST", base.rstrip("/") + "/api/chat", json=payload, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            piece = (d.get("message") or {}).get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
                chunks.append(piece)
            if d.get("done"):
                final = d
    total = (time.perf_counter() - t0) * 1000
    text = "".join(chunks)
    out_tok = int(final.get("eval_count") or 0)
    in_tok = int(final.get("prompt_eval_count") or 0)
    eval_ms = (final.get("eval_duration") or 0) / 1e6
    return {
        "text": text,
        "ttft_ms": round(ttft or total, 1),
        "total_ms": round(total, 1),
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "tok_s": round(out_tok / (eval_ms / 1000), 1) if eval_ms > 0 and out_tok else 0.0,
        "load_ms": round((final.get("load_duration") or 0) / 1e6, 1),
        "tok_est": False,
    }


def _est_tokens(text: str) -> int:
    """没有 usage 时按"中文 1 字≈1 token、英文 3.5 字符≈1 token"粗估。"""
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 3.5)


def call_openai(prompt: str, max_tokens: int, env: dict, model: str) -> dict:
    base = env.get("LLM_BASE_URL", "").rstrip("/")
    key = env.get("LLM_API_KEY", "")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    chunks: list[str] = []
    usage: dict = {}
    with httpx.stream("POST", base + "/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {key}"}, timeout=300) as r:
        if r.status_code >= 400:
            body = r.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"HTTP {r.status_code}: {body}")
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            d = json.loads(raw)
            if d.get("usage"):
                usage = d["usage"]
            delta = (d.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
                chunks.append(piece)
    total = (time.perf_counter() - t0) * 1000
    text = "".join(chunks)
    out_tok = int(usage.get("completion_tokens") or 0)
    in_tok = int(usage.get("prompt_tokens") or 0)
    est = not out_tok
    if est:
        out_tok, in_tok = _est_tokens(text), _est_tokens(prompt)
    gen_s = (total - (ttft or total)) / 1000
    return {
        "text": text,
        "ttft_ms": round(ttft or total, 1),
        "total_ms": round(total, 1),
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "tok_s": round(out_tok / gen_s, 1) if gen_s > 0.2 and out_tok else 0.0,
        "load_ms": 0.0,
        "tok_est": est,
    }


# ------------------------------------------------------------------ 质量判据
def strip_fence(t: str) -> str:
    s = t.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def quality(case: dict, text: str) -> dict:
    t = (text or "").strip()
    cjk = len(_CJK.findall(t))
    lat = len(_LAT.findall(t))
    total = max(1, cjk + lat)
    out = {
        "chars": len(t),
        "cjk_ratio": round(cjk / total, 3),
        "empty": len(t) == 0,
        "repeat_ratio": round(_repeat_ratio(t), 3),
    }
    if case["expect"] == "json":
        ok = False
        try:
            json.loads(strip_fence(t))
            ok = True
        except Exception:
            ok = False
        out["json_ok"] = ok
    elif case["expect"] == "zh":
        out["lang_ok"] = cjk / total >= 0.25 and lat / total <= 0.62
    elif case["expect"] == "en":
        out["lang_ok"] = cjk / total <= 0.03 and lat / total >= 0.55
    else:
        out["lang_ok"] = None
    return out


def _repeat_ratio(text: str, n: int = 12) -> float:
    """最长重复片段的占比：3B 模型在长输出上容易进入复读，这个值会明显升高。

    短文本不算：100 字的答案里出现两次 12-gram 是正常措辞，不是退化。
    """
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 200:
        return 0.0
    grams = [s[i:i + n] for i in range(0, len(s) - n + 1, n)]
    if not grams:
        return 0.0
    from collections import Counter
    top = Counter(grams).most_common(1)[0][1]
    return min(1.0, top * n / len(s))



def summarize(rows: list[dict], models: list[str]) -> dict:
    """按模型汇总。报告与图表都引用这里算出来的数，避免两处口径不一致。"""
    out = {}
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        if not rs:
            continue
        ttfts = [r["ttft_ms"] for r in rs]
        totals = [r["total_ms"] for r in rs]
        toks = [r["tok_s"] for r in rs if r["tok_s"] > 0]
        json_rows = [r for r in rs if r["cat"] == "json"]
        lang_rows = [r for r in rs if r.get("lang_ok") is not None]
        out[m] = {
            "label": MODELS[m]["label"],
            "n": len(rs),
            "ttft_mean_ms": round(statistics.mean(ttfts)),
            "ttft_p50_ms": round(statistics.median(ttfts)),
            "ttft_p90_ms": round(sorted(ttfts)[max(0, int(len(ttfts) * 0.9) - 1)]),
            "total_mean_ms": round(statistics.mean(totals)),
            "tok_s_mean": round(statistics.mean(toks), 1) if toks else 0,
            "empty": sum(1 for r in rs if r["empty"]),
            "json_ok": sum(1 for r in json_rows if r.get("json_ok")),
            "json_total": len(json_rows),
            "lang_ok": sum(1 for r in lang_rows if r.get("lang_ok")),
            "lang_total": len(lang_rows),
            "repeat_mean": round(statistics.mean(r["repeat_ratio"] for r in rs), 3),
            "out_tokens_total": sum(r["out_tokens"] for r in rs),
            "cost_cny": round(sum(r["cost_cny"] for r in rs), 6),
        }
    return out


# ------------------------------------------------------------------ 主流程
def run(models: list[str], limit: int | None, repeat: int, only_cat: str | None) -> dict:
    env = load_env()
    cases = all_cases()
    if only_cat:
        cases = [c for c in cases if c["cat"] == only_cat]
    if limit:
        cases = cases[:limit]

    result = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "cases_total": len(cases),
        "repeat": repeat,
        "models": {m: MODELS[m] for m in models},
        "gpu": {},
        "rows": [],
        "errors": [],
    }

    sampler = GpuSampler()
    if "local" in models:
        sampler.start()

    for m in models:
        cfg = MODELS[m]
        for r in range(repeat):
            for case in cases:
                # 思维链模型需要额外额度：把推理 token 也算进预算，否则会被截成空串。
                # 这不是偏袒——项目里 llm_max_tokens 本来就是 4096，这里只是按比例缩。
                budget = case["max_tokens"] + (600 if m == "cloud-main" else 0)
                t0 = time.perf_counter()
                try:
                    if cfg["kind"] == "ollama":
                        got = call_ollama(case["prompt"], budget, "http://127.0.0.1:11434")
                    else:
                        got = call_openai(case["prompt"], budget, env, cfg["model"])
                except Exception as exc:  # noqa: BLE001 单组失败不能中断整轮
                    result["errors"].append({"model": m, "case": case["id"], "error": str(exc)[:300]})
                    continue
                q = quality(case, got["text"])
                row = {
                    "model": m,
                    "model_label": cfg["label"],
                    "case": case["id"],
                    "cat": case["cat"],
                    "round": r + 1,
                    "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
                    **{k: v for k, v in got.items() if k != "text"},
                    **q,
                    "cost_cny": round(
                        got["in_tokens"] * cfg["price_in"] + got["out_tokens"] * cfg["price_out"], 8),
                    "answer": got["text"][:600],
                }
                result["rows"].append(row)
                flag = "!" if q["empty"] else ("J" if q.get("json_ok") is True else " ")
                print(f"  {m:11s} {case['id']} {case['cat']:6s} "
                      f"ttft={row['ttft_ms']:7.0f}ms total={row['total_ms']:7.0f}ms "
                      f"out={row['out_tokens']:4d}tok {row['tok_s']:5.1f}t/s {flag}", flush=True)

    if "local" in models:
        result["gpu"] = sampler.stop()

    # 按模型汇总，报告直接引用这些数
    result["summary"] = summarize(result["rows"], models)

    path = ROOT / "results" / f"bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    result["file"] = str(path)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="local,cloud-fast")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--cat", default=None, help="只跑某一类：qa/en2zh/zh2en/json/long")
    a = ap.parse_args()
    picked = list(MODELS) if a.models == "all" else [x.strip() for x in a.models.split(",")]
    for m in picked:
        if m not in MODELS:
            sys.exit(f"未知模型 {m}，可选 {list(MODELS)}")
    print(f"模型 {picked} / {a.limit or '全部'} 组 / 重复 {a.repeat} 轮")
    r = run(picked, a.limit, a.repeat, a.cat)
    print("\n=== 汇总 ===")
    print(json.dumps(r["summary"], ensure_ascii=False, indent=1))
    if r.get("gpu"):
        print("GPU:", r["gpu"])
    if r["errors"]:
        print("失败组:", len(r["errors"]), r["errors"][:2])
    print("结果文件:", r["file"])
