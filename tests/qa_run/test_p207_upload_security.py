"""QA-P2-07：伪造 PDF 的 HTTP 语义与路径脱敏。"""
import json
from pathlib import Path
import httpx


def main():
    c = httpx.Client(base_url="http://127.0.0.1:8014", timeout=60, trust_env=False)
    rows = []
    for data in (b"not a real pdf", b"%PDF-not-a-valid-document"):
        r = c.post("/api/documents/upload", files={"file": ("fake.pdf", data, "application/pdf")})
        body = r.text
        rows.append({"http": r.status_code, "body": body,
                     "status": "PASS" if r.status_code in (400, 422) and "D:\\" not in body and "projects" not in body else "FAIL"})
        print(rows[-1], flush=True)
    Path(__file__).resolve().with_name("p207_upload_security.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
