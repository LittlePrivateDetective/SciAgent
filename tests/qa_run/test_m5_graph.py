"""模块五：引用图谱、综述引用合法性与 Future Work 证据抽测。"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8013"
OUT = Path(__file__).resolve().parent / "m5_graph.json"

def main():
    c = httpx.Client(timeout=180, trust_env=False)
    rows = []
    def rec(case, ok, detail):
        rows.append({"case": case, "status": "PASS" if ok else "FAIL", "detail": detail})
        print(rows[-1]["status"], case, str(detail)[:180], flush=True)
    g = c.get(BASE + "/api/graph", params={"refresh": "true"}).json()
    nodes, edges = g.get("nodes") or [], g.get("edges") or []
    ids = {n.get("id") for n in nodes}
    rec("图谱结构完整", len(nodes) >= 20 and len(edges) >= 20 and all(e.get("source") in ids and e.get("target") in ids for e in edges),
        {"nodes": len(nodes), "edges": len(edges), "clusters": len({n.get("cluster") for n in nodes}), "top": sorted([(n.get("label"), n.get("pagerank"), n.get("in_degree")) for n in nodes], key=lambda x: x[1] or 0, reverse=True)[:5]})
    t = time.time()
    r = c.post(BASE + "/api/graph/survey", json={"max_papers": 6, "focus": "训练效率"})
    b = r.json(); text = b.get("survey") or ""; papers = b.get("papers") or []
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", text)]
    heads = ["领域脉络", "主要技术路线", "关键分歧", "开放问题"]
    rec("综述四节与引用编号合法", r.status_code == 200 and all(h in text for h in heads) and bool(nums) and all(1 <= n <= len(papers) for n in nums) and len(text) >= 300,
        {"http": r.status_code, "seconds": round(time.time()-t, 2), "chars": len(text), "citations": sorted(set(nums)), "paper_count": len(papers), "survey": text, "papers": papers})
    r = c.post(BASE + "/api/graph/survey", json={"cluster_id": 999999, "max_papers": 3})
    rec("不存在聚类友好拒绝", r.status_code == 400, {"http": r.status_code, "body": r.text[:200]})
    fw = c.get(BASE + "/api/research/future-work", params={"limit": 12}).json()
    items = fw.get("items") or []
    rec("Future Work 带来源定位", bool(items) and all(x.get("doc_id") and x.get("page") and (x.get("bbox") or x.get("anchor")) for x in items), fw)
    rp = c.get(BASE + "/api/research/reproducibility", params={"max_docs": 8, "online": "true"}).json()
    rec("复现性含真实仓库与指标", bool(rp.get("items")) and any(x.get("repos") for x in rp["items"]), rp)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(x["status"] == "PASS" for x in rows), "FAIL", sum(x["status"] == "FAIL" for x in rows))

if __name__ == "__main__":
    main()
