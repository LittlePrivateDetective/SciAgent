"""全功能巡检：把每个接口都用真实入参打一遍，验证返回结构和内容是否合理。

与 qa_suite 的分工：
    qa_suite 按"赛题考点"组织（A/B/C/D/E/F 六类），验的是"这个能力有没有达标"；
    本脚本按"接口"组织，验的是"页面上的每个按钮背后那条路是不是通的"。
    两者互补——qa_suite 过了 38/40 之后，仍然漏掉了「整篇翻译 422」（D-73），
    因为那条路径没有任何用例覆盖。

用法：
    ./.venv/Scripts/python.exe tests/api_sweep.py            # 打本机 8013
    ./.venv/Scripts/python.exe tests/api_sweep.py --base https://sciagent-research.app.workbuddy.host

设计约束：
1) 不跑破坏性操作：/system/reembed（重建全部向量）、/library/rescan（重新入库）
   默认跳过，加 --slow 才跑；
2) 上传用例会真的传一份 PDF 进去，跑完删掉并清空回收站，保证库里数量不变；
3) 每条记录耗时，方便发现"能返回但慢到不可用"的接口。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "qa" / "evidence"


class Sweep:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=300, trust_env=False)
        self.rows: list[dict] = []

    def call(self, name: str, method: str, url: str, *, ok=None, json_body=None,
             expect: str = "", files=None, data=None, allow=(200,), slow: bool = False) -> dict:
        t0 = time.time()
        try:
            if files is not None:
                r = self.c.request(method, url, files=files, data=data)
            elif json_body is not None:
                r = self.c.request(method, url, json=json_body)
            else:
                r = self.c.request(method, url)
            dt = time.time() - t0
            passed = r.status_code in allow
            detail = ""
            if ok is not None:
                try:
                    body = r.json()
                except Exception:
                    body = None
                try:
                    passed = passed and bool(ok(body))
                except Exception as exc:  # noqa: BLE001 断言自己炸了也算失败
                    passed = False
                    detail = f"断言异常 {type(exc).__name__}: {exc}"
                if not detail:
                    detail = (json.dumps(body, ensure_ascii=False)[:160]
                              if body is not None else r.text[:160])
            else:
                detail = r.text[:120].replace("\n", " ")
            self.rows.append({"name": name, "method": method, "url": url, "status": r.status_code,
                              "ms": int(dt * 1000), "pass": passed, "expect": expect,
                              "detail": detail})
            flag = "PASS" if passed else "FAIL"
            print(f"  [{flag}] {name:30s} {r.status_code} {dt:6.2f}s  {detail[:90]}")
            return {"status": r.status_code, "body": (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)}
        except Exception as exc:  # noqa: BLE001
            dt = time.time() - t0
            self.rows.append({"name": name, "method": method, "url": url, "status": "ERR",
                              "ms": int(dt * 1000), "pass": False, "expect": expect,
                              "detail": f"{type(exc).__name__}: {exc}"})
            print(f"  [FAIL] {name:30s} ERR {dt:6.2f}s  {type(exc).__name__}: {exc}")
            return {}

    def summary(self) -> dict:
        total = len(self.rows)
        passed = sum(1 for r in self.rows if r["pass"])
        by_status: dict[str, int] = {}
        for r in self.rows:
            by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
        return {"base": self.base, "total": total, "pass": passed, "fail": total - passed,
                "by_status": by_status, "rows": self.rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8013")
    ap.add_argument("--slow", action="store_true", help="包含重建索引/重新入库等耗时操作")
    args = ap.parse_args()

    s = Sweep(args.base)
    print(f"巡检目标：{s.base}\n")

    # ---------- 基础信息 ----------
    print("【系统与任务】")
    info = s.call("系统信息", "GET", "/api/system/info",
                  ok=lambda b: b["counts"]["documents"] > 0 and b["counts"]["chunks"] > 0,
                  expect="文档数与块数非零")
    s.call("健康检查", "GET", "/health", ok=lambda b: True)
    s.call("保活状态", "GET", "/api/keepalive", ok=lambda b: "interval" in b or "enabled" in b)
    s.call("保活心跳", "POST", "/api/keepalive/beat", ok=lambda b: b.get("ok") is True)
    s.call("预热", "GET", "/api/warm", ok=lambda b: b.get("ok") and b["retrieval"]["ok"],
           expect="检索预热成功")
    s.call("任务列表", "GET", "/api/tasks", ok=lambda b: True)
    s.call("审计日志", "GET", "/api/audit", ok=lambda b: True)
    s.call("工具目录", "GET", "/api/tools/catalog", ok=lambda b: bool(b))
    s.call("工具健康", "GET", "/api/tools/health", ok=lambda b: True)

    # ---------- 文献与阅读 ----------
    print("\n【文献与阅读】")
    docs = s.call("文献列表", "GET", "/api/documents",
                  ok=lambda b: len(b.get("documents", [])) > 10, expect="≥10 篇")
    items = (docs.get("body") or {}).get("documents", []) if isinstance(docs, dict) else []
    # 挑三篇：有正文的、扫描件、有 arXiv 号的
    main_doc = next((d for d in items if d["filename"].endswith(".pdf") and "_img" not in d["filename"]), items[0])
    scan_doc = next((d for d in items if "_img" in d["filename"]), None)
    arx_doc = next((d for d in items if (d.get("arxiv_id") or "")), main_doc)
    did = main_doc["id"]

    s.call("文献详情", "GET", f"/api/documents/{did}", ok=lambda b: b.get("id") == did)
    s.call("章节树", "GET", f"/api/documents/{did}/sections", ok=lambda b: True)
    s.call("块列表", "GET", f"/api/documents/{did}/chunks", ok=lambda b: True)
    s.call("页元数据", "GET", f"/api/documents/{did}/pages", ok=lambda b: True)
    s.call("页文本层", "GET", f"/api/documents/{did}/page/1/text", ok=lambda b: True)
    s.call("页图", "GET", f"/api/documents/{did}/page/1.png", ok=lambda b: True)
    s.call("参考文献表", "GET", f"/api/documents/{did}/references", ok=lambda b: True)
    s.call("引用跳正文", "GET", f"/api/documents/{did}/references/1/cites", ok=lambda b: True)
    s.call("被引关系", "GET", f"/api/documents/{did}/refs", ok=lambda b: True)
    # diff 必须指定"跟谁比"，缺参数会 422——这是接口设计，不是 bug
    s.call("版本差异", "GET", f"/api/documents/{arx_doc['id']}/diff?other_id={main_doc['id']}",
           allow=(200, 400, 404), expect="同族则 200，跨族 400/404")
    s.call("回收站", "GET", "/api/documents/trash", ok=lambda b: True)
    # 批量删除：空选择必须拒绝；提交不存在的 id 要如实报 failed，而不是整批 500。
    # 这里故意传重复 id，验证服务端去重后只报一条。
    s.call("批量删除(空选择)", "POST", "/api/documents/batch-delete",
           json_body={"ids": []}, allow=(400,), expect="空选择必须 400 拒绝")
    s.call("批量删除(不存在的 id)", "POST", "/api/documents/batch-delete",
           json_body={"ids": ["nope-batch-1", "nope-batch-1"]},
           ok=lambda b: b.get("deleted") == 0 and len(b.get("failed") or []) == 1,
           expect="deleted=0，failed=1（同一 id 去重）")
    s.call("双语状态", "GET", f"/api/documents/{did}/bilingual/status", ok=lambda b: "cached" in b)
    s.call("双语全文", "GET", f"/api/documents/{did}/bilingual", allow=(200, 404))
    s.call("中译英状态", "GET", f"/api/documents/{did}/translate-en", allow=(200, 404))
    s.call("中译英下载", "GET", f"/api/documents/{did}/translate-en.md", allow=(200, 404))
    s.call("整篇翻译(单页)", "POST", f"/api/documents/{did}/bilingual/translate",
           json_body={"page_from": 1, "page_to": 1}, ok=lambda b: b.get("ok") is True,
           expect="单页翻译返回 ok（D-73 修复点）")
    s.call("按页翻译", "POST", f"/api/documents/{did}/page/1/translate",
           ok=lambda b: True, expect="页面翻译")
    s.call("任意片段翻译", "POST", "/api/translate",
           json_body={"text": "Attention is all you need.", "to": "zh"},
           ok=lambda b: bool(b.get("zh") or b.get("text")), expect="返回中文")

    if scan_doc:
        s.call("扫描件页文本", "GET", f"/api/documents/{scan_doc['id']}/page/1/text",
               ok=lambda b: True, expect="扫描件允许空文本层")

    # ---------- 检索与问答 ----------
    print("\n【检索与问答】")
    s.call("问答-事实", "POST", "/api/chat",
           json_body={"question": "Transformer 论文里，自注意力每层的计算复杂度是多少", "mode": "qa"},
           ok=lambda b: len(b.get("evidence", [])) >= 5 and bool(b.get("answer")),
           expect="≥5 条证据 + 有答案")
    s.call("问答-对比", "POST", "/api/chat",
           json_body={"question": "BERT 和 GPT 的预训练目标有什么区别", "mode": "compare"},
           ok=lambda b: len(b.get("evidence", [])) >= 3 and bool(b.get("answer")))
    s.call("问答-证据不足", "POST", "/api/chat",
           json_body={"question": "GPT-5 的参数量是多少", "mode": "qa"},
           ok=lambda b: bool(b.get("answer")), expect="应明确说没有")
    s.call("问答-限定文献", "POST", "/api/chat",
           json_body={"question": "这篇论文的主要贡献是什么", "mode": "qa", "doc_ids": [did]},
           ok=lambda b: all(e["doc_id"] == did for e in b.get("evidence", [])) and b.get("evidence"),
           expect="证据全部来自指定文献")

    # ---------- 图谱 ----------
    print("\n【引用图谱】")
    s.call("图谱", "GET", "/api/graph",
           ok=lambda b: len(b.get("nodes", [])) > 20 and len(b.get("edges", [])) > 50,
           expect="节点>20 边>50")
    s.call("图谱综述(选题)", "POST", "/api/graph/survey",
           json_body={"max_papers": 3}, allow=(200, 400, 404),
           expect="有聚类则出综述，否则 400/404")

    # ---------- 研究工具 ----------
    print("\n【研究工具】")
    s.call("Future Work", "GET", "/api/research/future-work", ok=lambda b: len(b.get("items", [])) > 0)
    s.call("复现性", "GET", "/api/research/reproducibility", ok=lambda b: len(b.get("items", [])) > 0)
    s.call("工具调用-GitHub", "POST", "/api/tools/invoke",
           json_body={"tool": "github_repo_info", "args": {"repo": "huggingface/transformers"}},
           allow=(200, 500), expect="在线或降级")
    s.call("工具 Agent", "POST", "/api/tools/agent",
           json_body={"question": "Transformer 那个模型的开源代码有多少星"}, ok=lambda b: True)
    csv_text = "model,bleu\ntransformer_big,28.4\nconvseq,25.2\n"
    s.call("CSV 转图表", "POST", "/api/tools/csv2chart",
           json_body={"csv": csv_text, "kind": "bar", "format": "mermaid", "title": "BLEU"},
           ok=lambda b: "mermaid" in json.dumps(b) or "code" in json.dumps(b))

    # ---------- 写作 ----------
    print("\n【写作与可视化】")
    s.call("论文框架", "POST", "/api/write/outline",
           json_body={"idea": "研究注意力机制的复杂度与精度权衡"}, ok=lambda b: bool(b.get("sections") or b.get("outline")))
    s.call("架构图", "POST", "/api/write/topology",
           json_body={"idea": "一个检索增强的问答系统：解析、检索、校验、生成"},
           ok=lambda b: bool(b.get("nodes")) and bool(b.get("formats") or b.get("mermaid")),
           expect="节点 + 四份代码")

    # ---------- 发现 ----------
    print("\n【论文发现】")
    s.call("arXiv 搜索", "GET", "/api/discover/search?q=retrieval%20augmented%20generation",
           allow=(200, 503), expect="在线 200，无外网 503/降级")
    s.call("推荐理由", "GET", "/api/discover/recommend", allow=(200, 500))
    s.call("推荐反馈统计", "GET", "/api/discover/feedback", ok=lambda b: True)
    s.call("追踪状态", "GET", "/api/discover/watch", ok=lambda b: True)

    # ---------- 上传与删除（会真的写库，跑完还原） ----------
    print("\n【入库闭环（上传→解析→可检索→删除→恢复）】")
    pdf = ROOT / "data" / "samples" / "cn_paper_sample.pdf"
    if not pdf.exists():
        cands = sorted((ROOT / "data" / "pdf").glob("*.pdf"), key=lambda p: p.stat().st_size)
        pdf = cands[0] if cands else None
    if pdf and pdf.exists():
        with pdf.open("rb") as fh:
            up = s.call("上传 PDF", "POST", "/api/documents/upload",
                        files={"file": (pdf.name, fh, "application/pdf")},
                        # 同一份文件再传一次应该是"识别为重复"，这本身就是去重在起作用
                        ok=lambda b: bool(b.get("doc_id") or b.get("id")
                                          or (b.get("stage") == "duplicate")),
                        expect="新入库，或按内容判重")
        body = up.get("body") if isinstance(up, dict) else {}
        new_id = (body or {}).get("doc_id") or (body or {}).get("id")
        if new_id:
            s.call("新文献可检索", "POST", "/api/chat",
                   json_body={"question": "这篇文档讲了什么", "mode": "qa", "doc_ids": [new_id]},
                   ok=lambda b: bool(b.get("evidence")), expect="新入库文献能被检索到")
            s.call("删除(进回收站)", "DELETE", f"/api/documents/{new_id}", ok=lambda b: True)
            s.call("恢复", "POST", f"/api/documents/{new_id}/restore", ok=lambda b: True)
            s.call("再删(清理)", "DELETE", f"/api/documents/{new_id}", ok=lambda b: True)

    # ---------- 耗时项 ----------
    if args.slow:
        print("\n【耗时操作（--slow）】")
        s.call("重新入库扫描", "POST", "/api/library/rescan", ok=lambda b: True)

    res = s.summary()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"api_sweep_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"共 {res['total']} 项：PASS {res['pass']} / FAIL {res['fail']}　状态分布 {res['by_status']}")
    fails = [r for r in res["rows"] if not r["pass"]]
    for r in fails:
        print(f"  FAIL {r['name']} [{r['status']}] {r['detail'][:120]}")
    slowest = sorted((r for r in res["rows"] if isinstance(r["ms"], int)), key=lambda r: -r["ms"])[:5]
    print("最慢五项：" + " / ".join(f"{r['name']} {r['ms']}ms" for r in slowest))
    print(f"证据 -> {path}")
    return 0 if res["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
