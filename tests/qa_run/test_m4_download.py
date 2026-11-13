"""模块四：隔离实例上的真实 arXiv 下载入库与追踪（只测不改）。"""
from __future__ import annotations
import json
import time
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8014"
OUT = Path(__file__).resolve().parent / "m4_download.json"

def main():
    c = httpx.Client(timeout=300, trust_env=False)
    rows = []
    def add(case, ok, detail):
        rows.append({"case": case, "status": "PASS" if ok else "FAIL", "detail": detail})
        print(rows[-1]["status"], case, str(detail)[:250], flush=True)
    try:
        r = c.get(BASE + "/api/discover/search", params={"q": "Tool Attention Is All You Need", "max": 8})
        body = r.json(); items = body.get("items") or []
        add("arXiv 在线搜索", r.status_code == 200 and bool(items), {"http": r.status_code, "count": len(items), "titles": [x.get("title") for x in items[:3]]})
        target = next((x for x in items if x.get("arxiv_id") == "2604.21816"), items[0] if items else None)
        if target:
            t = time.time()
            r = c.post(BASE + "/api/discover/import", json={"arxiv_id": target["arxiv_id"], "title": target.get("title") or ""})
            b = r.json(); doc_id = b.get("doc_id") or b.get("id") or (b.get("doc") or {}).get("id")
            add("真实 PDF 下载并入库", r.status_code == 200 and (bool(doc_id) or b.get("stage") == "duplicate"), {"http": r.status_code, "seconds": round(time.time()-t, 2), "response": b})
            if doc_id:
                d = c.get(BASE + f"/api/documents/{doc_id}")
                chunks = c.get(BASE + f"/api/documents/{doc_id}/chunks").json().get("chunks") or []
                add("下载件可打开且切块", d.status_code == 200 and bool(chunks), {"doc": d.json(), "chunks": len(chunks)})
        r = c.get(BASE + "/api/discover/references", params={"doc_id": "dce63a636cf64", "limit": 8, "min_cites": 2})
        b = r.json(); items = b.get("items") or b.get("candidates") or []
        add("参考文献候选按被引排序", r.status_code == 200 and len(items) >= 3, {"http": r.status_code, "response": b})
        r = c.post(BASE + "/api/discover/references/import", json={"arxiv_ids": []})
        add("参考文献空批量拒绝", r.status_code == 400, {"http": r.status_code, "body": r.text[:200]})
        r = c.post(BASE + "/api/discover/watch/run")
        add("手动触发定期追踪", r.status_code == 200, {"http": r.status_code, "body": r.json()})
        st = c.get(BASE + "/api/discover/watch").json()
        add("追踪状态与最近快照", isinstance(st, dict) and "enabled" in st, st)
    except Exception as exc:
        add("测试执行异常", False, f"{type(exc).__name__}: {exc}")
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(x["status"] == "PASS" for x in rows), "FAIL", sum(x["status"] == "FAIL" for x in rows))

if __name__ == "__main__":
    main()
