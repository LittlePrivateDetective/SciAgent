"""翻译质量评测：同一批真实论文章节，跑多个模型，量五类指标。

为什么要专门写一套离线评测，而不是"读两句觉得还行"：

1. 论文翻译的错误是**结构性**的——漏译半句、数字掉一位、把 `[27]` 吞掉、
   把 `W0 + BA` 写成"W0 加上 BA 的秩分解"（意思还在但公式没了）。
   这些用眼睛扫一遍根本发现不了，必须逐项对账。
2. 换模型必须有据。仓库里默认 `TRANS_MODEL=qwen-flash` 是当初"快 3 倍、
   质量相当"的手感结论，没有量化过。这里把"相当"变成一个可以复核的数。

四项客观指标（脚本自己算，不依赖任何模型打分）：

- **协议合规**：段数是否严格等长（`_translate_batch` 的硬要求，错位整批丢弃）
- **数字保真**：原文的数字/百分比有多少出现在译文里
- **引用保真**：`[27]`、`[31, 2, 5]` 这类角标是否保留
- **术语命中**：人工术语表里的核心术语，译文是否用了通行译法
- **英文残留 / 长度比**：漏译与"半中半英"的粗筛

两项模型指标（用不参评的模型当裁判，避免自夸）：

- **回译相似度**：译文再译回英文，与原文的向量余弦（衡量"意思有没有跑")
- **裁判打分**：忠实 / 术语 / 流畅 / 格式 四维 1–5 分 + 错误类型清单

用法：
    python tests/trans_bench.py                 # 全量跑一遍，写 data/trans_eval/
    python tests/trans_bench.py --items 4       # 先跑 4 段冒烟
    python tests/trans_bench.py --audit         # 只体检库里已缓存的译文
    python tests/trans_bench.py --rebuild-fixture

跑完看 `data/trans_eval/report.md`，那里是给人看的结论。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.rag.bilingual import _SYS, _SYS_EN, _clean_zh, keep_verbatim  # noqa: E402

log = get_logger("trans_bench")

FIXTURE = ROOT / "tests" / "fixtures" / "trans_eval.json"
OUTDIR = ROOT / "data" / "trans_eval"

# ---------------------------------------------------------------- 参评系统
# proto:
#   json = 生产协议（system 提示词 + JSON 数组批量进出），与 bilingual._translate_batch 一致
#   mt   = 阿里专用翻译模型：只收 user 消息，靠 translation_options 定方向，一段一次
SYSTEMS: list[dict] = [
    {"id": "qwen-flash", "label": "qwen-flash（当前默认）", "model": "qwen-flash", "proto": "json"},
    {"id": "qwen3.7-flash", "label": "qwen3.7-flash", "model": "qwen3.7-flash", "proto": "json"},
    {"id": "qwen3.8-flash", "label": "qwen3.8-flash", "model": "qwen3.8-flash", "proto": "json"},
    {"id": "qwen-plus", "label": "qwen-plus", "model": "qwen-plus", "proto": "json"},
    {"id": "qwen3.8-max", "label": "qwen3.8-max（旗舰）", "model": "qwen3.8-max", "proto": "json"},
    {"id": "deepseek-v4.1-flash", "label": "deepseek-v4.1-flash", "model": "deepseek-v4.1-flash", "proto": "json"},
    {"id": "deepseek-v4-pro", "label": "deepseek-v4-pro", "model": "deepseek-v4-pro", "proto": "json"},
    # glm 系列强制开思考（关掉直接 400），成本上不划算，但作为"强模型上限"参照有意义
    {"id": "glm-5.3", "label": "glm-5.3（思考开）", "model": "glm-5.3", "proto": "json",
     "no_temp": False, "force_thinking": True},
    # kimi-k3 不接受 temperature 参数
    {"id": "kimi-k3.1-pro", "label": "kimi-k3.1-pro", "model": "kimi-k3.1-pro", "proto": "json",
     "no_temp": True},
    {"id": "qwen-mt-plus", "label": "qwen-mt-plus（专用机翻）", "model": "qwen-mt-plus", "proto": "mt"},
]

# 不参评的两个"裁判"：避免被评模型给自己打分
BT_MODEL = "qwen3.6-plus"
JUDGE_MODEL = "qwen3.7-max"

BATCH = 4          # 与生产 settings.trans_batch 一致
TEMPERATURE = 0.1


# ---------------------------------------------------------------- 数据集
# (文档 id 前缀, 页, 块序号) → 从库里取真实段落，剥掉 overlap 尾巴。
# 覆盖面刻意打散：摘要 / 方法 / 结果 / 图注 / 表注 / 变量密集段 / 引用密集段。
PICKS: list[dict] = [
    {"id": "attn-abs", "doc": "dce63a636cf64", "page": 1, "idx": 1, "kind": "text",
     "note": "摘要首段：术语密集、长句",
     "terms": {"sequence transduction": ["序列转换", "序列转导", "序列到序列"],
               "attention mechanism": ["注意力机制"], "encoder": ["编码器"], "decoder": ["解码器"]}},
    {"id": "attn-ref", "doc": "dce63a636cf64", "page": 1, "idx": 2, "kind": "text",
     "note": "引用密集段：[12] [7] [31, 2, 5] [34, 22, 14]",
     "terms": {"long short-term memory": ["长短期记忆", "长短时记忆"],
               "state of the art": ["最先进", "最优", "领先"], "sequence modeling": ["序列建模"]}},
    {"id": "attn-def", "doc": "dce63a636cf64", "page": 3, "idx": 17, "kind": "text",
     "note": "定义句：query / key-value / weighted sum",
     "terms": {"query": ["查询"], "key-value pairs": ["键值对"], "weighted sum": ["加权和"],
               "compatibility function": ["兼容性函数", "相容函数", "匹配函数"], "vector": ["向量"]}},
    {"id": "attn-mh", "doc": "dce63a636cf64", "page": 4, "idx": 23, "kind": "text",
     "note": "变量密集：dmodel / dk / dv 与投影维度",
     "terms": {"linear projection": ["线性投影"], "queries": ["查询"], "keys": ["键"],
               "values": ["值"], "attention function": ["注意力函数"]}},
    {"id": "ln-abs", "doc": "dc7092cdf7b5c", "page": 1, "idx": 2, "kind": "text",
     "note": "LayerNorm 摘要：batch normalization 对照术语",
     "terms": {"batch normalization": ["批归一化", "批量归一化"],
               "normalize the activities": ["归一化"], "stochastic gradient descent": ["随机梯度下降"],
               "recurrent neural networks": ["循环神经网络"]}},
    {"id": "lora-notation", "doc": "d3706de3b6864", "page": 2, "idx": 12, "kind": "text",
     "note": "符号声明段：Wq/Wk/Wv/Wo、∆W、秩 r、dffn = 4 × dmodel",
     "terms": {"projection matrices": ["投影矩阵"], "pre-trained weight matrix": ["预训练权重矩阵"],
               "rank": ["秩"], "feedforward dimension": ["前馈维度", "前馈层维度"]}},
    {"id": "lora-math", "doc": "d3706de3b6864", "page": 4, "idx": 27, "kind": "text",
     "note": "含符号分解 W0 + ∆W = W0 + BA 与集合写法，最容易译错的一段",
     "terms": {"frozen": ["冻结"], "gradient updates": ["梯度更新"], "trainable parameters": ["可训练参数"],
               "forward pass": ["前向传播", "前向过程"]}},
    {"id": "scal-factors", "doc": "d8821b3e541fa", "page": 3, "idx": 11, "kind": "text",
     "note": "三个规模因子 N / D / C，模型规模术语",
     "terms": {"model parameters": ["模型参数"], "embeddings": ["嵌入"], "dataset": ["数据集"],
               "compute": ["算力", "计算量"]}},
    {"id": "scal-compute", "doc": "d8821b3e541fa", "page": 3, "idx": 16, "kind": "text",
     "note": "固定算力预算下的最优训练策略：长句 + 数字",
     "terms": {"compute budget": ["算力预算", "计算预算"], "convergence": ["收敛"],
               "optimal performance": ["最优性能", "最佳性能"]}},
    {"id": "gpt3-intro", "doc": "d817ac4e56a32", "page": 1, "idx": 1, "kind": "text",
     "note": "GPT-3 引言：pre-training / fine-tuning 术语对照",
     "terms": {"pre-training": ["预训练"], "fine-tuning": ["微调"], "benchmarks": ["基准", "基准测试"],
               "corpus": ["语料", "语料库"]}},
    {"id": "dsr1-mixing", "doc": "d7c300e762602", "page": 2, "idx": 6, "kind": "text",
     "note": "语言混用 / 可读性：抽象名词多，机翻腔最容易露馅",
     "terms": {"readability": ["可读性"], "language mixing": ["语言混用", "语言混杂"],
               "chain-of-thought": ["思维链", "思维链式"], "rule-based": ["基于规则"],
               "reinforcement learning": ["强化学习"]}},
    {"id": "llava-intro", "doc": "d6feedd02fcae", "page": 1, "idx": 4, "kind": "text",
     "note": "多模态：universal interface / end-to-end",
     "terms": {"large language models": ["大语言模型", "大型语言模型"],
               "universal interface": ["通用接口", "统一接口"], "end-to-end": ["端到端"],
               "task instructions": ["任务指令"]}},
    {"id": "udl-caption", "doc": "d98dddd7a5905", "page": 33, "idx": 152, "kind": "figure",
     "note": "图注：ϕ = [ϕ0, ϕ1]、坐标轴说明，考「图 N：」格式与符号保留",
     "terms": {"linear regression": ["线性回归"], "y-intercept": ["y 轴截距", "纵截距", "截距"],
               "slope": ["斜率"], "prediction": ["预测"]}},
    {"id": "lora-table", "doc": "d3706de3b6864", "page": 7, "idx": 53, "kind": "table",
     "note": "表注：指标名 + 置信区间 + 星号脚注",
     "terms": {"adaptation methods": ["适配方法", "自适应方法"], "baselines": ["基线"],
               "trainable parameters": ["可训练参数"], "confidence intervals": ["置信区间"]}},
    # ---- 中译英（官方 1.6.4）----
    {"id": "zh-abs", "doc": "d7a55a36", "page": 1, "idx": 0, "kind": "text", "lang": "zh",
     "between": ["摘要：", "1 引言"], "note": "中文摘要：数字 + 百分点表述",
     "terms": {"检索增强": ["retrieval-augmented", "retrieval augmented", "retrieval augmentation"],
               "重排序": ["re-ranking", "reranking", "re-rank"], "注意力机制": ["attention mechanism"],
               "消融实验": ["ablation"]}},
    {"id": "zh-method", "doc": "d7a55a36", "page": 1, "idx": 0, "kind": "text", "lang": "zh",
     "between": ["2 方法", "3 实验"], "note": "中文方法段：双塔 / 交叉编码器 / 对比学习",
     "terms": {"双塔模型": ["two-tower", "dual-tower", "bi-encoder"], "交叉编码器": ["cross-encoder", "cross encoder"],
               "对比学习": ["contrastive learning"], "温度系数": ["temperature"]}},
    {"id": "zh-exp", "doc": "d7a55a36", "page": 1, "idx": 0, "kind": "text", "lang": "zh",
     "between": ["3 实验", None], "note": "中文实验段：指标 / 基线",
     "terms": {"评价指标": ["metric", "evaluation metric"], "准确率": ["accuracy"],
               "基线": ["baseline"], "平均延迟": ["average latency", "latency"]}},
]


def _strip_carry(prev: str, text: str) -> str:
    from app.rag.chunker import strip_carry
    return strip_carry(prev, text)


def build_fixture() -> dict:
    """从本地库抽段落，剥 overlap，落成可复现的 fixture。"""
    import sqlite3

    db = ROOT / "data" / "sciagent.db"
    con = sqlite3.connect(db)
    items = []
    for pick in PICKS:
        rows = con.execute(
            "select page, idx, kind, text from chunks where doc_id like ? order by page, idx",
            (pick["doc"] + "%",),
        ).fetchall()
        target = None
        prev = ""
        for page, idx, kind, text in rows:
            seg = _strip_carry(prev, text or "")
            prev = text or ""
            if page == pick["page"] and idx == pick["idx"]:
                target = seg
                break
        if not target:
            raise SystemExit(f"取不到段落：{pick['id']} p{pick['page']}.{pick['idx']}")
        src = target.strip()
        if pick.get("between"):
            # 中文那篇整页只有一个块，按小标题切：硬切字符会把「1 引言」粘到摘要尾巴上，
            # 模型会把它当成新的一段 → 段数不匹配 → 整批被生产代码丢弃。
            a, b = pick["between"]
            i = src.find(a)
            if i < 0:
                raise SystemExit(f"{pick['id']} 找不到切点 {a}")
            j = src.find(b, i + len(a)) if b else len(src)
            src = src[i : j if j > 0 else len(src)].strip()
        items.append({
            "id": pick["id"], "doc": pick["doc"], "kind": pick["kind"],
            "lang": pick.get("lang", "en"), "src": src,
            "terms": pick.get("terms", {}), "note": pick["note"],
        })
    doc_titles = {}
    for did, title in con.execute("select id, title from documents"):
        doc_titles[did] = title
    for it in items:
        it["title"] = doc_titles.get(it["doc"], it["doc"])
    con.close()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("fixture 写好：%d 段 → %s", len(items), FIXTURE)
    return {"items": items}


def load_fixture(rebuild: bool = False) -> list[dict]:
    if rebuild or not FIXTURE.exists():
        return build_fixture()["items"]
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]


# ---------------------------------------------------------------- 模型调用
class Ctx:
    """一次评测共享的 HTTP 连接与统计。"""

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=180)
        self.headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        self.base = settings.llm_base_url.rstrip("/")


def _post(ctx: Ctx, payload: dict, retries: int = 2) -> dict:
    last = ""
    for attempt in range(retries + 1):
        try:
            r = ctx.client.post(f"{ctx.base}/chat/completions", headers=ctx.headers, json=payload)
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                # 参数类 400 重试没有意义，直接返回
                raise RuntimeError(last)
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            if attempt == 0 and "HTTP 400" in last:
                break
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise RuntimeError(last)


def _text_of(data: dict) -> str:
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip()


def _parse_json(text: str) -> dict:
    from app.core.llm import parse_json_loose
    return parse_json_loose(text)


def translate_json(ctx: Ctx, sys_obj: dict, texts: list[str], to: str = "zh") -> dict:
    """生产同款协议：system 提示词 + JSON 数组，一次 4 段。

    与 `bilingual._translate_batch` 的差别只有两处，都是为了**量准模型本身**：
    - 输出预算按 2 倍字符给（生产是 1.5 倍），避免把"预算给少了"误算成"模型漏译"；
    - 同批最多重试一次，仍失败才退化为逐段（生产在整页层面也这么兜底）。
      单次 JSON 抖动不该被记成"这个模型协议不合规"。
    """
    chars = sum(len(t) for t in texts)
    budget = min(8000, 600 + int(chars * 2.0)) if to == "en" else min(8000, 600 + int(chars * 1.8))
    payload: dict = {
        "model": sys_obj["model"],
        "messages": [
            {"role": "system", "content": _SYS_EN if to == "en" else _SYS},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ],
        "max_tokens": budget,
        "stream": False,
    }
    if not sys_obj.get("no_temp"):
        payload["temperature"] = TEMPERATURE
    # glm-5.3 只接受 enable_thinking=True；其余模型显式关掉，免得推理吃掉输出预算
    payload["enable_thinking"] = bool(sys_obj.get("force_thinking"))

    t0 = time.time()
    last = ""
    finish = ""
    for attempt in range(2):
        try:
            data = _post(ctx, payload)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "out": [], "ms": int((time.time() - t0) * 1000),
                    "err": str(exc)[:200], "usage": {}}
        finish = (data.get("choices") or [{}])[0].get("finish_reason")
        obj = _parse_json(_text_of(data))
        arr = obj.get("out") or obj.get("zh") or obj.get("data") or obj.get("translations") or []
        if isinstance(arr, dict):
            arr = [arr[k] for k in sorted(arr, key=lambda x: str(x))]
        if isinstance(arr, list) and len(texts) == 1 and len(arr) > 1:
            arr = ["\n".join(str(x) for x in arr)]
        usage = data.get("usage") or {}
        if isinstance(arr, list) and len(arr) == len(texts):
            return {"ok": True, "out": [_clean_zh(x) for x in arr],
                    "ms": int((time.time() - t0) * 1000), "err": "", "fallback": "",
                    "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)}}
        last = f"段数/JSON 异常({finish})：{json.dumps(obj, ensure_ascii=False)[:160]}"

    # 两次都对不上：逐段重试，和生产的整页兜底逻辑一致
    if len(texts) > 1:
        singles, ok_all = [], True
        for t in texts:
            one = translate_json(ctx, sys_obj, [t], to)
            singles += one["out"] if one["ok"] else [""]
            ok_all = ok_all and one["ok"]
        if ok_all:
            return {"ok": True, "out": singles, "ms": int((time.time() - t0) * 1000),
                    "err": "", "fallback": "整批段数不匹配→逐段重试", "usage": {}}
    return {"ok": False, "out": [], "ms": int((time.time() - t0) * 1000), "err": last, "usage": {}}


def translate_mt(ctx: Ctx, sys_obj: dict, texts: list[str], to: str = "zh") -> dict:
    """专用翻译模型：一段一次，靠 translation_options 定方向，输出纯文本。"""
    t0 = time.time()
    outs, tin, tout = [], 0, 0
    src_lang, tgt_lang = ("Chinese", "English") if to == "en" else ("English", "Chinese")
    for t in texts:
        payload = {
            "model": sys_obj["model"],
            "messages": [{"role": "user", "content": t}],
            "stream": False,
            "translation_options": {"source_lang": src_lang, "target_lang": tgt_lang},
        }
        try:
            data = _post(ctx, payload)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "out": [], "ms": int((time.time() - t0) * 1000), "err": str(exc)[:200], "usage": {}}
        outs.append(_text_of(data))
        u = data.get("usage") or {}
        tin += u.get("prompt_tokens", 0)
        tout += u.get("completion_tokens", 0)
    return {"ok": len(outs) == len(texts) and all(outs), "out": outs,
            "ms": int((time.time() - t0) * 1000), "err": "", "usage": {"in": tin, "out": tout}}


def translate(ctx: Ctx, sys_obj: dict, texts: list[str], to: str = "zh") -> dict:
    if sys_obj["proto"] == "mt":
        return translate_mt(ctx, sys_obj, texts, to)
    return translate_json(ctx, sys_obj, texts, to)


# ---------------------------------------------------------------- 客观指标
RE_NUM = re.compile(r"\d+(?:[.,]\d+)*%?")
# 上标/下标数字：论文里 `C^0.27`、`ϕ0` 常被排成 ⁰·²⁷ / ϕ₀，不归一化会被当成"数字丢了"
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉０１２３４５６７８９", "012345678901234567890123456789")
_UNIT = {"B": 10, "M": 4, "K": 3}   # 175B ↔ 1750 亿；1M ↔ 100 万；1K ↔ 1 千
RE_CITE = re.compile(r"\[\s*\d+(?:\s*[,，]\s*\d+)*\s*\]")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")


def _nums(text: str) -> Counter:
    return Counter(RE_NUM.findall(text.translate(_SUP).replace(" ", "")))


def _alt_forms(tok: str) -> list[str]:
    """175B 这类缩写换算成中文计量后再比一次：直译 175B 和 1750 亿都算保真。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([BMK])", tok)
    if not m:
        return []
    val, unit = float(m.group(1)), _UNIT[m.group(2)]
    out = []
    for name, scale in (("亿", 8), ("万", 4), ("千", 3)):
        v = val * 10 ** (unit - scale)
        if v >= 1:
            out += [f"{v:g}", f"{int(v)}"]
    return out


def num_recall(src: str, zh: str) -> tuple[float, list[str]]:
    """原文数字在译文中的保留率。返回 (比例, 丢掉的数字)。"""
    want = _nums(src)
    if not want:
        return 1.0, []
    have = _nums(zh)
    missing, hit = [], 0
    for tok, n in want.items():
        got = min(have.get(tok, 0), n)
        if got < n:
            # 单位缩写被换算成中文计量（175B → 1750 亿）也算保真
            for alt in _alt_forms(tok):
                if alt in zh:
                    got = n
                    break
        hit += got
        if got < n:
            missing.append(tok)
    return hit / sum(want.values()), missing


def cite_recall(src: str, zh: str) -> tuple[float, list[str]]:
    want = RE_CITE.findall(src)
    if not want:
        return 1.0, []
    have = RE_CITE.findall(zh)

    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s)

    have_set = {norm(h) for h in have}
    missing = [w for w in want if norm(w) not in have_set]
    return (len(want) - len(missing)) / len(want), missing


def term_hit(terms: dict, zh: str) -> tuple[float, list[str]]:
    if not terms:
        return 1.0, []
    miss = []
    low = zh.lower()
    for en, alts in terms.items():
        if not any(a in zh or a.lower() in low for a in alts):
            miss.append(en)
    return (len(terms) - len(miss)) / len(terms), miss


def en_residue(zh: str) -> float:
    """译文里英文单词占比（按词数/汉字数粗算），用来抓漏译与"半中半英"。"""
    cjk = len(RE_CJK.findall(zh))
    words = len(RE_LATIN_WORD.findall(zh))
    if cjk == 0:
        return 1.0
    return words / (cjk / 1.6 + words)   # 中文 1.6 字 ≈ 1 个英文词


def len_ratio(en: str, zh: str) -> float:
    return len(RE_CJK.findall(zh)) / max(1, len(en))


# ---------------------------------------------------------------- 回译相似度
def _embed(ctx: Ctx, texts: list[str]) -> list[list[float]]:
    if not settings.emb_api_key:
        return []
    url = f"{settings.emb_base_url.rstrip('/')}/embeddings"
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):
        batch = texts[i : i + 16]
        try:
            r = ctx.client.post(url, headers={"Authorization": f"Bearer {settings.emb_api_key}",
                                              "Content-Type": "application/json"},
                                json={"model": settings.emb_model, "input": batch})
            if r.status_code >= 400:
                return []
            out += [d["embedding"] for d in r.json()["data"]]
        except Exception:  # noqa: BLE001
            return []
    return out


def _cos(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def back_translate(ctx: Ctx, texts: list[str]) -> list[str]:
    """用不参评的模型把中文译文译回英文。

    一批 3 段、4 路并发。**必须允许多次重试并逐段兜底**：第一版一次塞 6 段，
    长段落会把输出预算吃光 → JSON 被截断 → 整批回退成空串 → 相似度算出来是 0.36
    这种"看起来像模型质量问题"的噪声，其实是测量工具坏了。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: list[str] = [""] * len(texts)

    def work(idxs: list[int]) -> None:
        batch = [texts[i] for i in idxs]
        for _ in range(3):
            payload = {
                "model": BT_MODEL,
                "messages": [
                    {"role": "system", "content": _SYS_EN},
                    {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
                ],
                "temperature": 0.1,
                "max_tokens": min(8000, 1200 + int(sum(len(t) for t in batch) * 3)),
                "enable_thinking": False,
                "stream": False,
            }
            try:
                data = _post(ctx, payload)
                obj = _parse_json(_text_of(data))
                arr = obj.get("out") or obj.get("zh") or obj.get("data") or []
                if isinstance(arr, dict):
                    arr = [arr[k] for k in sorted(arr, key=lambda x: str(x))]
                if isinstance(arr, list) and len(arr) == len(batch):
                    for i, v in zip(idxs, arr):
                        out[i] = str(v).strip()
                    return
            except Exception:  # noqa: BLE001 换一批重试
                pass
        for i in idxs:
            out[i] = ""

    groups = [list(range(i, min(i + 3, len(texts)))) for i in range(0, len(texts), 3)]
    if groups:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, groups))
    return out


# ---------------------------------------------------------------- 裁判
JUDGE_SYS = (
    "你是学术翻译审校专家。下面给出英文原文和若干条匿名中文译文（A、B、C…）。\n"
    "请对**每一条**独立打分，不要互相比较后套用同一分数。四个维度，各 1–5 分：\n"
    "- faithfulness：忠实度。有无漏译、错译、增译；数字/引用/公式/变量是否保留；\n"
    "- terminology：术语。核心术语是否用学术通行译法，全篇是否一致；\n"
    "- fluency：流畅度。是否像中文论文而不是机翻，语序、搭配、虚词是否自然；\n"
    "- format：格式。图注「图 N：」、表注「表 N：」、段数与原文一致、无原文序号残留。\n"
    "另外列出该译文的具体错误（最多 3 条，每条 15 字以内，前缀类型：漏译/错译/术语/数字/公式/格式/机翻腔）。\n"
    '严格输出 JSON：{"scores":[{"id":"A","faithfulness":4,"terminology":5,"fluency":3,"format":4,'
    '"errors":["术语：self-attention 译成自关注"]}],"best":"C","worst":"A"}\n'
    "5 分表示该维度无可挑剔，3 分表示能用但有明显瑕疵，1 分表示不可用。"
)


def judge(ctx: Ctx, src: str, cands: dict[str, str], rng: random.Random) -> dict:
    """匿名盲评：译文用 A/B/C… 编号，顺序每段随机，避免位置与品牌偏见。"""
    ids = list(cands)
    rng.shuffle(ids)
    alias = {chr(ord("A") + i): sid for i, sid in enumerate(ids)}
    blocks = [f"[{letter}] {cands[sid]}" for letter, sid in alias.items()]
    user = f"原文：\n{src}\n\n译文：\n" + "\n\n".join(blocks)
    payload = {
        "model": JUDGE_MODEL,
        "messages": [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 6000,
        "enable_thinking": False,
        "stream": False,
    }
    try:
        data = _post(ctx, payload)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}
    obj = _parse_json(_text_of(data))
    scores = obj.get("scores") or []
    out = {}
    for s in scores:
        if not isinstance(s, dict):
            continue
        letter = str(s.get("id", "")).strip("[] ").upper()[:1]
        sid = alias.get(letter)
        if not sid:
            continue
        out[sid] = {
            "faithfulness": float(s.get("faithfulness") or 0),
            "terminology": float(s.get("terminology") or 0),
            "fluency": float(s.get("fluency") or 0),
            "format": float(s.get("format") or 0),
            "errors": [str(e)[:40] for e in (s.get("errors") or [])][:3],
        }
    best, worst = obj.get("best"), obj.get("worst")

    def _sid(x):
        if not isinstance(x, str):
            return None
        return alias.get(x.strip("[] ").upper()[:1]) or (x if x in cands else None)

    return {"scores": out, "best": _sid(best), "worst": _sid(worst),
            "order": list(alias.values()), "alias": alias}


# ---------------------------------------------------------------- 主流程
def run(items: list[dict], outdir: Path, reuse: bool = False) -> dict:
    ctx = Ctx()
    rng = random.Random(20261113)
    outdir.mkdir(parents=True, exist_ok=True)

    # 1) 逐系统翻译（--reuse 时直接读上次的 raw，只重算指标）
    results: dict[str, dict] = {}
    for sys_obj in SYSTEMS:
        sid = sys_obj["id"]
        cached = outdir / f"raw_{sid}.json"
        if reuse and cached.exists():
            results[sid] = json.loads(cached.read_text(encoding="utf-8"))
            log.info("==== %s（复用） ====", sid)
            continue
        log.info("==== %s ====", sid)
        item_out: dict[str, dict] = {}
        for direction in ("en", "zh"):
            group = [it for it in items if it["lang"] == direction]
            src_lang = "en" if direction == "en" else "zh"
            # 方向判定：lang 是源语言，目标取反
            to = "zh" if direction == "en" else "en"
            for i in range(0, len(group), BATCH):
                batch = group[i : i + BATCH]
                res = translate(ctx, sys_obj, [b["src"] for b in batch], to=to)
                for it, zh in zip(batch, res["out"] or [""] * len(batch)):
                    item_out[it["id"]] = {"src_lang": src_lang, "to": to, "zh": zh,
                                          "ok": res["ok"], "ms": res["ms"] // max(1, len(batch)),
                                          "err": res["err"], "usage": res["usage"],
                                          "fallback": res.get("fallback", "")}
                if not res["ok"]:
                    log.warning("%s 批次失败：%s", sid, res["err"])
        results[sid] = item_out
        Path(outdir / f"raw_{sid}.json").write_text(
            json.dumps(item_out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 回译
    pairs: list[tuple[str, str, str]] = []   # (sid, item_id, zh)
    for sid in results:
        for it in items:
            zh = results[sid].get(it["id"], {}).get("zh") or ""
            if zh:
                pairs.append((sid, it["id"], zh))
    back = back_translate(ctx, [p[2] for p in pairs]) if pairs else []
    bt_map = {(sid, iid): back[i] for i, (sid, iid, _) in enumerate(pairs)} if back else {}

    # 3) 向量相似度（原文 vs 回译）
    sim_map: dict[tuple[str, str], float] = {}
    if bt_map:
        src_by_id = {it["id"]: it["src"] for it in items}
        keys = [k for k in bt_map if bt_map[k]]
        vecs = _embed(ctx, [src_by_id[k[1]] for k in keys] + [bt_map[k] for k in keys])
        if vecs and len(vecs) == 2 * len(keys):
            n = len(keys)
            for j, k in enumerate(keys):
                sim_map[k] = _cos(vecs[j], vecs[n + j])

    # 4) 裁判
    judges: dict[str, dict] = {}
    jpath = outdir / "judge.json"
    if reuse and jpath.exists():
        judges = json.loads(jpath.read_text(encoding="utf-8"))
        log.info("裁判结果复用：%d 段", len(judges))
    for it in ([] if judges else items):
        cands = {sid: results[sid][it["id"]]["zh"] for sid in results
                 if results[sid].get(it["id"], {}).get("zh")}
        if len(cands) < 2:
            continue
        judges[it["id"]] = judge(ctx, it["src"], cands, rng)
        log.info("裁判 %s：%s", it["id"], judges[it["id"]].get("error") or "ok")

    (outdir / "judge.json").write_text(json.dumps(judges, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "back.json").write_text(json.dumps({"bt": {f"{k[0]}|{k[1]}": v for k, v in bt_map.items()},
                                                  "sim": {f"{k[0]}|{k[1]}": v for k, v in sim_map.items()}},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")

    # 5) 汇总
    summary = summarize(items, results, judges, sim_map)
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"items": items, "results": results, "judges": judges,
            "sim": sim_map, "summary": summary, "outdir": outdir}


JUDGE_DIMS = ("faithfulness", "terminology", "fluency", "format")


def summarize(items, results, judges, sim_map) -> dict:
    by_id = {it["id"]: it for it in items}
    rows = {}
    for sid, item_out in results.items():
        n = len(items)
        ok_n = sum(1 for v in item_out.values() if v["ok"] and v["zh"])
        nums, cites, terms, resid, lr, sims, lat, toks = [], [], [], [], [], [], [], []
        dup = 0
        seen = Counter()
        jdims = {d: [] for d in JUDGE_DIMS}
        errs: Counter = Counter()
        misses: dict[str, list] = {}
        for it in items:
            v = item_out.get(it["id"]) or {}
            zh = v.get("zh") or ""
            if not zh:
                continue
            seen[zh.strip()] += 1
            lat.append(v.get("ms", 0))
            toks.append((v.get("usage") or {}).get("out", 0))
            if it["lang"] == "en":
                r, miss = num_recall(it["src"], zh)
                nums.append(r)
                if miss:
                    misses.setdefault("数字", []).extend(miss[:3])
                r, miss = cite_recall(it["src"], zh)
                cites.append(r)
                if miss:
                    misses.setdefault("引用", []).extend(miss[:3])
                key = "en"
            else:
                key = "zh"
            r, miss = term_hit(it["terms"], zh)
            terms.append(r)
            if miss:
                misses.setdefault("术语", []).extend(miss[:3])
            resid.append(en_residue(zh) if it["lang"] == "en" else 0.0)
            lr.append(len_ratio(it["src"], zh) if it["lang"] == "en" else
                      len(RE_LATIN_WORD.findall(zh)) / max(1, len(RE_CJK.findall(it["src"]))))
            s = sim_map.get((sid, it["id"]))
            if s is not None:
                sims.append(s)
            sc = ((judges.get(it["id"]) or {}).get("scores") or {}).get(sid)
            if sc:
                for d in JUDGE_DIMS:
                    jdims[d].append(sc[d])
                for e in sc.get("errors") or []:
                    errs[str(e).split("：")[0][:6]] += 1
        dup = sum(c - 1 for c in seen.values() if c > 1)
        mean = lambda xs: round(statistics.mean(xs), 3) if xs else None  # noqa: E731
        rows[sid] = {
            "ok_rate": round(ok_n / n, 3),
            "num": mean(nums), "cite": mean(cites), "term": mean(terms),
            "resid": mean(resid), "len_ratio": mean(lr), "dup": dup,
            "bt_sim": mean(sims), "latency_ms": round(mean(lat) or 0),
            "out_tokens": round(mean(toks) or 0),
            "judge": {d: mean(jdims[d]) for d in JUDGE_DIMS},
            "judge_avg": mean([x for d in JUDGE_DIMS for x in jdims[d]]),
            "errors": errs.most_common(5),
            "misses": {k: sorted(set(v))[:6] for k, v in misses.items()},
        }
    # 裁判票数
    picks: Counter = Counter()
    for it in items:
        j = judges.get(it["id"]) or {}
        best = j.get("best")
        if best and best in (j.get("scores") or {}):
            picks[best] += 1
    return {"models": rows, "best_votes": dict(picks)}


def to_markdown(pack: dict) -> str:
    items, summary, judges = pack["items"], pack["summary"], pack["judges"]
    rows = summary["models"]
    order = [s["id"] for s in SYSTEMS if s["id"] in rows]
    labels = {s["id"]: s["label"] for s in SYSTEMS}
    lines = ["# 翻译多模型评测（自动生成）", "",
             f"- 语料：{len(items)} 段真实论文段落（{sum(1 for i in items if i['lang']=='en')} 段英译中、"
             f"{sum(1 for i in items if i['lang']=='zh')} 段中译英）",
             f"- 协议：与生产一致的 system 提示词 + JSON 数组批量，每批 4 段",
             f"- 回译模型：{BT_MODEL}；裁判模型：{JUDGE_MODEL}（均不参评）", "",
             "## 总表", "",
             "| 模型 | 协议合规 | 数字保真 | 引用保真 | 术语命中 | 英文残留 | 长度比 | 重复段 | 回译相似 | 裁判均分 | 忠实 | 术语 | 流畅 | 格式 | 均耗时 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    def f(x):
        if x is None:
            return "—"
        if isinstance(x, float):
            return f"{x:.3f}"
        return str(x)
    for sid in order:
        r = rows[sid]
        j = r["judge"]
        lines.append("| " + " | ".join([
            labels.get(sid, sid), f"{r['ok_rate']*100:.0f}%", f(r["num"]), f(r["cite"]), f(r["term"]),
            f(r["resid"]), f(r["len_ratio"]), str(r["dup"]), f(r["bt_sim"]), f(r["judge_avg"]),
            f(j["faithfulness"]), f(j["terminology"]), f(j["fluency"]), f(j["format"]),
            f"{r['latency_ms']}ms",
        ]) + " |")
    lines += ["", "## 裁判点名（每段最喜欢的译文）", ""]
    votes = summary.get("best_votes") or {}
    if votes:
        total = sum(votes.values())
        for sid, c in sorted(votes.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {labels.get(sid, sid)}：{c}/{total} 段被选为最佳")
    lines += ["", "## 各段明细", ""]
    for it in items:
        j = judges.get(it["id"]) or {}
        lines += [f"### {it['id']} · {it['title'][:40]} · {it['note']}", "",
                  "> " + it["src"].replace("\n", " ")[:240] + "…", ""]
        sc = j.get("scores") or {}
        if not sc:
            lines += [f"（裁判失败：{j.get('error')}）", ""]
            continue
        for sid in order:
            v = pack["results"].get(sid, {}).get(it["id"], {})
            zh = (v.get("zh") or "").replace("\n", " ")
            s = sc.get(sid)
            if not s:
                continue
            mark = " ⭐" if j.get("best") == sid else ""
            err = "；".join(s.get("errors") or [])
            lines.append(f"- **{labels.get(sid, sid)}**{mark} 忠{s['faithfulness']:.0f}/术{s['terminology']:.0f}/"
                         f"顺{s['fluency']:.0f}/格{s['format']:.0f} — {zh[:200]}" + (f" 〔{err}〕" if err else ""))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 体检已有译文
def _is_prose(src: str, kind: str) -> bool:
    """这一段是否"值得翻译"的自然语言。

    作者名单、arXiv 页眉、脚注 URL、公式块本来就不该翻——它们原样保留是对的。
    如果不先把它们排掉，体检报告里会冒出"8% 译文整段是英文"这种假警报，
    真正的问题反而被埋掉。
    """
    if keep_verbatim(kind, src):
        return False
    if len(RE_CJK.findall(src)) >= 40:   # 中文段落：本来就没几个小写英文词
        return True
    low = [w for w in re.findall(r"[A-Za-z]{3,}", src) if w.islower()]
    if len(low) < 8:
        return False
    if "http" in src and len(low) < 25:
        return False
    return True


RE_DECIMAL = re.compile(r"\d+\.\d+%?|\d+%")


def audit(limit: int = 0) -> dict:
    """用同一套客观指标体检库里已缓存的译文（现状质量，不调模型）。"""
    import sqlite3

    con = sqlite3.connect(ROOT / "data" / "sciagent.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select t.chunk_id, t.doc_id, t.page, t.zh, t.model, c.text, c.kind, d.title "
        "from translations t join chunks c on c.id=t.chunk_id join documents d on d.id=t.doc_id "
        "order by t.doc_id, t.page, t.chunk_id"
    ).fetchall()
    # 还原 overlap 后的原文：同 doc 内按 (page, idx) 顺序推进
    chunk_rows = con.execute(
        "select id, doc_id, page, idx, text, kind from chunks order by doc_id, page, idx"
    ).fetchall()
    prev_by_doc: dict[str, str] = {}
    clean: dict[str, tuple[str, str]] = {}
    for c in chunk_rows:
        clean[c["id"]] = (_strip_carry(prev_by_doc.get(c["doc_id"], ""), c["text"] or ""), c["kind"])
        prev_by_doc[c["doc_id"]] = c["text"] or ""
    con.close()

    st = {"total": len(rows), "modeled": 0, "skipped_nonprose": 0, "empty": 0,
          "untranslated": 0, "num_lost_int": 0, "num_lost_dec": 0, "cite_lost": 0,
          "dup": 0, "by_model": Counter(), "by_doc": {}}
    worst: list[dict] = []
    seen: Counter = Counter()
    for r in rows:
        zh = (r["zh"] or "").strip()
        src, kind = clean.get(r["chunk_id"], (r["text"] or "", r["kind"]))
        st["by_model"][r["model"] or "?"] += 1
        if r["model"] == "verbatim":
            continue                      # 原样保留的公式/代码，不参与质量统计
        st["modeled"] += 1
        bucket = st["by_doc"].setdefault((r["title"] or "")[:34],
                                         {"n": 0, "bad": 0, "resid": 0, "num": 0})
        bucket["n"] += 1
        if not zh:
            st["empty"] += 1
            continue
        seen[zh] += 1
        if not _is_prose(src, kind):
            st["skipped_nonprose"] += 1
            continue
        nr, nmiss = num_recall(src, zh)
        cr, cmiss = cite_recall(src, zh)
        resid = en_residue(zh)
        dec = RE_DECIMAL.findall(src)
        dec_miss = [d for d in dec if d not in zh]
        if resid > 0.35:
            st["untranslated"] += 1
            bucket["resid"] += 1
        if nmiss:
            st["num_lost_int"] += 1
            bucket["num"] += 1
        if dec_miss:
            st["num_lost_dec"] += 1
        if cr < 1:
            st["cite_lost"] += 1
        if resid > 0.35 or nmiss or dec_miss or cr < 1:
            bucket["bad"] += 1
            worst.append({"doc": (r["title"] or "")[:30], "page": r["page"], "chunk": r["chunk_id"],
                          "num": round(nr, 2), "cite": round(cr, 2), "resid": round(resid, 2),
                          "nmiss": nmiss[:5], "dmiss": dec_miss[:5], "cmiss": cmiss[:3],
                          "src": src[:200], "zh": zh[:200]})
    st["dup"] = sum(c - 1 for c in seen.values() if c > 1)
    worst.sort(key=lambda w: (w["num"] + w["cite"] - w["resid"]))
    payload = {"stats": {k: (dict(v) if isinstance(v, Counter) else v) for k, v in st.items()},
               "worst": worst[: (limit or 24)]}
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=0, help="只跑前 N 段（冒烟）")
    ap.add_argument("--audit", action="store_true", help="只体检库内已有译文")
    ap.add_argument("--rebuild-fixture", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="复用已有 raw_*.json，只重算指标")
    ap.add_argument("--outdir", default=str(OUTDIR))
    args = ap.parse_args()

    if args.audit:
        p = audit()
        s = p["stats"]
        print(json.dumps(s, ensure_ascii=False, indent=1))
        print("\n问题最多的段落：")
        for w in p["worst"][:10]:
            print(f"  {w['doc']} p{w['page']} num={w['num']} cite={w['cite']} resid={w['resid']} "
                  f"丢{w['nmiss']} {w['cmiss']}")
        return

    items = load_fixture(args.rebuild_fixture)
    if args.items:
        items = items[: args.items]
    outdir = Path(args.outdir)
    pack = run(items, outdir, reuse=args.reuse)
    md = to_markdown(pack)
    (outdir / "report.md").write_text(md, encoding="utf-8")
    print(md[:4000])


if __name__ == "__main__":
    main()
