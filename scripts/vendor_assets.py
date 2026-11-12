"""把前端依赖从 CDN 抄到本地 web/vendor/，并把页面引用改成 /static/vendor/...

为什么做这件事：
    页面原来从 cdn.jsdelivr.net 引 7 个依赖（Vue / Element Plus / ECharts / Mermaid /
    KaTeX 及其字体）。国内访问 jsdelivr 经常几秒起步甚至失败，评委点开演示链接
    很可能看到"白屏"或"没样式"，而不是我们辛苦做的界面。
    本地实测单取 Vue 就要 2.8 秒，而首屏并发取 7 个——首屏 5 秒以上。

为什么不是"装个打包工具"：
    项目从头到尾没有构建链（Vue 用 global 版、模板写在 HTML 里）。这里只是把
    静态文件换个位置放，仍然是"打开 HTML 就能跑"，只是不再依赖外网。

用法：
    ./.venv/Scripts/python.exe scripts/vendor_assets.py
    ./.venv/Scripts/python.exe scripts/vendor_assets.py --check   # 只检查还有没有外链
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
VENDOR = WEB / "vendor"

CDN = "https://cdn.jsdelivr.net/npm"

# 远程地址 → 本地文件名
ASSETS = {
    f"{CDN}/vue@3.4.31/dist/vue.global.prod.js": "vue.global.prod.js",
    f"{CDN}/element-plus@2.7.6/dist/index.full.min.js": "element-plus.full.min.js",
    f"{CDN}/element-plus@2.7.6/dist/index.css": "element-plus.css",
    f"{CDN}/element-plus@2.7.6/dist/locale/zh-cn.min.js": "element-plus-zh-cn.min.js",
    f"{CDN}/echarts@5.5.0/dist/echarts.min.js": "echarts.min.js",
    f"{CDN}/mermaid@10.9.1/dist/mermaid.min.js": "mermaid.min.js",
    f"{CDN}/katex@0.16.11/dist/katex.min.js": "katex.min.js",
    f"{CDN}/katex@0.16.11/dist/katex.min.css": "katex.min.css",
    f"{CDN}/katex@0.16.11/dist/contrib/auto-render.min.js": "katex-auto-render.min.js",
}

VERSION = "5"  # 改静态引用后同步改页面上的版本号，绕开浏览器缓存


def fetch(url: str, dest: Path, retries: int = 4) -> int:
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SciAgent"})
            with urllib.request.urlopen(req, timeout=45) as r:
                data = r.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return len(data)
        except Exception as exc:  # noqa: BLE001 网络抖动重试
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"下载失败 {url}: {last}")


def vendor(force: bool = False) -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    if force:
        for p in VENDOR.glob("*"):
            p.unlink()
    total = 0
    for url, name in ASSETS.items():
        size = fetch(url, VENDOR / name)
        total += size
        print(f"  {name:34s} {size/1024:8.0f} KB")

    # KaTeX 的 CSS 里用相对路径引字体（url(fonts/KaTeX_xxx.woff2)），
    # 少一个字体就会退化成"方块字"，所以按 CSS 里出现的清单逐个抓。
    css = (VENDOR / "katex.min.css").read_text(encoding="utf-8")
    fonts = sorted(set(re.findall(r"url\(fonts/([^)]+)\)", css)))
    fdir = VENDOR / "fonts"
    fdir.mkdir(exist_ok=True)
    for fn in fonts:
        size = fetch(f"{CDN}/katex@0.16.11/dist/fonts/{fn}", fdir / fn)
        total += size
    print(f"  KaTeX 字体 {len(fonts)} 个，合计 {sum((fdir/f).stat().st_size for f in fonts)/1024:.0f} KB")
    print(f"vendor/ 合计 {total/1024/1024:.1f} MB（字体另计）")


def rewrite() -> None:
    """把两个页面里的 CDN 引用换成 /static/vendor/...，并把静态资源版本号推到 VERSION。"""
    mapping = {url: f"/static/vendor/{name}" for url, name in ASSETS.items()}
    for page in ("index.html", "admin.html"):
        p = WEB / page
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        for url, local in mapping.items():
            s = s.replace(url, local)
        # 顺带升版本号：style.css / app.js / admin.js 都带 ?v=
        s = re.sub(r"\?v=\d+", f"?v={VERSION}", s)
        p.write_text(s, encoding="utf-8")
        print(f"  {page} 已改写")


def check() -> int:
    bad = []
    for page in ("index.html", "admin.html"):
        p = WEB / page
        if not p.exists():
            continue
        for m in re.finditer(r'(?:src|href)="(https?://[^"]+)"', p.read_text(encoding="utf-8")):
            bad.append((page, m.group(1)))
    if bad:
        print("仍有外链：")
        for page, url in bad:
            print(f"  {page}: {url}")
        return 1
    print("页面无任何外链依赖 ✓")
    missing = [n for n in ASSETS.values() if not (VENDOR / n).exists()]
    if missing:
        print(f"缺文件：{missing}")
        return 1
    print(f"vendor/ 文件齐全（{len(ASSETS)} 个 + 字体）✓")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否还有外链")
    ap.add_argument("--force", action="store_true", help="重新下载已存在的文件")
    args = ap.parse_args()
    if args.check:
        return check()
    vendor(force=args.force)
    rewrite()
    return check()


if __name__ == "__main__":
    sys.exit(main())
