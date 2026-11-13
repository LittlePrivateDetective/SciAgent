"""中文扩充文献下载（测试前置工作，不修改被测项目）。

中文信息学报的 PDF 直链格式（实测）：
    http://jcip.cipsc.org.cn/cn/article/pdf/preview/{article_id}.pdf
article_id 形如 zwxxxb_3861，旧系统的 abstract 编号与新 id 数字一致。
"""
import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
ITEMS = [
    ("zwxxxb_3861", "大语言模型微调方法研究综述", "吴春志 等", 2025, "LLM 微调综述"),
    ("zwxxxb_3659", "大语言模型评测综述", "罗文, 王厚峰", 2024, "LLM 评测综述"),
    ("zwxxxb_3660", "医疗领域对话系统口语理解综述", "任芳慧 等", 2024, "对话系统综述"),
    ("zwxxxb_2994", "神经机器翻译前沿综述", "冯洋, 邵晨泽", 2020, "机器翻译综述"),
]


def main(extra: list[tuple] | None = None) -> None:
    items = ITEMS + (extra or [])
    cli = httpx.Client(timeout=180, follow_redirects=True)
    log = []
    for aid, title, authors, year, tag in items:
        dst = OUT / f"ZH_{aid}_{title[:20]}.pdf"
        rec = {"id": aid, "title": title, "authors": authors, "year": year,
               "topic": tag, "file": dst.name, "lang": "zh"}
        if dst.exists() and dst.stat().st_size > 50000:
            rec.update(status="ok_cached", bytes=dst.stat().st_size)
        else:
            url = f"http://jcip.cipsc.org.cn/cn/article/pdf/preview/{aid}.pdf"
            try:
                r = cli.get(url)
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    dst.write_bytes(r.content)
                    rec.update(status="ok", bytes=len(r.content), source=url)
                    print("ok", aid, title, len(r.content), flush=True)
                else:
                    rec.update(status="failed", error=f"{r.status_code} not-pdf")
                    print("FAILED", aid, r.status_code, flush=True)
            except Exception as exc:  # noqa: BLE001
                rec.update(status="failed", error=str(exc)[:120])
                print("ERR", aid, str(exc)[:90], flush=True)
        log.append(rec)
    (OUT / "download_zh.json").write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                          encoding="utf-8")


if __name__ == "__main__":
    import sys
    extra = []
    if len(sys.argv) > 1 and sys.argv[1] == "--extra":
        extra = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        extra = [tuple(x) for x in extra]
    main(extra)
