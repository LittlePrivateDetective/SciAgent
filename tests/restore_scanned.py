"""恢复扫描件：走产品自带的「上传 → 判重 → 两份都留」真实流程。

rescan 会把 1706.03762v1_img.pdf 判成 v1 的同族版本而跳过，
但 QA 硬性用例 1 要求库内必须有一份扫描件（OCR 路径）可测。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
B = "http://127.0.0.1:8013"
src = ROOT / "data" / "pdf" / "1706.03762v1_img.pdf"

t = time.time()
with open(src, "rb") as f:
    r = httpx.post(B + "/api/documents/upload",
                   files={"file": (src.name, f, "application/pdf")}, timeout=900).json()
print("upload:", json.dumps({k: v for k, v in r.items() if k != "diff"}, ensure_ascii=False)[:600])

if r.get("stage") == "duplicate" and r.get("pending_id"):
    r2 = httpx.post(B + "/api/documents/resolve",
                    json={"pending_id": r["pending_id"], "action": "both"}, timeout=900).json()
    print("resolve both:", json.dumps(r2, ensure_ascii=False)[:600])
else:
    print("resolve: skipped")

info = httpx.get(B + "/api/system/info", timeout=30).json()
print(f"elapsed={time.time()-t:.1f}s counts={info['counts']}")
