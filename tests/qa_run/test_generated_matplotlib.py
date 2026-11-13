"""生成的 Matplotlib 代码真实执行，验证不是仅有语法外壳。"""
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.tools.chart import convert as convert_chart

CSV = "Model,BLEU,Params\nTransformer,28.4,65\nBERT,34.5,110\nGPT-3,41.0,175\n"
OUT = Path(__file__).resolve().parent / "generated_figures"
OUT.mkdir(exist_ok=True)

def main():
    os.chdir(OUT)
    rows = []
    for kind in ("bar", "line", "radar"):
        result = convert_chart(CSV, "matplotlib", "模型对比", kind=kind, caption=False)
        path = OUT / f"{kind}.py"
        path.write_text(result["code"], encoding="utf-8")
        try:
            exec(compile(result["code"], str(path), "exec"), {"__name__": "__main__", "__file__": str(path)})
            png = OUT / f"figure_{kind}.png"
            pdf = OUT / f"figure_{kind}.pdf"
            # 生成代码以当前工作目录为输出目录；测试命令在 OUT 下执行。
            ok = png.exists() and png.stat().st_size > 1000 and pdf.exists() and pdf.stat().st_size > 500
            rows.append({"kind": kind, "status": "PASS" if ok else "FAIL", "png": str(png), "pdf": str(pdf)})
        except Exception as exc:
            rows.append({"kind": kind, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
    Path(__file__).resolve().with_name("generated_matplotlib.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
