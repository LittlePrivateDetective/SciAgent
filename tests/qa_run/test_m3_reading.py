"""模块三：版式阅读、引用跳转数据与双向翻译出口实测（只测不改）。"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8013"
OUT = Path(__file__).resolve().parent / "m3_reading.json"

def main():
    c = httpx.Client(timeout=240, trust_env=False)
    docs = c.get(BASE + "/api/documents").json()["documents"]
    def find(fragment):
        return next(d for d in docs if fragment in d["filename"])
    en = find("Constitutional_AI")
    attn = find("1706.03762v1.pdf")
    zh = find("cn_paper.pdf")
    scan = find("1706.03762v1_img.pdf")
    rows = []
    def rec(name, fn):
        t = time.time()
        try:
            ok, detail = fn()
            rows.append({"case": name, "status": "PASS" if ok else "FAIL", "seconds": round(time.time()-t, 2), "detail": detail})
        except Exception as exc:
            rows.append({"case": name, "status": "FAIL", "seconds": round(time.time()-t, 2), "detail": f"{type(exc).__name__}: {exc}"})
        print(rows[-1]["status"], name, str(rows[-1]["detail"])[:180], flush=True)

    rec("章节树真实标题", lambda: (lambda r: (r.status_code == 200 and len(r.json().get("sections", [])) >= 3 and all(x.get("title") for x in r.json()["sections"]), r.json()))(c.get(BASE + f"/api/documents/{en['id']}/sections")))
    rec("页文本层坐标", lambda: (lambda r: (r.status_code == 200 and r.json().get("has_text_layer") and len(r.json().get("words", [])) > 20, {"http": r.status_code, "words": len(r.json().get("words", []))}))(c.get(BASE + f"/api/documents/{en['id']}/page/1/text")))
    rec("页图 PNG", lambda: (lambda r: (r.status_code == 200 and r.content.startswith(b"\x89PNG") and len(r.content) > 10000, {"http": r.status_code, "bytes": len(r.content), "type": r.headers.get("content-type")}))(c.get(BASE + f"/api/documents/{en['id']}/page/1.png")))
    rec("扫描件文本层状态诚实", lambda: (lambda r: (r.status_code == 200 and r.json().get("has_text_layer") is False, r.json()))(c.get(BASE + f"/api/documents/{scan['id']}/page/1/text")))
    rec("正文引用反查有命中", lambda: (lambda r: (r.status_code == 200 and r.json().get("occurrences", 0) > 0 and all(("anchor" in x or "bbox" in x) for x in r.json().get("hits", [])), r.json()))(c.get(BASE + f"/api/documents/{attn['id']}/references/1/cites")))

    def zh_export():
        a = c.get(BASE + f"/api/documents/{en['id']}/translate-zh", params={"preview": 20000})
        b = c.get(BASE + f"/api/documents/{en['id']}/translate-zh.md")
        text = b.text
        return (a.status_code == 200 and b.status_code == 200 and len(text) > 1000 and len(re.findall(r"[\u4e00-\u9fff]", text)) > 300 and text.startswith("# "),
                {"preview_http": a.status_code, "download_http": b.status_code, "chars": len(text), "cjk": len(re.findall(r'[\u4e00-\u9fff]', text)), "head": text[:500]})
    rec("中文译本预览与 Markdown 下载", zh_export)

    translations = [
        ("英译中保留数字引用", "The model has 175B parameters [3], and uses 8 attention heads.", "zh", ["175", "8", "[3]"]),
        ("中译英", "我们提出一种检索增强方法，在三个中文问答数据集上将准确率提高 4.2 个百分点。", "en", ["4.2", "three"]),
        ("公式符号保留", "Let β1 = 0.9 and β2 = 0.999; the learning rate is 3e-4.", "zh", ["0.9", "0.999", "3e-4"]),
    ]
    for name, text, target, must in translations:
        def call(text=text, target=target, must=must):
            r = c.post(BASE + "/api/translate", json={"text": text, "to": target})
            b = r.json(); out = b.get("text") or b.get("zh") or ""
            return r.status_code == 200 and len(out) > 8 and all(x.lower() in out.lower() for x in must), {"http": r.status_code, "output": out, "ms": b.get("ms")}
        rec(name, call)

    def en_job():
        st = c.get(BASE + f"/api/documents/{zh['id']}/translate-en").json()
        if not st.get("ready") and not (st.get("segments") and not st.get("running")):
            st = c.post(BASE + f"/api/documents/{zh['id']}/translate-en").json()
        deadline = time.time() + 180
        while time.time() < deadline:
            st = c.get(BASE + f"/api/documents/{zh['id']}/translate-en").json()
            if st.get("running"):
                time.sleep(2); continue
            break
        dl = c.get(BASE + f"/api/documents/{zh['id']}/translate-en.md")
        return (not st.get("running") and not st.get("error") and dl.status_code == 200 and len(dl.text) > 500 and "Attention" in dl.text,
                {"status": st, "download_http": dl.status_code, "chars": len(dl.text), "head": dl.text[:500]})
    rec("中文论文整篇译英与下载", en_job)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", sum(r["status"] == "PASS" for r in rows), "FAIL", sum(r["status"] == "FAIL" for r in rows), "TOTAL", len(rows))

if __name__ == "__main__":
    main()
