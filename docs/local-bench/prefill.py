"""纯 prefill 测试：固定输出长度，只让输入变长。

为什么单独做：40 组基准里的"总耗时"混了两件事——读输入（prefill）和写输出
（decode）。不同题目的答案长短差三倍，把总耗时画成"输入长度"的函数会得出
反直觉的结论（图上一度显示本地斜率比云端还低）。

这里把输出锁死（num_predict=16），只观察首字延迟随输入增长怎么涨：

    输入 512 → 1024 → 2048 → 4096 → 8192 token，同一句问题

这一条曲线才是"本地模型能扛多长上下文"的直接答案，也是显存到底够不够的判据。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
# 同 bench.py：不写死盘符，换机器 clone 下来也能跑
PROJ = Path(os.environ.get("SCIAGENT_HOME") or ROOT.parents[1])
LEVELS = [512, 1024, 2048, 4096, 8192]
FILLER = (
    "检索增强生成把外部知识引入生成模型以缓解幻觉。流水线包含解析、分块、向量化、索引、"
    "检索、重排与生成七个阶段。分块需要在上下文完整性与检索精度之间权衡。"
    "稀疏检索在长尾实体上更稳，稠密检索对同义改写更敏感，混合检索通常能带来稳定增益。"
)


def build(n_tokens: int, tag: str) -> str:
    """按目标长度拼材料。开头带唯一 tag，避免 Ollama 复用 KV cache 把 prefill 省掉。"""
    # 中文字符≈1 token，粗略按 1:1 拼，再截断到目标长度附近
    body = (FILLER * (n_tokens // len(FILLER) + 2))[:n_tokens]
    return f"【材料 {tag}】\n{body}\n\n问题：这份材料的主题是什么？只回答一个词。"


def call_ollama(prompt: str, num_ctx: int) -> dict:
    payload = {
        "model": "qwen2.5:3b",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "options": {"num_predict": 16, "temperature": 0, "num_ctx": num_ctx},
    }
    t0 = time.perf_counter()
    ttft = None
    final: dict = {}
    text = ""
    with httpx.stream("POST", "http://127.0.0.1:11434/api/chat", json=payload, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            piece = (d.get("message") or {}).get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
                text += piece
            if d.get("done"):
                final = d
    return {
        "ttft_ms": round(ttft or (time.perf_counter() - t0) * 1000, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "in_tokens": int(final.get("prompt_eval_count") or 0),
        "out_tokens": int(final.get("eval_count") or 0),
        "text": text[:60],
    }


def call_cloud(prompt: str, env: dict) -> dict:
    payload = {
        "model": "qwen-flash",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 16,
        "temperature": 0,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    text = ""
    usage: dict = {}
    with httpx.stream("POST", env["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
                      json=payload, headers={"Authorization": f"Bearer {env['LLM_API_KEY']}"},
                      timeout=600) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.read().decode('utf-8','ignore')[:200]}")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            d = json.loads(raw)
            if d.get("usage"):
                usage = d["usage"]
            piece = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
                text += piece
    return {
        "ttft_ms": round(ttft or (time.perf_counter() - t0) * 1000, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "in_tokens": int(usage.get("prompt_tokens") or 0),
        "out_tokens": int(usage.get("completion_tokens") or 0),
        "text": text[:60],
    }


def load_env() -> dict:
    env = {}
    for line in (PROJ / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


if __name__ == "__main__":
    env = load_env()
    out = {"run_at": datetime.now().isoformat(timespec="seconds"), "levels": LEVELS, "rows": []}
    for lv in LEVELS:
        prompt = build(lv, f"P{lv}")
        for name, fn in (("local", lambda p: call_ollama(p, 16384)),
                         ("cloud-fast", lambda p: call_cloud(p, env))):
            try:
                r = fn(prompt)
            except Exception as exc:  # noqa: BLE001 超长输入可能直接失败，这本身是结论
                r = {"ttft_ms": None, "total_ms": None, "in_tokens": 0, "out_tokens": 0,
                     "text": "", "error": str(exc)[:200]}
            r.update({"model": name, "level": lv, "chars": len(prompt)})
            out["rows"].append(r)
            print(f"  {name:11s} target={lv:5d} in={r['in_tokens']:6d} "
                  f"ttft={r['ttft_ms']}ms total={r['total_ms']}ms "
                  f"{'' if 'error' not in r else 'ERR ' + r['error']}", flush=True)

    path = ROOT / "results" / f"prefill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果文件:", path)
