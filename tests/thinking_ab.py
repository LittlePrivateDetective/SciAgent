"""DeepSeek 开思考 / 关思考的对照测评（纯模型层，固定证据）。

为什么要绕开 /api/chat：那个接口里混了检索、重排、校验、反思多个节点，耗时与
答案都被别的东西影响。这里把证据**先冻结**下来，两边喂同一份 prompt，只切
`enable_thinking`，测的才是"思维链值不值那几秒"。

五个维度，每题都要求 JSON 输出（与线上 ANSWER_PROMPT 一致），顺带测结构化能力：
- A 事实抽取：答案在证据里明写
- B 数值/超参：必须原样抄数，最容易看出编造
- C 跨文献对比：需要把两份证据的差异组织起来
- D 证据不足：论文里根本没有这些信息，正确行为是拒答而不是编
- E 结构化输出：给定 schema 的 JSON

自动判定只用可机器核验的量（引用编号是否越界、数字是否都在证据里、是否明确
拒答、JSON 能否解析），不做"看起来好不好"的主观打分——那种分数换个评委就变。

    ./.venv/Scripts/python.exe tests/thinking_ab.py            # 跑全部 30 题 × 2 模式
    ./.venv/Scripts/python.exe tests/thinking_ab.py --limit 3  # 冒烟
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "qa" / "evidence"
MODEL = "deepseek-v4.1-flash"

# (编号, 类别, 问题, 输出模板)
# qa = 线上 ANSWER_PROMPT 口径；json = 指定字段的结构化任务
CASES: list[tuple[str, str, str, str]] = [
    # ---------- A 事实抽取 ----------
    ("A1", "fact", "Transformer 的编码器和解码器各由多少层相同的层堆叠而成？", "qa"),
    ("A2", "fact", "BERT 使用了哪两个无监督预训练任务？", "qa"),
    ("A3", "fact", "Mistral 7B 的上下文窗口长度是多少？", "qa"),
    ("A4", "fact", "LLaMA-65B 训练时使用了多少张 GPU、训练了多长时间？", "qa"),
    ("A5", "fact", "ELECTRA 的判别器要预测什么？", "qa"),
    ("A6", "fact", "DeepSeek-V3 的总参数量和激活参数量分别是多少？", "qa"),
    ("A7", "fact", "Qwen2.5 支持多少种语言？", "qa"),
    ("A8", "fact", "GPT 论文中使用的优化器是什么，学习率如何调度？", "qa"),
    # ---------- B 数值与超参 ----------
    ("B1", "number", "Transformer base 模型的 d_model、注意力头数 h、层数分别是多少？", "qa"),
    ("B2", "number", "Mistral 7B 的滑动窗口大小 W 是多少？", "qa"),
    ("B3", "number", "BERT-base 的层数、隐藏层维度、注意力头数分别是多少？", "qa"),
    ("B4", "number", "LLaMA 2 的最大上下文长度是多少？", "qa"),
    ("B5", "number", "GPT-3 使用了多少训练 token？", "qa"),
    ("B6", "number", "Transformer 训练时 warmup 步数是多少？", "qa"),
    # ---------- C 跨文献对比 ----------
    ("C1", "compare", "BERT 和 GPT 的预训练目标有什么不同？", "qa"),
    ("C2", "compare", "ELECTRA 与 BERT 在训练效率上的差异是什么？", "qa"),
    ("C3", "compare", "Mistral 7B 与 LLaMA 2 13B 在基准测试上的表现差异？", "qa"),
    ("C4", "compare", "LLaMA 2 相比 LLaMA 1 在上下文长度和训练数据上有哪些变化？", "qa"),
    ("C5", "compare", "Qwen2.5 与 LLaMA 3 在训练数据规模或多语言能力上的差别？", "qa"),
    # ---------- D 证据不足（正确答案是拒答）----------
    ("D1", "refuse", "Attention Is All You Need 这篇论文的第一作者邮箱是什么？", "qa"),
    ("D2", "refuse", "BERT 论文的作者们目前分别在哪个机构任职？", "qa"),
    ("D3", "refuse", "Mistral 7B 训练总共用了多少张 A100 显卡？", "qa"),
    ("D4", "refuse", "LLaMA 论文的致谢部分感谢了哪些具体的人？", "qa"),
    ("D5", "refuse", "DeepSeek-V3 的商业授权费用是多少？", "qa"),
    # ---------- E 结构化输出 ----------
    ("E1", "struct", "用 JSON 输出这篇论文的 {title, year, venue}，字段未知就留空串。", "json1"),
    ("E2", "struct", "用 JSON 输出对比结果：{\"aspects\":[{\"name\":\"预训练目标\",\"bert\":\"…\",\"gpt\":\"…\"}]}", "json2"),
    ("E3", "struct", "用 JSON 输出 {\"claims\":[{\"text\":\"…\",\"evidence\":[编号]}]}，claims 给 3 条。", "json3"),
    ("E4", "struct", "用 JSON 输出 {\"method\":\"…\",\"dataset\":\"…\",\"metric\":\"…\",\"value\":\"…\"}", "json4"),
    ("E5", "struct", "用 JSON 输出 {\"missing\":[\"…\"]}：说明要完整回答还缺哪些信息，没有就空数组。", "json5"),
    ("E6", "struct", "用 JSON 输出 {\"steps\":[{\"step\":1,\"action\":\"…\",\"input\":\"…\"}]}，最多 4 步。", "json6"),
]

TPL = {
    "qa": None,   # 用线上 ANSWER_PROMPT
    "json1": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
    "json2": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
    "json3": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
    "json4": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
    "json5": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
    "json6": """【证据】\n{evidence}\n\n【任务】\n{question}\n\n只输出一个 JSON 对象，不要解释、不要代码块围栏。""",
}

NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\d)")
CITE = re.compile(r"\[(\d+(?:\s*[,，、\-]\s*\d+)*)\]")
REFUSE_HINT = ("未提及", "未给出", "未说明", "未提供", "无法确定", "没有提到",
               "证据中不包含", "不存在", "未公开")


def load_evidence(only: list[str] | None = None) -> dict:
    """冻结证据：同一份 prompt 喂给两个模式，排除检索波动。"""
    from app.rag.hybrid import search_with_rerank

    cache_path = OUT / "thinking_ab_evidence.json"
    out = {}
    if cache_path.exists():
        out = json.loads(cache_path.read_text(encoding="utf-8"))

    for cid, cat, q, _tpl in CASES:
        if only is not None and cid not in only:
            continue
        if cid in out:
            continue
        try:
            hits = search_with_rerank(q, top_k=8)
        except Exception as exc:  # noqa: BLE001
            print("检索失败", cid, exc)
            hits = []
        out[cid] = {
            "question": q,
            "cat": cat,
            "items": [
                {"doc": h.doc_name, "page": h.page, "text": h.text[:900], "kind": h.kind}
                for h in hits
            ],
        }
        print(f"  证据 {cid}: {len(hits)} 条")
    OUT.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def evidence_block(items: list[dict]) -> tuple[str, str]:
    """返回 (给模型看的证据块, 用于校验的原文拼接)。"""
    lines, raw = [], []
    for i, it in enumerate(items, 1):
        lines.append(f"[{i}] 来源：{it['doc']} 第{it['page']}页 | {it['text']}")
        raw.append(it["text"])
    return "\n".join(lines) if lines else "（无检索结果）", "\n".join(raw)


def call_api(messages: list[dict], thinking: bool, key: str, base: str) -> dict:
    body = {"model": MODEL, "messages": messages, "temperature": 0.2,
            "max_tokens": 4096, "stream": False}
    if not thinking:
        body["enable_thinking"] = False
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200], "elapsed_ms": int((time.time() - t) * 1000)}
    el = int((time.time() - t) * 1000)
    ch = (d.get("choices") or [{}])[0].get("message") or {}
    u = d.get("usage") or {}
    detail = u.get("completion_tokens_details") or {}
    return {
        "ok": True, "elapsed_ms": el,
        "text": ch.get("content") or "",
        "reasoning_chars": len(ch.get("reasoning_content") or ""),
        "in_tokens": u.get("prompt_tokens") or 0,
        "out_tokens": u.get("completion_tokens") or 0,
        "reasoning_tokens": detail.get("reasoning_tokens") or 0,
    }


def extract_answer(text: str) -> tuple[str, dict | None]:
    """尽量从返回里抠出正文与 JSON。模型偶尔会包 ```json 围栏。"""
    body = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if m:
        body = m.group(1).strip()
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        s, e = body.find("{"), body.rfind("}")
        obj = None
        if s >= 0 and e > s:
            try:
                obj = json.loads(body[s:e + 1])
            except Exception:  # noqa: BLE001
                obj = None
    if isinstance(obj, dict):
        ans = obj.get("answer") or obj.get("text") or obj.get("claims") or obj.get("steps") or ""
        if not isinstance(ans, str):
            ans = json.dumps(ans, ensure_ascii=False)
        return ans, obj
    return body, None


def score(case: tuple, res: dict, raw_evidence: str, n_ev: int) -> dict:
    cid, cat, q, tpl = case
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}

    text = res["text"]
    ans, obj = extract_answer(text)
    if cat == "struct" and obj is not None:
        # 结构化题没有 answer 字段，整段 JSON 就是它的产出
        ans = json.dumps(obj, ensure_ascii=False)
    # 引用编号是否越界：越界说明它在指向不存在的证据
    refs = []
    for grp in CITE.findall(ans):
        for part in re.split(r"[,，、]", grp):
            part = part.strip()
            if "-" in part:
                a, _, b = part.partition("-")
                if a.strip().isdigit() and b.strip().isdigit():
                    refs.extend(range(int(a), int(b) + 1))
            elif part.isdigit():
                refs.append(int(part))
    bad_refs = [r for r in refs if r < 1 or r > n_ev]
    # 数字幻觉：答案里的数字若不在证据原文中，标记出来
    ev_nums = set()
    for m in NUM.finditer(raw_evidence):
        try:
            ev_nums.add(round(float(m.group()), 6))
        except ValueError:
            pass
    bad_nums = []
    for m in NUM.finditer(ans):
        try:
            v = round(float(m.group()), 6)
        except ValueError:
            continue
        if v in ev_nums:
            continue
        if 1900 <= v <= 2100 or v <= 10:      # 年份、编号、枚举序号不算
            continue
        bad_nums.append(m.group())
    refused = any(h in ans for h in REFUSE_HINT)
    return {
        "ok": True,
        "json_ok": obj is not None,
        "answer_chars": len(ans),
        "refs": len(set(refs)),
        "bad_refs": len(set(bad_refs)),
        "bad_nums": len(bad_nums),
        "bad_num_samples": bad_nums[:5],
        "refused": refused,
        "elapsed_ms": res["elapsed_ms"],
        "in_tokens": res["in_tokens"],
        "out_tokens": res["out_tokens"],
        "reasoning_tokens": res["reasoning_tokens"],
        "answer": ans[:2000],
    }


def run(limit: int = 0) -> dict:
    from pathlib import Path as P

    from app.agents.prompts import ANSWER_PROMPT, SYSTEM_CONTRACT

    env = {}
    for line in P(".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key, base = env["LLM_API_KEY"], env.get("LLM_BASE_URL", "")
    if not base:
        raise SystemExit("缺少 LLM_BASE_URL")

    cases = CASES[:limit] if limit else CASES
    ev = load_evidence([c[0] for c in cases])
    rows = []
    for cid, cat, q, tpl in cases:
        items = ev[cid]["items"]
        block, raw = evidence_block(items)
        user = (ANSWER_PROMPT.format(evidence=block, question=q) if tpl == "qa"
                else TPL[tpl].format(evidence=block, question=q))
        msgs = [{"role": "system", "content": SYSTEM_CONTRACT.format(role="科研写作助手")},
                {"role": "user", "content": user}]
        for thinking in (True, False):
            res = call_api(msgs, thinking, key, base)
            sc = score((cid, cat, q, tpl), res, raw, len(items))
            sc.update({"case": cid, "cat": cat, "thinking": thinking, "q": q})
            rows.append(sc)
            flag = "✓" if sc.get("ok") else "✗"
            print(f"  {flag} {cid:3s} think={int(thinking)} {sc.get('elapsed_ms',0):6d}ms "
                  f"out={sc.get('out_tokens',0):4d} ans={sc.get('answer_chars',0):4d} "
                  f"badref={sc.get('bad_refs','-')} badnum={sc.get('bad_nums','-')}")
            time.sleep(0.3)

    result = {"model": MODEL, "cases": len(cases), "rows": rows,
              "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "thinking_ab_result.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    result["file"] = str(path)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    r = run(args.limit)
    print(f"\n写出 {r['file']}，共 {len(r['rows'])} 条记录")
