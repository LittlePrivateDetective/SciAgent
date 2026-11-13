"""模块六：写作、图表全部合法组合、拓扑与异常输入实测。"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8013"
OUT = Path(__file__).resolve().parent / "m6_writing.json"
CSV = "Model,BLEU,Params\nTransformer,28.4,65\nBERT,34.5,110\nGPT-3,41.0,175\n"
COMBOS = {"bar": ["mermaid", "tikz", "matplotlib"], "line": ["mermaid", "tikz", "matplotlib"], "radar": ["tikz", "matplotlib"], "table": ["latex", "markdown"]}

def main():
    c = httpx.Client(timeout=180, trust_env=False)
    docs = c.get(BASE + "/api/documents").json()["documents"]
    ids = [next(d["id"] for d in docs if x in d["filename"]) for x in ("2106.09685v2.pdf", "2305.14314v1.pdf")]
    rows = []
    def rec(case, ok, detail):
        rows.append({"case": case, "status": "PASS" if ok else "FAIL", "detail": detail})
        print(rows[-1]["status"], case, str(detail)[:160], flush=True)
    for kind, fmts in COMBOS.items():
        for fmt in fmts:
            t = time.time(); r = c.post(BASE + "/api/tools/csv2chart", json={"csv": CSV, "kind": kind, "format": fmt, "title": "模型对比", "caption": False})
            b = r.json(); code = b.get("code") or ""
            numbers_ok = all(x in code for x in ("28.4", "34.5", "41.0", "65", "110", "175"))
            grammar = {"mermaid": "xychart-beta" in code, "tikz": "\\begin" in code and "\\end" in code,
                       "matplotlib": "matplotlib" in code and ("plot" in code or "bar" in code),
                       "latex": "\\toprule" in code and "\\bottomrule" in code,
                       "markdown": "|" in code and "---" in code}[fmt]
            rec(f"CSV {kind}/{fmt}", r.status_code == 200 and b.get("ok") and numbers_ok and grammar,
                {"http": r.status_code, "seconds": round(time.time()-t, 2), "numbers_ok": numbers_ok, "grammar": grammar, "chars": len(code), "code_head": code[:300]})
    r = c.post(BASE + "/api/tools/csv2chart", json={"csv": CSV, "kind": "bar", "format": "mermaid", "title": "模型对比", "caption": True})
    b = r.json(); cap = json.dumps(b.get("caption") or b, ensure_ascii=False)
    rec("Figure Caption 数字回查", r.status_code == 200 and not (b.get("caption_checks") or b.get("cap_checks") or {}).get("foreign"),
        {"http": r.status_code, "caption": b.get("caption"), "caption_zh": b.get("caption_zh"), "caption_en": b.get("caption_en"), "checks": b.get("caption_checks") or b.get("cap_checks"), "raw_keys": list(b)})
    r = c.post(BASE + "/api/tools/csv2chart", json={"csv": "only_header", "kind": "bar", "format": "mermaid"})
    rec("无数据 CSV 拒绝", r.status_code == 400, {"http": r.status_code, "body": r.text[:200]})
    r = c.post(BASE + "/api/tools/csv2chart", json={"csv": CSV, "kind": "radar", "format": "mermaid"})
    rec("不支持的图表组合拒绝", r.status_code == 400, {"http": r.status_code, "body": r.text[:200]})
    t = time.time(); r = c.post(BASE + "/api/write/topology", json={"idea": "面向论文阅读的检索增强系统：PDF 解析、混合检索、证据校验、答案生成与原文定位"})
    b = r.json(); code = json.dumps(b.get("code") or b.get("codes") or b, ensure_ascii=False)
    rec("架构拓扑四类代码", r.status_code == 200 and len(b.get("nodes") or []) >= 4 and all(x in code for x in ("mermaid", "dot", "tikz", "matplotlib")),
        {"http": r.status_code, "seconds": round(time.time()-t, 2), "nodes": b.get("nodes"), "edges": b.get("edges"), "keys": list(b), "source": b.get("source")})
    t = time.time(); r = c.post(BASE + "/api/write/outline", json={"idea": "比较低秩适配与量化微调在长文献科研问答中的成本与效果", "doc_ids": ids, "top_k": 6})
    b = r.json(); refs = b.get("references") or []
    rec("写作大纲与真实 References", r.status_code == 200 and len(b.get("sections") or []) >= 4 and len(refs) >= 2 and all(x.get("doc_id") in ids for x in refs),
        {"http": r.status_code, "seconds": round(time.time()-t, 2), "title": b.get("title"), "sections": b.get("sections"), "references": refs, "stats": b.get("stats")})
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(x["status"] == "PASS" for x in rows), "FAIL", sum(x["status"] == "FAIL" for x in rows), "TOTAL", len(rows))

if __name__ == "__main__":
    main()
