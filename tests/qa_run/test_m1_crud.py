"""模块一：PDF 文件管理 CRUD 与内容级去重（只测不改）。

跑在**隔离实例**（8014，数据库为 tests/qa_run/data/sciagent.db 副本）上，
不上传/删除主实例 8013 的任何文献，不修改被测项目源码与配置。

用法：.venv/Scripts/python.exe tests/qa_run/test_m1_crud.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8014"
ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "corpus"
PDF = ROOT / "pdf"
OUT = Path(__file__).resolve().parent / "m1_crud.json"

results: list[dict] = []


def rec(case: str, expect: str, ok: bool | None, detail: str, ms: int = 0) -> None:
    r = {"case": case, "expect": expect, "status": "PASS" if ok else ("FAIL" if ok is False else "BLOCK"),
         "detail": detail[:600], "ms": ms}
    results.append(r)
    print(f"[{r['status']:5s}] {case:38s} {detail[:150]}", flush=True)


def upload(cli: httpx.Client, path: Path, force: bool = False,
           replace_doc_id: str | None = None) -> tuple[int, dict, int]:
    t = time.time()
    with path.open("rb") as fh:
        data = {"force": str(force).lower()}
        if replace_doc_id:
            data["replace_doc_id"] = replace_doc_id
        r = cli.post(BASE + "/api/documents/upload",
                     files={"file": (path.name, fh, "application/pdf")}, data=data)
    ms = int((time.time() - t) * 1000)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:300]}
    return r.status_code, body, ms


def main() -> None:
    # trust_env=False：本机有 http_proxy，默认会把 127.0.0.1 的请求也交给代理，
    # 结果拿到的是代理的 502/HTML 而不是接口 JSON
    cli = httpx.Client(timeout=300, trust_env=False)
    r = cli.get(BASE + "/api/documents")
    docs = r.json()
    items = docs.get("items") or docs.get("documents") or []
    n0 = len(items)
    rec("列表接口", "返回库内文献列表", r.status_code == 200 and n0 > 0,
        f"HTTP {r.status_code}，{n0} 篇；首篇字段 {sorted((items[0] if items else {}).keys())[:12]}")

    # ---- 单篇上传（英文，双栏 BERT）----
    en = CORPUS / "EN_2402.09353_DoRA.pdf"
    sc, body, ms = upload(cli, en)
    en_id = body.get("doc_id") or body.get("id") or (body.get("doc") or {}).get("id")
    rec("单篇上传（英文双栏）", "解析入库；重复执行时正确判重",
        sc == 200 and (bool(en_id) or body.get("stage") == "duplicate"),
        f"HTTP {sc} doc_id={en_id} 页={body.get('page_count')} 块={body.get('chunks')} 耗时 {ms/1000:.1f}s "
        f"msg={body.get('message')}", ms)

    # ---- 同文件再传：应判重 ----
    sc, body, ms = upload(cli, en)
    rec("同文件重复上传", "按内容判重，不重复入库", sc == 200 and body.get("stage") == "duplicate",
        f"HTTP {sc} stage={body.get('stage')} msg={str(body.get('message'))[:80]} "
        f"duplicates={len(body.get('duplicates') or [])}", ms)

    # ---- 中文上传 ----
    zh = CORPUS / "ZH_zwxxxb_3659_大语言模型评测综述.pdf"
    sc, body, ms = upload(cli, zh)
    zh_id = body.get("doc_id") or body.get("id") or (body.get("doc") or {}).get("id")
    rec("单篇上传（中文双栏综述）", "中文解析入库；若隔离库已有则正确判重",
        sc == 200 and (bool(zh_id) or body.get("stage") == "duplicate"),
        f"HTTP {sc} doc_id={zh_id} 页={body.get('page_count')} 块={body.get('chunks')} 耗时 {ms/1000:.1f}s "
        f"stage={body.get('stage')} msg={body.get('message')}", ms)

    # ---- 批量上传（3 篇）----
    batch_ids = []
    for name in ["EN_1909.11942_ALBERT.pdf", "EN_2302.04761_Toolformer.pdf",
                 "ZH_zwxxxb_3314_多模态信息处理前沿综述.pdf"]:
        sc, body, ms = upload(cli, CORPUS / name)
        did = body.get("doc_id") or body.get("id") or (body.get("doc") or {}).get("id")
        batch_ids.append(did)
        rec(f"批量上传 {name[:26]}", "逐篇入库；若隔离库已有则正确判重",
            sc == 200 and (bool(did) or body.get("stage") == "duplicate"),
            f"HTTP {sc} id={did} 页={body.get('page_count')} 块={body.get('chunks')} {ms/1000:.1f}s "
            f"stage={body.get('stage')}", ms)

    # ---- 版本去重：库内是 v7，再传 v1 ----
    v1 = PDF / "1706.03762v1.pdf"
    sc, body, ms = upload(cli, v1)
    rec("版本去重（库内 v7 → 传 v1）", "识别库内更新版本并给出选择，不静默覆盖",
        sc == 200 and body.get("stage") == "duplicate",
        f"HTTP {sc} stage={body.get('stage')} msg={str(body.get('message'))[:120]} "
        f"pending_id={body.get('pending_id')} "
        f"dup_keys={list((body.get('duplicates') or [{}])[0].keys())[:10] if body.get('duplicates') else []}", ms)
    pending = body.get("pending_id")

    # ---- 非 PDF 文件 ----
    tmp = Path(__file__).resolve().parent / "not_a_pdf.txt"
    tmp.write_text("not a pdf", encoding="utf-8")
    r = cli.post(BASE + "/api/documents/upload",
                 files={"file": ("not_a_pdf.txt", b"not a pdf", "text/plain")})
    rec("上传非 PDF", "返回 400 与可读提示", r.status_code == 400,
        f"HTTP {r.status_code} body={r.text[:120]}")

    # ---- 详情与删除/恢复 ----
    current = (cli.get(BASE + "/api/documents").json().get("documents") or [])
    victim = batch_ids[0] or next((d.get("id") for d in current if d.get("filename") == "EN_1909.11942_ALBERT.pdf"), None) or en_id
    if not en_id:
        en_id = next((d.get("id") for d in current if d.get("filename") == en.name), None)
    if victim:
        r = cli.get(BASE + f"/api/documents/{victim}")
        rec("文献详情", "返回结构化字段", r.status_code == 200,
            f"HTTP {r.status_code} keys={list(r.json().keys())[:12]}")
        r = cli.delete(BASE + f"/api/documents/{victim}")
        rec("删除文献", "进入回收站而非物理删除", r.status_code == 200,
            f"HTTP {r.status_code} body={r.text[:150]}")
        r = cli.get(BASE + "/api/documents")
        after = r.json().get("documents") or r.json().get("items") or []
        still = any((d.get("id") == victim) for d in after)
        rec("删除后列表", "主列表不再出现该文献", not still,
            f"剩余 {len(after)} 篇，目标仍在列表={still}")
        # 回收站
        for path in ["/api/documents/trash", "/api/trash", "/api/documents?trash=1"]:
            rr = cli.get(BASE + path)
            if rr.status_code == 200:
                rec("回收站列表", "可查到被删文献", True, f"GET {path} → {rr.text[:200]}")
                break
        else:
            rec("回收站列表", "可查到被删文献", None, "未找到回收站接口（需人工确认前端入口）")
        # 恢复
        restored = False
        for path, method in [(f"/api/documents/{victim}/restore", "post"),
                             (f"/api/documents/{victim}/restore", "put")]:
            rr = getattr(cli, method)(BASE + path)
            if rr.status_code == 200:
                restored = True
                rec("恢复文献", "从回收站恢复", True, f"{method.upper()} {path} → {rr.text[:150]}")
                break
        if not restored:
            rec("恢复文献", "从回收站恢复", None, "恢复接口未命中（需按实际路由补测）")

    # ---- 覆盖（force）----
    if en_id:
        sc, body, ms = upload(cli, en, force=True)
        rec("强制覆盖上传", "force=True 时允许重新解析覆盖", sc == 200,
            f"HTTP {sc} stage={body.get('stage')} msg={str(body.get('message'))[:100]}", ms)

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"\n模块一 CRUD：PASS {passed} / FAIL {failed} / 共 {len(results)}")
    print("明细 →", OUT)


if __name__ == "__main__":
    main()
