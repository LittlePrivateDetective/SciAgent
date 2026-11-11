"""并发能力对比：本地单卡 vs 云端 API。

为什么单独测：单条延迟只说明"一个人用得快不快"，而真实场景里常是
几个人同时提问，或者后台在翻译整篇论文的同时前台还在问答。这时本地单卡
会和云端拉开明显差距——显存只有 6GB，第二个请求要么排队，要么把模型挤到内存里。

指标：
    墙钟时间    10 个请求全部完成用了多久（并发度 4）
    单请求延迟  各自的耗时（看排队有多严重）
    总吞吐      所有请求产出 token 之和 / 墙钟时间
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bench import MODELS, call_ollama, call_openai, load_env  # noqa: E402
from prompts import all_cases  # noqa: E402


def one(cfg: dict, env: dict, prompt: str, max_tokens: int) -> dict:
    t0 = time.perf_counter()
    try:
        if cfg["kind"] == "ollama":
            got = call_ollama(prompt, max_tokens, "http://127.0.0.1:11434")
        else:
            got = call_openai(prompt, max_tokens, env, cfg["model"])
        return {"ok": True, "wall_ms": (time.perf_counter() - t0) * 1000, **got}
    except Exception as exc:  # noqa: BLE001 并发下失败要计入，而不是让整轮崩掉
        return {"ok": False, "error": str(exc)[:200], "wall_ms": (time.perf_counter() - t0) * 1000,
                "out_tokens": 0, "tok_s": 0, "text": ""}


def run(models: list[str], workers: int, n: int) -> dict:
    env = load_env()
    cases = [c for c in all_cases() if c["cat"] == "qa"][:n]
    out = {"run_at": datetime.now().isoformat(timespec="seconds"),
           "workers": workers, "requests": len(cases), "models": {}}

    for m in models:
        cfg = MODELS[m]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, cfg, env, c["prompt"], c["max_tokens"]) for c in cases]
            results = [f.result() for f in futs]
        wall = time.perf_counter() - t0

        oks = [r for r in results if r["ok"]]
        tokens = sum(r.get("out_tokens", 0) for r in oks)
        walls = sorted(r["wall_ms"] for r in results)
        out["models"][m] = {
            "label": cfg["label"],
            "ok": len(oks),
            "failed": len(results) - len(oks),
            "wall_s": round(wall, 2),
            "wall_baseline_s": None,
            "req_p50_ms": round(walls[len(walls) // 2]),
            "req_max_ms": round(walls[-1]),
            "out_tokens": tokens,
            "throughput_tok_s": round(tokens / wall, 1) if wall > 0 else 0,
            "errors": [r["error"] for r in results if not r["ok"]][:3],
        }
        print(f"  {m:11s} ok={len(oks)}/{len(results)} wall={wall:5.1f}s "
              f"p50={walls[len(walls)//2]:6.0f}ms max={walls[-1]:6.0f}ms "
              f"throughput={out['models'][m]['throughput_tok_s']:.1f} tok/s", flush=True)

    # 串行基线：同样的请求一个个跑，用来看并发到底带来多少收益
    for m in models:
        cfg = MODELS[m]
        t0 = time.perf_counter()
        oks = 0
        for c in cases:
            r = one(cfg, env, c["prompt"], c["max_tokens"])
            oks += 1 if r["ok"] else 0
        wall = time.perf_counter() - t0
        out["models"][m]["wall_baseline_s"] = round(wall, 2)
        out["models"][m]["speedup"] = round(wall / out["models"][m]["wall_s"], 2) \
            if out["models"][m]["wall_s"] else 0
        print(f"  {m:11s} 串行基线 {wall:5.1f}s → 并发加速 {out['models'][m]['speedup']}×", flush=True)

    path = ROOT / "results" / f"concurrency_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    out["file"] = str(path)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="local,cloud-fast")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args()
    picked = list(MODELS) if a.models == "all" else [x.strip() for x in a.models.split(",")]
    print(f"并发 {a.workers} × {a.n} 个请求")
    r = run(picked, a.workers, a.n)
    print(json.dumps(r["models"], ensure_ascii=False, indent=1))
    print("结果文件:", r["file"])
