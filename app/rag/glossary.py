"""论文翻译术语表：给提示词注入统一译法，并检查"同词不同译"。

为什么需要它：翻译评测（`tests/trans_bench.py`）里，10 个模型、17 段真实段落，
裁判挑出的错误有七成是"术语用词"——同一篇论文里 `dominant` 一会儿译"主导"
一会儿译"主流"，`hidden units` 一会儿"隐藏单元"一会儿"隐单元"。这不是模型
听不懂，是**没有给它一个可遵守的约束**：长文分几十批翻，每批独立调用，
模型只能各自"即兴发挥"。

两条设计约束（否则注入术语表反而会变差）：
1. **只注入本批真正出现的词条**，最多 12 条。把 100 条全塞进 system 提示，
   轻则多花 200 token/批，重则模型开始"为了用术语而用术语"，把正常句子改坏。
2. **绝不强制替换**译文。术语表是给模型的约束，不是给代码的后处理——用正则
   把"隐单元"替换成"隐藏单元"会改坏"隐单元的维度是 512"这种正常句子。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import BASE_DIR
from app.core.logger import get_logger

log = get_logger(__name__)

GLOSSARY_PATH = BASE_DIR / "data" / "glossary.json"
MAX_INJECT = 12


@dataclass(frozen=True)
class Term:
    en: str
    zh: str
    keep_en: bool = False

    @property
    def pattern(self) -> re.Pattern:
        # 边界要连字符一起排除，否则 "attention" 会命中 "self-attention" 内部，
        # 一条英文段落里同时注入 `self-attention` 与 `attention` 两条，
        # 模型会纠结"到底按哪个译"（实测出现过半句用"自注意力"半句用"注意力"）。
        return re.compile(rf"(?<![A-Za-z\-]){re.escape(self.en)}(?![A-Za-z\-])", re.IGNORECASE)


@lru_cache(maxsize=1)
def load() -> tuple[Term, ...]:
    if not GLOSSARY_PATH.exists():
        return ()
    try:
        raw = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 术语表坏了不能拖垮翻译：退回"没有术语表"，翻译照常走。
        log.warning("术语表读取失败，本次不注入：%s", exc)
        return ()
    terms = []
    for item in raw.get("terms") or []:
        en = (item.get("en") or "").strip()
        zh = (item.get("zh") or "").strip()
        if en and zh:
            terms.append(Term(en=en, zh=zh, keep_en=bool(item.get("keep_en"))))
    return tuple(terms)


def reload() -> int:
    load.cache_clear()
    return len(load())


def for_text(text: str, limit: int = MAX_INJECT) -> list[Term]:
    """挑出这段文字里真实出现的术语，长词优先（避免被短词抢先占位）。"""
    if not text:
        return []
    terms = sorted(load(), key=lambda t: -len(t.en))
    taken: list[tuple[int, int]] = []
    picked: list[Term] = []
    for t in terms:
        for m in t.pattern.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue          # 与已选中的术语区间重叠，跳过
            taken.append((s, e))
            if t not in picked:
                picked.append(t)
            break                  # 同一术语只记一次
        if len(picked) >= limit:
            break
    return picked


def prompt_block(terms: list[Term]) -> str:
    if not terms:
        return ""
    lines = ["【术语表（本段出现的术语必须按下表译，全篇保持一致）】"]
    for t in terms:
        lines.append(f"- {t.en} → {'保留英文原词' if t.keep_en else t.zh}")
    return "\n".join(lines)


def compliance(pairs: list[tuple[str, str]]) -> list[dict]:
    """统计"术语遵守率"：原文出现该术语的段落里，有多少译文中用了标准译名。

    只报告、不改写。低于 60% 才算"不一致"——同一批里出现"注意力"和"注意力机制"
    是正常措辞变化，不该当成错误去回炉（回炉会把好不容易翻好的句子重来一遍，
    而且大概率还是那个译法）。
    """
    out: list[dict] = []
    for t in load():
        if t.keep_en:
            continue
        hits = [dst for src, dst in pairs if t.pattern.search(src or "")]
        if len(hits) < 2:
            continue
        ok = sum(1 for d in hits if t.zh and t.zh in (d or ""))
        rate = ok / len(hits)
        if rate < 0.6:
            out.append({"en": t.en, "zh": t.zh, "n": len(hits),
                        "ok": ok, "rate": round(rate, 2)})
    return out
