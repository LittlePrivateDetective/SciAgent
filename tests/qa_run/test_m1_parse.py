"""模块一：PDF 细粒度解析管线核验（只测不改）。

直接调用项目的 `app.parse.structure.parse_pdf`（不修改任何项目代码），
对官方三类样本与扩充集抽样逐篇核对：标题、作者、摘要、标题树、参考文献、
公式/表格/图注块、扫描件识别、长书解析。

用法：.venv/Scripts/python.exe tests/qa_run/test_m1_parse.py [--full]
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.parse.structure import parse_pdf  # noqa: E402

OUT = Path(__file__).resolve().parent / "m1_parse.json"
CORPUS = ROOT / "tests" / "corpus"
PDF = ROOT / "pdf"

CASES = [
    # (路径, 期望标题关键词, 备注)
    (PDF / "1706.03762v1.pdf", "Attention", "官方标准样本"),
    (PDF / "1706.03762v7.pdf", "Attention", "官方更新版本"),
    (PDF / "1706.03762v1_img.pdf", "", "官方图片版（应识别为扫描件）"),
    (PDF / "UnderstandingDeepLearning_02_09_26_C.pdf", "", "官方 541 页长书"),
    (CORPUS / "EN_1810.04805_BERT.pdf", "BERT", "扩充·双栏"),
    (CORPUS / "EN_1909.11942_ALBERT.pdf", "", "扩充·双栏（文件名为下载任务标签，标题以 PDF 原文为准）"),
    (CORPUS / "EN_2307.08691_FlashAttention-2.pdf", "FlashAttention", "扩充·公式密集"),
    (CORPUS / "EN_2404.14219_Phi-3_Technical_Report.pdf", "Phi-3", "扩充·含大量表格"),
    (CORPUS / "ZH_zwxxxb_3659_大语言模型评测综述.pdf", "", "扩充·中文双栏"),
    (CORPUS / "ZH_zwxxxb_3314_多模态信息处理前沿综述.pdf", "", "扩充·中文双栏"),
    (CORPUS / "ZH_1_大语言模型的工具使用综述.pdf", "", "扩充·自动化学报"),
    (CORPUS / "ZH_zwxxxb_3055_知识图谱研究现状及军事应用.pdf", "", "扩充·知识图谱"),
]


def check(path: Path, title_kw: str, note: str, max_pages: int | None = None) -> dict:
    r = {"file": path.name, "note": note}
    if not path.exists():
        r.update(status="BLOCK", error="文件不存在")
        return r
    t0 = time.time()
    try:
        d = parse_pdf(str(path), max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001
        r.update(status="FAIL", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return r
    ms = int((time.time() - t0) * 1000)
    kinds = Counter(b.kind for b in d.blocks)
    levels = Counter(s.get("level") for s in d.sections)
    title_ok = bool(d.title and len(d.title.strip()) >= 4)
    title_match = (title_kw.lower() in (d.title or "").lower()) if title_kw else None
    heads = [s.get("title", "") for s in d.sections][:6]
    r.update({
        "pages": d.page_count, "parsed_pages": max_pages or d.page_count,
        "ms": ms, "is_scanned": d.is_scanned,
        "title": d.title, "title_nonempty": title_ok, "title_match": title_match,
        "authors_n": len(d.authors), "authors": d.authors[:6],
        "affiliations_n": len(d.affiliations),
        "abstract_chars": len(d.abstract or ""),
        "sections_n": len(d.sections), "section_levels": dict(levels),
        "sections_head": heads,
        "references_n": len(d.references),
        "refs_sample": [x.get("text", "")[:90] for x in d.references[:3]],
        "block_kinds": dict(kinds),
        "arxiv_id": d.arxiv_id, "doi": d.doi,
        "char_count": d.char_count,
    })
    problems = []
    if not title_ok:
        problems.append("标题为空")
    if title_match is False:
        problems.append(f"标题不含期望关键词 {title_kw}")
    if not d.authors:
        problems.append("作者为空")
    if len(d.abstract or "") < 60:
        problems.append("摘要过短/为空")
    if len(d.sections) < 3:
        problems.append("标题树过少")
    if len(d.references) < 3:
        problems.append("参考文献过少")
    r["problems"] = problems
    r["scan_detection"] = "PASS" if d.is_scanned == ("_img" in path.name) else "FAIL"
    r["status"] = "PASS" if not problems else "WARN"
    if d.is_scanned:
        r["status"] = "WARN"
        r["note"] += "（扫描件检测通过；无 OCR 正文，内容识别不得判 PASS）"
    return r


def main() -> None:
    full = "--full" in sys.argv
    cases = CASES
    if "--corpus-all" in sys.argv:
        cases = [(p, "", "扩充集全量逐篇解析") for p in sorted(CORPUS.glob("*.pdf"))]
        global OUT
        OUT = Path(__file__).resolve().parent / "m1_parse_all30.json"
    rows = []
    for path, kw, note in cases:
        mp = None
        if "541 页" in note and not full:
            mp = 40  # 长书抽样 40 页，避免单次测试跑几分钟
        print(f"解析 {path.name} ...", flush=True)
        r = check(path, kw, note, max_pages=mp)
        rows.append(r)
        print(f"  [{r['status']}] {r.get('ms')}ms title={str(r.get('title'))[:60]!r} "
              f"authors={r.get('authors_n')} sections={r.get('sections_n')} "
              f"refs={r.get('references_n')} kinds={r.get('block_kinds')} "
              f"problems={r.get('problems')}", flush=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in rows if r["status"] == "PASS")
    warn = sum(1 for r in rows if r["status"] == "WARN")
    fail = sum(1 for r in rows if r["status"] == "FAIL")
    print(f"\n解析核验：PASS {ok} / WARN {warn} / FAIL {fail}，共 {len(rows)} 篇")
    print("明细 →", OUT)


if __name__ == "__main__":
    main()
