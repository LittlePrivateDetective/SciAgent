"""解析器升级后重算入库指纹（content_hash / simhash）。

为什么需要：这两个指纹在入库时按当时解析出的正文算，之后解析器改过（表格几何重建、
章节识别、图表判定顺序），同一份 PDF 重新解析出来的正文就和存量的指纹对不上了。
后果不是"测试挂"，而是**去重会失效**——新上传的同一篇论文按新解析算指纹，
跟库里按旧解析存的指纹不相等，标题又恰好不同（比如加了 v2 后缀）时就判不出重复。

用法：
    ./.venv/Scripts/python.exe scripts/refresh_hashes.py --dry-run
    ./.venv/Scripts/python.exe scripts/refresh_hashes.py

只动 documents 表的两列，不碰 chunk、不碰向量、不碰译文缓存。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.logger import get_logger  # noqa: E402
from app.db.models import Document  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.parse.structure import parse_pdf  # noqa: E402
from app.rag.dedup import content_hash, simhash64  # noqa: E402

log = get_logger("refresh_hashes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告差异，不写库")
    args = ap.parse_args()

    changed: list[tuple[str, str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    total = 0

    with SessionLocal() as db:
        docs = db.scalars(select(Document).order_by(Document.filename)).all()
        total = len(docs)
        for doc in docs:
            path = Path(doc.file_path)
            if not path.exists():
                skipped.append((doc.filename, "文件不存在"))
                continue
            try:
                parsed = parse_pdf(str(path))
                text = parsed.body_text()
            except Exception as exc:  # noqa: BLE001 解析失败不该中断整批
                skipped.append((doc.filename, f"解析失败：{type(exc).__name__}"))
                continue

            new_h, new_s = content_hash(text), simhash64(text)
            same = doc.content_hash == new_h and doc.simhash == new_s
            if not same:
                changed.append((doc.filename, doc.content_hash or "", new_h, new_s))
                if not args.dry_run:
                    doc.content_hash = new_h
                    doc.simhash = new_s
            print(f"  {doc.filename[:44]:44s} {'一致' if same else '变化'}  {len(text):>7} 字")

        if not args.dry_run:
            db.commit()

    print(f"\n共 {total} 篇，指纹更新 {len(changed)} 篇，跳过 {len(skipped)} 篇")
    for name, reason in skipped:
        print(f"  跳过 {name}: {reason}")
    if args.dry_run and changed:
        print("（--dry-run，未写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
