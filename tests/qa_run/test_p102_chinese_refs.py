"""QA-P1-02：8 篇中文信息学报，独立文本层编号对照项目解析结果。"""
import json
import re
import sys
from pathlib import Path
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.parse.structure import parse_pdf
OUT = Path(__file__).resolve().parent / "p102_chinese_refs.json"

def main():
    rows = []
    for path in sorted((ROOT / "tests/corpus").glob("ZH_zwxxxb_*.pdf")):
        with pymupdf.open(path) as doc:
            raw = "\n".join(p.get_text() for p in doc)
        raw = raw[raw.rfind("参考文献"):] if "参考文献" in raw else raw
        source_ids = [int(x) for x in re.findall(r"\[(\d{1,3})\]", raw)]
        expected_max = max(source_ids) if source_ids else 0
        parsed = parse_pdf(str(path))
        ids = [int(r["index"]) for r in parsed.references]
        # 编号完整且数量吻合：既防漏抽，也防把续行重复计成新条目。
        ok = bool(parsed.references) and expected_max in ids and len(parsed.references) == expected_max
        row = {"file": path.name, "source_max_index": expected_max, "parsed_count": len(ids),
               "parsed_min_max": [min(ids), max(ids)] if ids else [], "missing_indices": sorted(set(range(1, expected_max + 1)) - set(ids)),
               "sample_first": parsed.references[0]["text"] if parsed.references else "",
               "sample_last": parsed.references[-1]["text"] if parsed.references else "",
               "status": "PASS" if ok else "FAIL"}
        rows.append(row)
        print(row["status"], path.name, f"source={expected_max} parsed={len(ids)}", flush=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(r["status"] == "PASS" for r in rows), "FAIL", sum(r["status"] == "FAIL" for r in rows))

if __name__ == "__main__":
    main()
