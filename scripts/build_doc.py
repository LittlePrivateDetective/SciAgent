"""把 docs/技术文档.md 渲染成可提交的 PDF。

为什么自己写这一层，而不是用现成的 md→pdf 工具链：
- 本机没有 wkhtmltopdf / pandoc / LaTeX，装任何一套都比这一百多行代码重；
- 版面要求是"学术论文"：A4、宋体正文、章节另起一页、表格不跨页断行，
  这些用 CSS 的 @page 与 page-break 控制最直接；
- 渲染器用系统已有的 Edge（headless），不额外引入浏览器。

依赖：`markdown`（只用于本文档构建，不在 requirements.txt 里，避免给运行时
增加一个用不到的包）。缺它时先 `pip install markdown`。

用法：
    ./.venv/Scripts/python.exe scripts/build_doc.py
产物：
    docs/技术文档.html   （可直接在浏览器看）
    docs/技术文档.pdf    （提交件，页数会打印出来；超过 30 页返回码 2）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC_MD = DOCS / "技术文档.md"
OUT_HTML = DOCS / "技术文档.html"
OUT_PDF = DOCS / "技术文档.pdf"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CSS = r"""
@page { size: A4; margin: 19mm 17mm 17mm 17mm; }

* { box-sizing: border-box; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  margin: 0;
  font-family: "Times New Roman", "SimSun", "Songti SC", serif;
  font-size: 10.5pt;
  line-height: 1.78;
  color: #1f2937;
  text-align: justify;
}

/* ---------------- 封面 ---------------- */
.cover {
  height: 252mm;
  padding-top: 58mm;
  page-break-after: always;
  text-align: center;
}
.cover-kicker {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 10pt;
  letter-spacing: 1.5px;
  color: #6b7280;
  margin-bottom: 22mm;
}
.cover-title {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 46pt;
  font-weight: 700;
  letter-spacing: 2px;
  color: #1e3a8a;
  line-height: 1.1;
}
.cover-rule {
  width: 64mm; height: 2.4pt;
  background: #1e3a8a;
  margin: 7mm auto 8mm auto;
}
.cover-sub {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 16pt;
  color: #374151;
  letter-spacing: 1px;
}
.cover-sub2 {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 13pt;
  color: #6b7280;
  letter-spacing: 6px;
  margin-top: 5mm;
}
.cover-meta {
  margin: 30mm auto 0 auto;
  width: 140mm;
  border-top: 0.6pt solid #cbd5e1;
  padding-top: 5mm;
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 9.5pt;
  text-align: left;
}
.cm-row { display: flex; margin-bottom: 2.6mm; }
.cm-k { width: 22mm; color: #6b7280; flex: none; }
.cm-v { color: #1f2937; flex: 1; }

/* ---------------- 目录 ---------------- */
.pb { page-break-after: always; height: 0; }

.toc { margin-top: 2mm; }
.toc h1 { display: none; }
.toc-title {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 19pt;
  font-weight: 700;
  color: #1e3a8a;
  padding-bottom: 2.4mm;
  border-bottom: 1.6pt solid #1e3a8a;
  margin-bottom: 5mm;
}
.toc ul { list-style: none; padding-left: 0; margin: 0; }
.toc > ul > li { margin: 2.6mm 0 1.2mm 0; }
.toc > ul > li > a {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 11pt;
  font-weight: 700;
  color: #1e3a8a;
}
.toc ul ul { padding-left: 6mm; margin: 1mm 0 1.4mm 0; }
.toc ul ul li { margin: 0.9mm 0; font-size: 9.8pt; }
.toc ul ul ul { display: none; }
.toc a { color: #374151; text-decoration: none; }
.toc a:hover { color: #1e3a8a; }

/* ---------------- 标题 ---------------- */
h1 {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 19pt;
  color: #1e3a8a;
  margin: 0 0 6mm 0;
  padding-bottom: 2.4mm;
  border-bottom: 1.6pt solid #1e3a8a;
  page-break-before: always;
  page-break-after: avoid;
  letter-spacing: 0.5px;
}
h1:first-of-type { page-break-before: auto; }

h2 {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 12.6pt;
  color: #1e293b;
  margin: 7mm 0 2.6mm 0;
  padding-left: 3mm;
  border-left: 3.6pt solid #6366f1;
  page-break-after: avoid;
}

h3 {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 11pt;
  color: #334155;
  margin: 5mm 0 2mm 0;
  page-break-after: avoid;
}

p { margin: 0 0 2.4mm 0; orphans: 2; widows: 2; }

strong { color: #111827; }

/* ---------------- 表格 ---------------- */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 3mm 0 4mm 0;
  font-size: 8.8pt;
  line-height: 1.55;
  page-break-inside: auto;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th {
  background: #eef2ff;
  color: #1e3a8a;
  font-family: "Microsoft YaHei", sans-serif;
  font-weight: 600;
  text-align: left;
  padding: 1.8mm 2mm;
  border: 0.5pt solid #c7d2fe;
}
td {
  padding: 1.6mm 2mm;
  border: 0.5pt solid #d1d5db;
  vertical-align: top;
}
tbody tr:nth-child(even) { background: #fbfcff; }

/* ---------------- 代码 ---------------- */
pre {
  background: #f8fafc;
  border: 0.5pt solid #e2e8f0;
  border-left: 3pt solid #94a3b8;
  border-radius: 2pt;
  padding: 2.6mm 3mm;
  font-family: Consolas, "Courier New", monospace;
  font-size: 8.4pt;
  line-height: 1.5;
  overflow-wrap: break-word;
  white-space: pre-wrap;
  page-break-inside: avoid;
  margin: 2.6mm 0 4mm 0;
}
code {
  font-family: Consolas, "Courier New", monospace;
  font-size: 8.8pt;
  background: #f1f5f9;
  padding: 0 1.2mm;
  border-radius: 2pt;
  color: #0f172a;
}
pre code { background: none; padding: 0; font-size: 8.4pt; }

/* ---------------- 列表 ---------------- */
ul, ol { margin: 0 0 2.6mm 0; padding-left: 7mm; }
li { margin: 0.9mm 0; }
li > p { margin-bottom: 1.2mm; }

/* ---------------- 图与公式 ---------------- */
.fig {
  margin: 3mm 0 5mm 0;
  page-break-inside: avoid;
  text-align: center;
}
.fig svg { width: 100%; height: auto; }

.formula {
  font-family: "Times New Roman", serif;
  font-size: 11pt;
  text-align: center;
  margin: 3.4mm 0 4mm 0;
  padding: 2.4mm 0;
  background: #f8fafc;
  border-top: 0.5pt solid #e2e8f0;
  border-bottom: 0.5pt solid #e2e8f0;
}

blockquote {
  margin: 3mm 0;
  padding: 2mm 4mm;
  border-left: 3pt solid #cbd5e1;
  color: #4b5563;
  background: #f9fafb;
}

hr { border: none; border-top: 0.6pt solid #e5e7eb; margin: 5mm 0; }
"""


_TAG_RE = re.compile(r"(<[^>]+>)")


def smart_quotes(html: str) -> str:
    """把正文里的 ASCII 双引号换成中文弯引号 “ ”。

    只处理**标签之外**的文本：标签内部（class="fig"）与 <pre>/<code> 内部保持原样，
    否则会把 HTML 属性写坏、把代码里的字符串改成弯引号。
    引号状态跨文本节点保持，因为 "…<strong>…</strong>…" 这种被行内标签切开的引号
    必须成对，不能每个节点各自从开引号算起。
    """
    parts = _TAG_RE.split(html)
    out: list[str] = []
    open_q = True
    skip = 0
    for part in parts:
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith("<pre") or low.startswith("<code"):
                skip += 1
            elif low.startswith("</pre") or low.startswith("</code"):
                skip = max(0, skip - 1)
            out.append(part)
            continue
        if skip or '"' not in part:
            out.append(part)
            continue
        buf = []
        for ch in part:
            if ch == '"':
                buf.append("“" if open_q else "”")
                open_q = not open_q
            else:
                buf.append(ch)
        out.append("".join(buf))
    return "".join(out)


def render_html(md_text: str) -> str:
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        extension_configs={"toc": {"toc_depth": "1-2", "permalink": False}},
    )
    body = md.convert(md_text)
    body = smart_quotes(body)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>SciAgent 技术文档</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("未找到 msedge.exe，无法打印 PDF")


def main() -> None:
    if not SRC_MD.exists():
        raise SystemExit(f"源文件不存在：{SRC_MD}")

    md_text = SRC_MD.read_text(encoding="utf-8")
    html = render_html(md_text)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"[1/3] HTML 已生成：{OUT_HTML.relative_to(ROOT)}　{len(html):,} 字节")

    edge = find_edge()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_pdf = Path(tmp) / "doc.pdf"
        profile = Path(tmp) / "profile"
        cmd = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=15000",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={tmp_pdf}",
            OUT_HTML.as_uri(),
        ]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not tmp_pdf.exists():
            print(r.stdout[-2000:], r.stderr[-2000:])
            raise SystemExit("PDF 未生成")
        shutil.copy2(tmp_pdf, OUT_PDF)
        print(f"[2/3] PDF 已生成：{OUT_PDF.relative_to(ROOT)}　耗时 {time.time()-t0:.1f}s")

    try:
        import pymupdf

        doc = pymupdf.open(OUT_PDF)
        n = doc.page_count
        chars = sum(len(p.get_text()) for p in doc)
        print(f"[3/3] 页数 {n}　可提取文字 {chars:,} 字　平均 {chars // max(n,1)} 字/页")
        if n > 30:
            print(f"⚠️  超出 30 页上限 {n - 30} 页，需要压缩")
            sys.exit(2)
    except ImportError:
        print("[3/3] 未安装 pymupdf，跳过页数统计")


if __name__ == "__main__":
    main()
