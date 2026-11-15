"""只重建章节树（sections 表），不重切分块、不重算向量。

为什么单独做这件事：章节树的错误全部来自"块分类"阶段（作者名被当成标题、
编号被分到另一栏、表格列名被加粗），和分块/向量无关。重新入库会把 2.7 万个
分块重新向量化（几分钟 + 真金白银的接口费），而这里只要几秒。

用法：
    ./.venv/Scripts/python.exe scripts/rebuild_sections.py            # 全部
    ./.venv/Scripts/python.exe scripts/rebuild_sections.py --dry-run  # 只看差异
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import Document, Section  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.parse.structure import parse_pdf  # noqa: E402


def _pdf_for(doc: Document) -> Path | None:
    """按 document 记录找回原始 PDF。上传件带哈希前缀，官方集在 data/pdf。"""
    name = doc.filename or ""
    for cand in (settings.upload_dir / name, settings.pdf_dir / name):
        if cand.exists():
            return cand
    if name:
        hit = next(iter(settings.upload_dir.glob(f"*{name}")), None)
        if hit:
            return hit
    return None


def main() -> None:
    dry = "--dry-run" in sys.argv
    with SessionLocal() as db:
        docs = list(db.scalars(select(Document)).all())
        changed = 0
        missing = 0
        for doc in docs:
            path = _pdf_for(doc)
            if not path:
                missing += 1
                print(f"[跳过] 找不到源文件: {doc.filename}")
                continue
            parsed = parse_pdf(str(path))
            new_rows = [(s["level"], s["title"], s["page"], s["order"]) for s in parsed.sections]
            old_n = db.scalar(select(Section).where(Section.doc_id == doc.id).limit(1))
            old_count = db.query(Section).filter(Section.doc_id == doc.id).count()
            if dry:
                print(f"{doc.filename[:44]:46s} {old_count:3d} → {len(new_rows):3d}")
                continue
            if old_count == len(new_rows) and old_n is not None:
                # 条数一样不代表内容一样，仍然重写（几毫秒，省得判断漏）
                pass
            db.execute(delete(Section).where(Section.doc_id == doc.id))
            for lvl, title, page, order in new_rows:
                db.add(Section(doc_id=doc.id, level=lvl, title=title,
                               page=page, order_idx=order))
            db.commit()
            changed += 1
            print(f"{doc.filename[:44]:46s} {old_count:3d} → {len(new_rows):3d}")
        print(f"\n完成：{changed} 篇重写，{missing} 篇缺源文件，共 {len(docs)} 篇")


if __name__ == "__main__":
    main()
