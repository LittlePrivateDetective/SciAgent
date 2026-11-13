"""扩充测试集校验与清单生成（测试前置工作，不修改被测项目）。

逐篇确认：可打开、未损坏、文字层可提取；登记页数、语言、版式、拟测模块。
"""
import json
import re
import shutil
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent
INCOMING = OUT.parents[1] / "_incoming"


def layout_of(page) -> str:
    """单栏/双栏粗判：看文字块在页面水平方向是否明显分成两簇。"""
    w = page.rect.width
    blocks = [b for b in page.get_text("blocks") if (b[4] or "").strip()]
    if len(blocks) < 6:
        return "单栏"
    left = sum(1 for b in blocks if (b[0] + b[2]) / 2 < w * 0.48)
    right = sum(1 for b in blocks if (b[0] + b[2]) / 2 > w * 0.52)
    return "双栏" if left >= 3 and right >= 3 else "单栏"


def inspect(path: Path) -> dict:
    rec = {"file": path.name, "bytes": path.stat().st_size}
    try:
        doc = pymupdf.open(str(path))
        rec["pages"] = doc.page_count
        txt = "".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
        rec["text_chars_first3"] = len(txt.strip())
        rec["text_layer"] = "ok" if len(txt.strip()) > 200 else "weak"
        rec["layout"] = layout_of(doc[min(1, doc.page_count - 1)])
        head = " ".join(doc[0].get_text().split())[:160]
        rec["head"] = head
        # 是否扫描件：前几页几乎没有文字但有图像
        imgs = sum(len(doc[i].get_images()) for i in range(min(3, doc.page_count)))
        rec["images_first3"] = imgs
        if rec["text_layer"] == "weak" and imgs:
            rec["layout"] = "扫描件/图片型（需 OCR）"
        doc.close()
        rec["status"] = "ok" if rec["text_layer"] == "ok" else "需 OCR"
    except Exception as exc:  # noqa: BLE001
        rec.update(status="损坏", error=str(exc)[:120])
    return rec


def main() -> None:
    # 复用 _incoming 的 4 篇（复制，不移动、不改原文件）
    for name, lang in [("EN_1_RAG_arXiv2005.11401.pdf", "en"),
                       ("EN_2_RETRO_arXiv2112.04426.pdf", "en"),
                       ("ZH_1_大语言模型的工具使用综述.pdf", "zh"),
                       ("ZH_2_基于大模型的具身智能系统综述.pdf", "zh")]:
        src = INCOMING / name
        if src.exists() and not (OUT / name).exists():
            shutil.copy2(src, OUT / name)
    # 去掉下载过程中产生的重复命名（保留带文章 id 的版本）
    dup = OUT / "ZH_大语言模型微调方法研究综述.pdf"
    if dup.exists() and (OUT / "ZH_zwxxxb_3861_大语言模型微调方法研究综述.pdf").exists():
        dup.unlink()

    rows = []
    for p in sorted(OUT.glob("*.pdf")):
        r = inspect(p)
        r["lang"] = "en" if p.name.startswith("EN_") else "zh"
        m = re.match(r"(?:EN|ZH)_(\d{4}\.\d{4,5})", p.name)
        r["arxiv"] = m.group(1) if m else ""
        rows.append(r)
        print(f"{r['lang']} {p.name[:52]:52s} p{str(r.get('pages')):>4} {r.get('layout')} {r['status']}")

    en = [r for r in rows if r["lang"] == "en"]
    zh = [r for r in rows if r["lang"] == "zh"]
    print(f"\n英文 {len(en)} 篇，中文 {len(zh)} 篇，合计 {len(rows)} 篇")
    bad = [r for r in rows if r["status"] != "ok"]
    print("需特别处理：", [(r["file"], r["status"]) for r in bad])
    (OUT / "corpus_inventory.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                               encoding="utf-8")


if __name__ == "__main__":
    main()
