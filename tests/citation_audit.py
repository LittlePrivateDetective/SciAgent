"""锚点保真度全库抽样：100 个块，逐个回 PDF 取矩形文本对账。

为什么不能只靠 `qa_suite` 的 A04：A04 只验 10 条引用，而且是"模型生成的引用"
——样本小、还混入了模型质量的影响。评审会问的是更硬的问题：
**"你的 [p3@120,340,560,410] 到底指不指得到那段话？"**

这里把问题拆干净：不调模型、不生成答案，直接拿库里每个块的 `bbox` 去 PDF 上
取那个矩形里的文字，和第块自己的 `text` 比。命中率低只有一种解释——坐标错了。

抽样是分层的：按文档轮转、每种块类型（text/table/figure/formula/reference）
都保底抽到，避免 100 条全落在同一本书的正文里。

用法：
  ./.venv/Scripts/python.exe tests/citation_audit.py              # 默认 100 条
  ./.venv/Scripts/python.exe tests/citation_audit.py --n 200 --show 15
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sqlalchemy import select  # noqa: E402

from app.core.config import resolve_doc_file  # noqa: E402
from app.db.models import Chunk, Document  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.rag.chunker import strip_carry  # noqa: E402

OUT = ROOT / "docs" / "qa" / "evidence" / "citation_audit.json"

# 归一化：PDF 取出的文本带着换行、连字符断词、多余空格，逐字比对必然全错。
# 去掉所有空白，再把 "-\n" 断词接上，是比较"是不是同一句话"的最小处理。
_RE_WS = re.compile(r"\s+")
_RE_HYPHEN = re.compile(r"(\w)-\s*\n\s*(\w)")
_RE_WORD = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


def norm(text: str) -> str:
    t = _RE_HYPHEN.sub(r"\1\2", text or "")
    return _RE_WS.sub("", t)


def words(text: str) -> set[str]:
    return {w.lower() for w in _RE_WORD.findall(text or "") if len(w) > 1}


def sample(n: int) -> list[dict]:
    with SessionLocal() as db:
        docs = {d.id: d for d in db.scalars(select(Document)).all()}
        rows = db.execute(
            select(Chunk.id, Chunk.doc_id, Chunk.page, Chunk.idx, Chunk.kind,
                   Chunk.bbox, Chunk.text).order_by(Chunk.doc_id, Chunk.page, Chunk.idx)
        ).all()

    # 同 doc 内先按 (page, idx) 排序，便于还原 overlap
    by_doc: dict[str, list] = defaultdict(list)
    for r in rows:
        by_doc[r.doc_id].append(r)

    clean: dict[str, str] = {}
    for did, items in by_doc.items():
        prev = ""
        for r in items:
            clean[r.id] = strip_carry(prev, r.text or "")
            prev = r.text or ""

    # 分层轮转：先每篇每种类型各取一条，再按篇轮流补足
    picked: list = []
    seen_ids: set[str] = set()
    kinds = ("text", "table", "figure", "formula", "reference")
    cursor = {did: 0 for did in by_doc}
    for kind in kinds:
        for did in by_doc:
            for r in by_doc[did]:
                if r.kind == kind and r.id not in seen_ids and len(clean[r.id] or "") > 40:
                    picked.append(r)
                    seen_ids.add(r.id)
                    break
    while len(picked) < n:
        progressed = False
        for did, items in by_doc.items():
            i = cursor[did]
            while i < len(items):
                r = items[i]
                i += 1
                if r.id in seen_ids or len(clean[r.id] or "") < 40:
                    continue
                picked.append(r)
                seen_ids.add(r.id)
                progressed = True
                break
            cursor[did] = i
            if len(picked) >= n:
                break
        if not progressed:
            break

    return [{"id": r.id, "doc_id": r.doc_id, "doc_name": docs[r.doc_id].title or docs[r.doc_id].filename,
             "page": r.page, "kind": r.kind, "bbox": r.bbox, "text": clean[r.id]}
            for r in picked[:n]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--show", type=int, default=10, help="打印多少条未命中的样例")
    ap.add_argument("--min-recall", type=float, default=0.80)
    ap.add_argument("--min-precision", type=float, default=0.50)
    args = ap.parse_args()

    import pymupdf

    items = sample(args.n)
    print(f"抽样 {len(items)} 条")

    with SessionLocal() as db:
        docs = {d.id: d for d in db.scalars(select(Document)).all()}

    pages: dict[tuple[str, int], object] = {}
    docs_open: dict[str, object] = {}
    results = []
    t0 = time.time()
    for it in items:
        doc = docs.get(it["doc_id"])
        rec = {**it, "ok": False, "recall": 0.0, "precision": 0.0, "note": ""}
        if doc is None:
            rec["note"] = "文档不存在"
            results.append(rec)
            continue
        path = resolve_doc_file(doc)
        if not path:
            rec["note"] = "PDF 找不到"
            results.append(rec)
            continue
        if it["doc_id"] not in docs_open:
            docs_open[it["doc_id"]] = pymupdf.open(str(path))
        pdf = docs_open[it["doc_id"]]
        if not (1 <= (it["page"] or 0) <= pdf.page_count):
            rec["note"] = "页码越界"
            results.append(rec)
            continue
        try:
            x0, y0, x1, y1 = [float(v) for v in (it["bbox"] or "0,0,0,0").split(",")]
        except ValueError:
            rec["note"] = "bbox 解析失败"
            results.append(rec)
            continue
        page = pdf[it["page"] - 1]
        rect = pymupdf.Rect(x0, y0, x1, y1)
        if rect.is_empty or rect.width <= 1 or rect.height <= 1:
            rec["note"] = "bbox 面积为零"
            results.append(rec)
            continue
        if not rect.intersects(page.rect):
            rec["note"] = "bbox 在页面外"
            results.append(rec)
            continue
        # 整页没有文本层 = 图片版 PDF（有 1 篇 Word 转的图，页面上靠渲染图看）。
        # 这种文档用文本层校核没有意义，单独归类，不混进可达率的分母——
        # 否则 98% 会被写成"2 条失败"，而实际上那两条根本无从校核。
        if len((page.get_text("text") or "").strip()) < 20:
            rec.update(ok=None, note="图片版 PDF（无文本层，靠页图渲染）")
            results.append(rec)
            continue
        got = page.get_text("text", clip=rect) or ""
        a, b = norm(it["text"]), norm(got)
        if not b:
            rec["note"] = "矩形内取不到文本（图片区/扫描页）"
            results.append(rec)
            continue
        wa, wb = words(it["text"]), words(got)
        recall = len(wa & wb) / max(len(wa), 1)
        precision = len(wa & wb) / max(len(wb), 1)
        # 判定只看 recall：它回答的是"点了引用能不能落到这段原文"。
        # precision 低是另一回事——首页那种"标题+作者+摘要"合一块的块，
        # bbox 是联合框，矩形里当然还框着页眉和作者名单；内容照样看得见，
        # 只是高亮面积偏大。把它算成"溯源失败"会掩盖真正的问题（坐标错位）。
        rec.update(recall=round(recall, 3), precision=round(precision, 3),
                   ok=recall >= args.min_recall,
                   tight=recall >= args.min_recall and precision >= args.min_precision)
        results.append(rec)

    for obj in docs_open.values():
        obj.close()

    graded = [r for r in results if r["ok"] is not None]
    skipped = [r for r in results if r["ok"] is None]
    hit = sum(1 for r in graded if r["ok"])
    tight = sum(1 for r in graded if r.get("tight"))
    by_kind = Counter(r["kind"] for r in results)
    ok_kind = Counter(r["kind"] for r in results if r["ok"])
    recalls = sorted(r["recall"] for r in results)
    precs = sorted(r["precision"] for r in results)
    med = lambda xs: xs[len(xs) // 2] if xs else 0.0  # noqa: E731

    print(f"可达 {hit}/{len(graded)} = {hit/max(len(graded),1):.1%}   "
          f"精确高亮 {tight}/{len(graded)} = {tight/max(len(graded),1):.1%}   "
          f"不可校核 {len(skipped)} 条（图片版 PDF）   用时 {time.time()-t0:.1f}s")
    print(f"recall 中位数 {med(recalls):.3f} / precision 中位数 {med(precs):.3f}")
    print("按类型：", {k: f"{ok_kind.get(k,0)}/{v}" for k, v in by_kind.items()})
    bad = [r for r in graded if not r["ok"]]
    loose = [r for r in graded if r["ok"] and not r.get("tight")]
    if loose:
        print(f"（其中 {len(loose)} 条能落到原文但高亮框偏大，多为首页作者块）")
    for r in bad[: args.show]:
        print(f"  MISS {r['kind']:9s} p{r['page']:<3} recall={r['recall']:.2f} "
              f"prec={r['precision']:.2f} {r['note'] or ''} | {(r['text'] or '')[:70]!r}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sampled": len(results), "graded": len(graded), "unverifiable": len(skipped),
        "hit": hit, "tight": tight,
        "rate": round(hit / max(len(graded), 1), 4),
        "tight_rate": round(tight / max(len(graded), 1), 4),
        "median_recall": med(recalls), "median_precision": med(precs),
        "by_kind": {k: [ok_kind.get(k, 0), v] for k, v in by_kind.items()},
        "misses": [r for r in bad][:40],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告：{OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
