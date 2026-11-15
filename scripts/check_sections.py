"""章节树体检：逐篇解析，跟 PDF 自带书签对照，并跑一组结构检查。

用法：
    ./.venv/Scripts/python.exe scripts/check_sections.py            # 汇总
    ./.venv/Scripts/python.exe scripts/check_sections.py --full 3   # 详细看第 3 篇

检查项（都是"人眼一看就知道错"的那类）：
1. 标题像句子（含句中断句 / 中文句号）
2. 标题里有 ≥3 个独立数字（表格行）
3. 标题是纯数字/纯符号
4. 同级标题重复（相邻同名）
5. 层级跳跃（L1 → L3，中间没父级）
6. 页码倒序（后一个标题的页码比前一个小）
7. 与 PDF 自带书签的差集（少了哪些、多了哪些）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402

from app.parse.structure import parse_pdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "sections_check.json"

RE_SENT = re.compile(r'(?:\w{3,}|\))\.\s+[A-Z][a-z"\'(]')
RE_NUM3 = re.compile(r"(?<![\w.])\d{1,4}(?![\w.])")


def norm(t: str) -> str:
    """归一化后比较：去掉空白与**编号前缀**。

    书签写的是 `5.1 Invariance under weights…`，解析出来的标题常常只有正文部分
    （`Invariance under weights…`）——编号在块里被单独拆走了。不剥编号，
    每条都会报"书签里少了"，全是假警报。
    """
    t = re.sub(r"^\s*(?:[A-Z]\.(?:\d{1,3}\.){0,3}|\d{1,2}(?:\.\d{1,2}){0,3}\.?)\s*", "", t)
    return re.sub(r"[\s\u3000]+", "", t).lower()


def check(path: Path) -> dict:
    issues: list[str] = []
    try:
        d = parse_pdf(str(path))
    except Exception as exc:  # noqa: BLE001
        return {"file": path.name, "error": str(exc), "issues": ["解析失败"]}

    rows = [(s["level"], s["title"].strip(), s["page"]) for s in d.sections]
    if not rows:
        issues.append("零章节")

    # 1) 句子
    for lvl, t, pg in rows:
        body = re.sub(r"^\s*(?:[A-Z]\.(?:\d{1,3}\.){0,3}|\d{1,2}(?:\.\d{1,2}){0,3}\.?)", "", t)
        if RE_SENT.search(body) or "。" in t:
            issues.append(f"p{pg} 像句子: {t[:48]}")

    # 2) 数字过多
    for lvl, t, pg in rows:
        body = re.sub(r"^\s*\d{1,2}(?:\.\d{1,2}){0,3}", "", t)
        if len(RE_NUM3.findall(body)) >= 3:
            issues.append(f"p{pg} 数字行: {t[:48]}")

    # 3) 纯数字/符号
    for lvl, t, pg in rows:
        if re.fullmatch(r"[\d\W_]{0,6}", t):
            issues.append(f"p{pg} 纯数字/符号: {t!r}")

    # 4) 相邻同名
    for (l1, t1, p1), (l2, t2, p2) in zip(rows, rows[1:]):
        if l1 == l2 and t1 == t2:
            issues.append(f"p{p2} 相邻重复: {t2[:40]}")

    # 5) 层级跳跃
    prev = 0
    for lvl, t, pg in rows:
        if lvl > prev + 1 and prev > 0:
            issues.append(f"p{pg} 层级跳跃 L{prev}->L{lvl}: {t[:40]}")
        prev = lvl

    # 6) 页码倒序（同一层级）
    last_page_by_level: dict[int, int] = {}
    for lvl, t, pg in rows:
        if lvl in last_page_by_level and pg < last_page_by_level[lvl]:
            issues.append(f"页码倒序 p{pg}<p{last_page_by_level[lvl]}（L{lvl}）: {t[:36]}")
        last_page_by_level[lvl] = max(pg, last_page_by_level.get(lvl, pg))

    # 7) 与 PDF 自带书签对照
    toc_missing: list[str] = []
    try:
        doc = pymupdf.open(str(path))
        toc = doc.get_toc(simple=True)
        doc.close()
    except Exception:  # noqa: BLE001
        toc = []
    if toc:
        have = {norm(t) for _, t, _ in rows}
        for _, tt, _ in toc:
            n = norm(tt)
            if n and n not in have and not any(n in h for h in have):
                toc_missing.append(tt[:50])
        if toc_missing:
            issues.append(f"书签里少了 {len(toc_missing)} 条（书签共 {len(toc)}）")

    return {
        "file": path.name,
        "sections": len(rows),
        "toc": len(toc),
        "toc_missing": toc_missing[:12],
        "issues": issues,
        "rows": [{"level": l, "title": t, "page": p} for l, t, p in rows],
    }


def main() -> None:
    files = sorted((ROOT / "data" / "pdf").glob("*.pdf"))
    results = [check(p) for p in files]
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    bad = [r for r in results if r.get("issues")]
    print(f"共 {len(results)} 篇，{len(bad)} 篇有问题\n")
    for r in bad:
        print(f"=== {r['file'][:52]}  节 {r.get('sections')}  书签 {r.get('toc')}")
        for i in r["issues"][:8]:
            print("    -", i)
        if len(r["issues"]) > 8:
            print(f"    … 还有 {len(r['issues'])-8} 条")

    if "--full" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--full") + 1])
        r = results[idx]
        print(f"\n===== 全文：{r['file']}")
        for row in r["rows"]:
            print(f"  L{row['level']} p{row['page']:<4} {row['title'][:78]}")


if __name__ == "__main__":
    main()
