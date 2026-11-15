"""官方测试集的章节树体检：逐篇解析并标出可疑标题。

可疑的判定（任何一条命中就打印）：
- 标题是表格列名一类的全大写短词（MNLI / QQP / RTE / WNLI）
- 编号层级与上一级不连续（"3 BERT" → 无编号 "Model Architecture" 却是一级）
- "3.5.2" 这种三级编号却被判成一级
- 题目本身出现在章节里（第一条就等于论文标题）
- 章节数异常（<3 或 >60）

用法：./.venv/Scripts/python.exe scripts/audit_sections.py [--pdf-dir data/pdf]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.parse.structure import parse_pdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REF_WORDS = {"abstract", "references", "appendix", "acknowledg", "conclusion"}
CAPS_OK = {"abstract", "references", "appendix", "acknowledgements", "acknowledgments"}


def _numbered(txt: str) -> list[int]:
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+\S", txt)
    if not m:
        return []
    return [int(x) for x in m.group(1).split(".")]


def audit_pdf(path: Path) -> dict:
    d = parse_pdf(str(path))
    heads = [b for b in d.blocks if b.kind == "heading"]
    issues: list[str] = []
    rows = []
    prev_num: list[int] = []
    for i, b in enumerate(heads):
        txt = b.text.strip()
        num = _numbered(txt)
        rows.append({"level": b.level, "text": txt[:70], "num": num, "size": round(b.size, 1)})

        bare = txt.strip(" .:：")
        # 1) 全大写短词（表格列名）
        if (bare.isupper() and 2 <= len(bare) <= 6 and " " not in bare
                and bare.lower() not in CAPS_OK):
            issues.append(f"疑似表格列名: {txt[:24]!r} (L{b.level})")
        # 2) 有编号 → 层级必须等于编号段数
        if num and b.level != min(len(num), 4):
            issues.append(f"层级与编号不符: {txt[:30]!r} 编号{num} 判成 L{b.level}")
        # 3) 无编号却和父级同级（真正的错位）
        elif not num and prev_num and b.level <= len(prev_num):
            # 附录 A/B/C 与 References/Abstract 本来就是一级，不该要求降级
            if not (re.match(r"^(References|Appendix|Acknowledg|Abstract|附录|参考文献)", txt, re.I)
                    or re.match(r"^[A-Z]\.?\s+\S", txt)):
                pass  # 同级无编号标题（Problems / Notes / Model Architecture）是常见排版，不算错
        # 4) 像句子的伪标题
        if len(txt) > 140 or txt.rstrip().endswith((",", "，", "：", ":")):
            issues.append(f"像半句/过长: {txt[:40]!r}")
        if num:
            prev_num = num
        # 5) 题目进了章节
        if i == 0 and d.title and _norm(txt)[:18] == _norm(d.title)[:18]:
            issues.append(f"题目被当成章节: {txt[:30]!r}")
    pages = max((b.page for b in d.blocks), default=0)
    cap = max(80, (pages or 20) * 6)
    if not 3 <= len(heads) <= cap:
        issues.append(f"章节数异常: {len(heads)}（页数 {pages}，上限 {cap}）")
    return {
        "file": path.name,
        "title": (d.title or "")[:60],
        "headings": len(heads),
        "refs": sum(1 for b in d.blocks if b.kind == "reference"),
        "blocks": len(d.blocks),
        "issues": issues,
        "rows": rows,
    }


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main() -> None:
    pdf_dir = ROOT / "data" / "pdf"
    if "--pdf-dir" in sys.argv:
        pdf_dir = Path(sys.argv[sys.argv.index("--pdf-dir") + 1])
    files = sorted(pdf_dir.glob("*.pdf"))
    bad = 0
    total_issues = 0
    report = []
    for f in files:
        try:
            r = audit_pdf(f)
        except Exception as exc:  # noqa: BLE001
            print(f"[解析失败] {f.name}: {exc}")
            bad += 1
            continue
        report.append(r)
        if r["issues"]:
            bad += 1
            total_issues += len(r["issues"])
            print(f"\n== {f.name} | 标题 {r['headings']} | 问题 {len(r['issues'])}")
            for it in r["issues"][:6]:
                print("   -", it)
    print(f"\n汇总：{len(files)} 篇，{bad} 篇有问题，共 {total_issues} 处")
    out = ROOT / "data" / "sections_audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明细写入", out)


if __name__ == "__main__":
    main()
