"""批量导入官方测试集：39 篇 / 1986 页。

设计要点：
- 断点续传：已入库的文件名直接跳过，脚本中断后重跑不会重复建文档
- 逐文件计时：解析 / 向量化 / 总耗时分开记，541 页长书单独高亮
- 去重不算失败：v1 与 v7 内容重合会被拦下，记成 duplicate 而不是 error
- 单文件失败不中断整批：一个 PDF 坏掉不影响其余 38 篇
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.models import Document
from app.db.session import SessionLocal, init_db
from app.pipeline.ingest import ingest

PDF_DIR = Path("data/pdf")
REPORT = Path("data/logs/bulk_import.json")


def main() -> None:
    init_db()
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        done = {d.filename for d in db.scalars(select(Document)).all()}

    files = sorted(PDF_DIR.glob("*.pdf"))
    todo = [f for f in files if f.name not in done]
    print(f"总文件 {len(files)} | 已入库 {len(done)} | 待处理 {len(todo)}", flush=True)

    records: list[dict] = []
    t_all = time.time()

    for i, f in enumerate(todo, 1):
        t0 = time.time()
        tag = "长书" if "UnderstandingDeepLearning" in f.name else ""
        print(f"[{i}/{len(todo)}] {f.name} {tag}", flush=True)
        try:
            with SessionLocal() as db:
                r = ingest(db, f, f.name)
        except Exception as exc:  # noqa: BLE001 单篇崩掉不能拖垮整批
            r = {"ok": False, "stage": "exception", "error": f"{type(exc).__name__}: {exc}"}

        cost = round(time.time() - t0, 1)
        rec = {"file": f.name, "cost_s": cost, **r}
        records.append(rec)

        if r.get("ok"):
            print(
                f"    OK  {r['chunks']} 块 / {r['sections']} 标题 / {r['references']} 参考文献"
                f" / {cost}s (向量 {r['embedding_ms']}ms)",
                flush=True,
            )
        elif r.get("stage") == "duplicate":
            dup = (r.get("duplicates") or [{}])[0]
            print(f"    DUP 与 {dup.get('filename')} 重合 | {dup.get('reason')} / {cost}s", flush=True)
        else:
            print(f"    FAIL {r.get('stage')} {str(r.get('error'))[:120]} / {cost}s", flush=True)

        # 每篇都落一次报告，中途被杀也留得下已完成的统计
        REPORT.write_text(
            json.dumps(
                {"total_pdfs": len(files), "records": records, "elapsed_s": round(time.time() - t_all, 1)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    with SessionLocal() as db:
        docs = db.scalars(select(Document)).all()
        pages = sum(d.page_count or 0 for d in docs)

    ok = [r for r in records if r.get("ok")]
    dup = [r for r in records if r.get("stage") == "duplicate"]
    bad = [r for r in records if not r.get("ok") and r.get("stage") != "duplicate"]

    print("\n" + "=" * 60, flush=True)
    print(f"本轮成功 {len(ok)} | 重复拦截 {len(dup)} | 失败 {len(bad)} | 总耗时 {round(time.time()-t_all,1)}s", flush=True)
    print(f"库内文档 {len(docs)} 篇 / {pages} 页", flush=True)
    for b in bad:
        print(f"   失败 {b['file']}: {b.get('error')}", flush=True)


if __name__ == "__main__":
    main()
