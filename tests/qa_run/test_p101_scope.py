"""QA-P1-01 真实服务回归：文字版/扫描版同族不得越过 doc_ids。"""
import json
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8014"
QUESTIONS = [
    "Transformer 原文的编码器和解码器各有多少层？注意力头的数量是多少？请给出数值。",
    "Transformer 的 base 模型总参数量是多少？回答后给出可直接定位原文数值的页码与区域。",
]

def main():
    c = httpx.Client(timeout=180, trust_env=False)
    docs = c.get(BASE + "/api/documents").json()["documents"]
    text = next(d for d in docs if d["filename"] == "1706.03762v1.pdf")
    scan = next(d for d in docs if d["filename"] == "1706.03762v1_img.pdf")
    rows = []
    for q in QUESTIONS:
        r = c.post(BASE + "/api/chat", json={"question": q, "doc_ids": [text["id"]], "mode": "qa", "no_cache": True})
        b = r.json(); evidence = b.get("evidence") or []
        row = {"question": q, "http": r.status_code, "text_id": text["id"], "scan_id": scan["id"],
               "evidence_ids": [e.get("doc_id") for e in evidence], "answer": b.get("answer"),
               "status": "PASS" if r.status_code == 200 and evidence and all(e.get("doc_id") == text["id"] for e in evidence) else "FAIL"}
        rows.append(row); print(row["status"], row["evidence_ids"], flush=True)
    # 第二次命中缓存也必须维持边界。
    r = c.post(BASE + "/api/chat", json={"question": QUESTIONS[0], "doc_ids": [text["id"]], "mode": "qa"})
    b = r.json(); evidence = b.get("evidence") or []
    rows.append({"question": "缓存复测", "meta": b.get("meta"), "evidence_ids": [e.get("doc_id") for e in evidence],
                 "status": "PASS" if evidence and all(e.get("doc_id") == text["id"] for e in evidence) else "FAIL"})
    out = Path(__file__).resolve().parent / "p101_scope.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(x["status"] == "PASS" for x in rows), "FAIL", sum(x["status"] == "FAIL" for x in rows))

if __name__ == "__main__":
    main()
