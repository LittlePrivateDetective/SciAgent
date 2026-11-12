"""去重算法的体检：指纹一致性、区分度、场景矩阵、阈值敏感性。

为什么要专门检查：去重的错是"静默"的——判重了就说一句"内容高度重合"，
用户既看不到距离也算不出相似度，误判和漏判都只能靠事后发现（D-87 就是这样
过了两周才被撞出来）。所以这里不测"接口返回 200"，而是把算法本身摊开量：

1. **一致性**：用当前解析器重算 38 篇的指纹，与库里存的是否一致。
   不一致意味着"解析器一升级，老文献重传就会被当成新文献"——静默漏判。
2. **区分度**：全库两两汉明距离分布。如果不同论文的距离大量落在阈值内，
   说明这个阈值形同虚设，判重全靠标题兜底。
3. **场景矩阵**：同篇改版 / 节选 / 改标题 / 同领域不同篇 / 跨语言 / 极短文本，
   逐条给期望与实测。
4. **阈值敏感性**：标题门槛取 0.5/0.6/0.7/0.8 时，误判与漏判各是多少。

用法：
    ./.venv/Scripts/python.exe tests/dedup_matrix.py
    ./.venv/Scripts/python.exe tests/dedup_matrix.py --limit 12   # 只取前 12 篇，快速跑
证据：docs/qa/evidence/dedup_matrix_<时间>.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 直接跑 scripts/ 下的文件时要能 import app

from sqlalchemy import select

from app.db.models import Document
from app.db.session import SessionLocal
from app.parse.structure import parse_pdf
from app.rag.dedup import (SIMHASH_MAX_DIST, TITLE_NEAR, TITLE_SAME, content_hash,
                           family_key, find_duplicates, hamming, normalize, simhash64)

OUT = ROOT / "docs" / "qa" / "evidence"


@dataclass
class Doc:
    id: str
    filename: str
    title: str
    text: str
    simhash: int
    chash: str
    family: str
    arxiv_id: str = ""


def load_docs(limit: int = 0) -> tuple[list[Doc], list[dict]]:
    """解析每篇 PDF 取 body_text（与入库时用的是同一份文本），缓存到内存。"""
    db = SessionLocal()
    rows = list(db.scalars(select(Document)).all())
    db.close()
    if limit:
        rows = rows[:limit]
    out, notes = [], []
    for d in rows:
        src = Path(d.file_path)
        if not src.is_file():
            notes.append({"file": d.filename, "note": "源文件缺失，跳过"})
            continue
        t0 = time.time()
        try:
            parsed = parse_pdf(str(src))
            text = parsed.body_text()
            axid = parsed.arxiv_id or ""
        except Exception as exc:  # noqa: BLE001
            notes.append({"file": d.filename, "note": f"解析失败 {type(exc).__name__}: {exc}"})
            continue
        out.append(Doc(id=d.id, filename=d.filename, title=d.title or "",
                       text=text, simhash=d.simhash or 0, chash=d.content_hash or "",
                       family=d.family_key or "", arxiv_id=axid))
        notes.append({"file": d.filename, "chars": len(text),
                      "parse_s": round(time.time() - t0, 1)})
    return out, notes


def part1_consistency(docs: list[Doc]) -> dict:
    """重算指纹 vs 库里存的。"""
    drift, allrows = [], []
    for d in docs:
        sh = simhash64(d.text)
        ch = content_hash(d.text)
        fam = family_key(d.arxiv_id, d.title, d.text[:8000])
        # family 的"漂移"要分两种：
        #   a) 库里是 arxiv:/doi:，现在只算得出 title: —— 说明库里的编号来自入库时的
        #      另一条途径（文件名映射、批量导入清单），不是解析器漂移，判重时靠
        #      指纹与标题仍能命中，不算问题；
        #   b) 两边都是 arxiv:/doi: 但不一样，或两边都是 title: 但哈希不同 ——
        #      这才是真漂移，意味着同一篇重传会被当成新文献。
        fam_db, fam_now = d.family, fam
        kind_db = fam_db.split(":")[0] if fam_db else ""
        kind_now = fam_now.split(":")[0] if fam_now else ""
        fam_same = (not fam_db) or (not fam_now) or fam_db == fam_now or (
            kind_db in ("arxiv", "doi") and kind_now == "title")
        row = {"file": d.filename, "simhash_same": sh == d.simhash,
               "chash_same": ch == d.chash,
               "family_same": fam_same,
               "family_weaker_now": bool(kind_db in ("arxiv", "doi") and kind_now == "title"),
               "fam_db": fam_db, "fam_now": fam_now}
        allrows.append(row)
        if not (row["simhash_same"] and row["chash_same"] and row["family_same"]):
            row["dist"] = hamming(sh, d.simhash) if sh and d.simhash else None
            drift.append(row)
    return {"total": len(docs), "drift": len(drift), "rows": drift, "all": allrows}


def part2_separation(docs: list[Doc]) -> dict:
    """两两距离、标题相似度，以及现行规则下的误判。"""
    pairs = []
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            a, b = docs[i], docs[j]
            if not a.simhash or not b.simhash:
                continue
            dist = hamming(a.simhash, b.simhash)
            ratio = (SequenceMatcher(None, normalize(a.title), normalize(b.title)).ratio()
                     if a.title and b.title else 0.0)
            pairs.append({"dist": dist, "title": round(ratio, 3),
                          "a": a.filename, "b": b.filename,
                          "same_family": bool(a.family and a.family == b.family)})
    pairs.sort(key=lambda x: x["dist"])
    n = len(pairs)
    within = sum(1 for p in pairs if p["dist"] <= SIMHASH_MAX_DIST)
    # 现行规则：d<=阈值 且 标题>=门槛
    false_pos = [p for p in pairs
                 if p["dist"] <= SIMHASH_MAX_DIST and p["title"] >= TITLE_NEAR]
    # 只看标题也很像的对（不同篇）——这才是"标题门槛救不了"的隐患
    risky = sorted([p for p in pairs if p["dist"] <= SIMHASH_MAX_DIST],
                   key=lambda x: -x["title"])[:6]
    return {
        "pairs": n,
        "dist_le_6": within,
        "dist_le_6_pct": round(100.0 * within / max(n, 1), 1),
        "dist_hist": {str(k): sum(1 for p in pairs if p["dist"] == k) for k in range(0, 9)},
        "closest": pairs[:8],
        "false_positive_pairs": false_pos,
        "most_similar_titles": risky,
    }


def _judge(db, text: str, title: str, arxiv_id: str = "", exclude: str | None = None) -> dict:
    hits = find_duplicates(db, text=text, arxiv_id=arxiv_id, title=title,
                           exclude_doc_id=exclude)
    top = None
    if hits:
        h0 = hits[0]
        top = {"doc": h0.doc.filename, "reason": h0.reason, "sim": h0.similarity}
    return {"hit": bool(hits), "top": top, "count": len(hits)}


def load_outsiders(limit: int = 0) -> list[Doc]:
    """库外样本：_incoming 下那几份还没入库的 PDF。

    为什么必须有"库外"样本：去重是"新文本 vs 库里已有"的比较。拿库内文档去查，
    无论内容怎么改都会命中它自己（标题也一样），这种用例测不出任何东西 ——
    第一版就是这么写的，三条"不应判重"全红，其实全是自己撞自己。
    """
    out: list[Doc] = []
    inc = ROOT / "_incoming"
    if not inc.is_dir():
        return out
    for f in sorted(inc.glob("*.pdf")):
        try:
            parsed = parse_pdf(str(f))
        except Exception:  # noqa: BLE001
            continue
        out.append(Doc(id="", filename=f.name, title=parsed.title or "",
                       text=parsed.body_text(), simhash=0, chash="", family="",
                       arxiv_id=parsed.arxiv_id or ""))
        if limit and len(out) >= limit:
            break
    return out


def _rule_says_dup(a: Doc, b: Doc) -> tuple[bool, int, float]:
    """不入库，直接按现行规则判断"库里已有 a，再传 b 会不会判重"。"""
    dist = hamming(simhash64(a.text), simhash64(b.text))
    ratio = (SequenceMatcher(None, normalize(a.title), normalize(b.title)).ratio()
             if a.title and b.title else 1.0)
    return ((dist <= SIMHASH_MAX_DIST and ratio >= TITLE_NEAR) or ratio >= TITLE_SAME,
            dist, round(ratio, 3))


def part3_scenarios(docs: list[Doc], outsiders: list[Doc]) -> dict:
    db = SessionLocal()

    def pick(pool, *keys):
        for k in keys:
            for d in pool:
                if k in d.filename:
                    return d
        return None

    a = pick(docs, "1706.03762v1.pdf", "2005.14165", "N19-1423")
    zh_in = pick(docs, "cn_paper")
    long_doc = pick(docs, "UnderstandingDeepLearning")
    rag = pick(outsiders, "EN_1_RAG")
    retro = pick(outsiders, "EN_2_RETRO")
    zh1 = pick(outsiders, "ZH_1_")
    zh2 = pick(outsiders, "ZH_2_")

    results = []

    def add(name, expect, got, reason="", top="", note=""):
        results.append({"case": name, "expect_hit": expect, "got_hit": got,
                        "reason": reason, "top": top, "note": note,
                        "pass": (expect is None) or (got == expect)})

    # ── A. 同篇：用库内文档查库，命中它自己就是对的
    if a:
        paras = a.text.split("\n")

        def drop(frac):
            random.seed(42)
            return "\n".join([p for p in paras if random.random() > frac])

        for name, text, title, note in [
            ("同篇：原样再传", a.text, a.title, "应该走到「正文完全一致」"),
            ("同篇：删掉约 5% 段落", drop(0.05), a.title, "小改版仍应判重"),
            ("同篇：删掉约 30% 段落", drop(0.30), a.title, "大改版仍应判重"),
            ("同篇：只留前 50%（节选）", a.text[:len(a.text) // 2], a.title, "节选也算同篇"),
            ("同篇：标题换中文译名", a.text, "注意力就是你所需要的一切", "靠正文救回来"),
            ("同篇：标题加副标题", a.text, a.title + ": A Revisit", "副标题不该影响"),
            ("同篇：标题换成文件名式", a.text, "1706.03762v9.pdf", "文件名不能当同族依据"),
            ("同篇：空标题（扫描件）", a.text, "", "标题缺失时保守判重"),
            # 这一条是**故意放行**的：正文大改 + 标题全换（典型的中文译版），
            # 任何基于词形的指纹都认不出来。宁可多存一篇，也不要把新论文堵在门外。
            ("同篇：删 30% 段落 + 标题换译名（已知取舍）", drop(0.30), "注意力机制综述",
             "正文与标题都变，刻意放行；靠用户自己判断"),
        ]:
            expect = False if "已知取舍" in name else True
            r = _judge(db, text, title, "")
            add(name, expect, r["hit"], (r["top"] or {}).get("reason", ""),
                (r["top"] or {}).get("doc", ""), note)

    # ── B. 无关：库外新论文 vs 库内 38 篇（这是最贴近真实使用的场景）
    for d, note in [(rag, "检索增强方向，与库内 GPT-3/Llama 词汇高度重叠"),
                    (retro, "同上，且引用了库内多篇"),
                    (zh1, "中文综述 vs 全英文库"),
                    (zh2, "中文综述 vs 全英文库")]:
        if not d:
            continue
        r = _judge(db, d.text, d.title, d.arxiv_id)
        add(f"无关：{d.filename[:22]} vs 库内", False, r["hit"],
            (r["top"] or {}).get("reason", ""), (r["top"] or {}).get("doc", ""), note)

    # ── C. 无关：库内文档之间互查（排除自身后，38 篇里不该有第二篇重复）
    for d, note in [(zh_in, "中文样本 vs 37 篇英文"), (long_doc, "541 页书 vs 论文")]:
        if not d:
            continue
        r = _judge(db, d.text, d.title, d.arxiv_id, exclude=d.id)
        add(f"无关：{d.filename[:22]} vs 其余 37 篇", False, r["hit"],
            (r["top"] or {}).get("reason", ""), (r["top"] or {}).get("doc", ""), note)

    # ── D. 两两构造：库里放 a、再传 b 会不会误判（不入库，直接按规则算）
    for x, y, note in [(zh1, zh2, "两篇中文综述，同领域同体裁"),
                       (rag, retro, "两篇检索增强，同一年代"),
                       (rag, zh1, "中英各一篇，主题相近")]:
        if not (x and y):
            continue
        dup, dist, ratio = _rule_says_dup(x, y)
        add(f"两两：{x.filename[:16]} → {y.filename[:16]}", False, dup,
            f"d={dist} 标题={ratio}", "", note)
        dup2, dist2, ratio2 = _rule_says_dup(y, x)
        add(f"两两(反向)：{y.filename[:14]} → {x.filename[:14]}", False, dup2,
            f"d={dist2} 标题={ratio2}", "", "顺序不该影响结论")
        if dup != dup2:
            add("两两：正反向结论是否一致", True, False, "正反向结果不同", "", "对称性检查")

    db.close()
    return {"rows": results,
            "failed": [r for r in results if not r["pass"]],
            "undecided": [r for r in results if r["expect_hit"] is None]}


def part4_threshold(docs: list[Doc]) -> dict:
    """标题门槛取不同值时，能拦住多少"不同篇误判"。"""
    rows = []
    pairs = []
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            a_, b_ = docs[i], docs[j]
            if not a_.simhash or not b_.simhash:
                continue
            dist = hamming(a_.simhash, b_.simhash)
            if dist > SIMHASH_MAX_DIST:
                continue
            ratio = (SequenceMatcher(None, normalize(a_.title), normalize(b_.title)).ratio()
                     if a_.title and b_.title else 1.0)
            pairs.append((dist, ratio))
    for th in (0.5, 0.6, 0.7, 0.8, 0.9):
        rows.append({"title_threshold": th,
                     "would_flag_pairs": sum(1 for _, r in pairs if r >= th)})
    return {"within_6_pairs": len(pairs), "by_threshold": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只解析前 N 篇，快速跑")
    args = ap.parse_args()

    t0 = time.time()
    docs, notes = load_docs(args.limit)
    outsiders = load_outsiders()
    print(f"解析完成：库内 {len(docs)} 篇 + 库外样本 {len(outsiders)} 篇，"
          f"用时 {time.time() - t0:.1f}s\n")

    p1 = part1_consistency(docs)
    print("【1】指纹一致性（重算 vs 库内）")
    print(f"  {p1['total']} 篇中漂移 {p1['drift']} 篇")
    weaker = [r for r in p1.get("all", []) if r.get("family_weaker_now")]
    if weaker:
        print(f"  （另有 {len(weaker)} 篇：库里有 arXiv/DOI 编号，重新解析只能得标题哈希，"
              f"算信息更全而非漂移）")
        for r in weaker[:3]:
            print(f"    {r['file'][:36]:38s} {r['fam_db']} → {r['fam_now']}")
    for r in p1["rows"][:5]:
        print(f"    {r['file'][:34]:36s} simhash={r['simhash_same']} "
              f"chash={r['chash_same']} family={r['family_same']} d={r.get('dist')}")

    p2 = part2_separation(docs)
    print("\n【2】区分度（不同文档之间的距离）")
    print(f"  {p2['pairs']} 对里 d<=6 的占 {p2['dist_le_6']} 对（{p2['dist_le_6_pct']}%）")
    print(f"  距离直方 0..8：{p2['dist_hist']}")
    print(f"  现行规则（d<={SIMHASH_MAX_DIST} 且标题>={TITLE_NEAR}）会误判："
          f"{len(p2['false_positive_pairs'])} 对")
    print("  最近的几对（都应是不同论文）：")
    for p in p2["closest"][:5]:
        print(f"    d={p['dist']:2d} 标题相似={p['title']:.2f}  {p['a'][:28]:30s} | {p['b'][:28]}")
    if p2["most_similar_titles"]:
        print("  距离在阈值内、标题最像的几对（最接近误判的地方）：")
        for p in p2["most_similar_titles"][:5]:
            print(f"    标题={p['title']:.2f} 距离={p['dist']:2d}  "
                  f"{p['a'][:26]:28s} | {p['b'][:26]}")

    p3 = part3_scenarios(docs, outsiders)
    print("\n【3】场景矩阵")
    for r in p3["rows"]:
        mark = "OK " if r["pass"] else "FAIL"
        exp = {True: "应判重", False: "不应判重", None: "观察"}[r["expect_hit"]]
        print(f"  [{mark}] {r['case'][:34]:36s} {exp:6s} 实测={'判重' if r['got_hit'] else '放行'}"
              f"  {r['reason'][:38]}")

    p4 = part4_threshold(docs)
    print("\n【4】标题门槛敏感性（只看 d<=6 的 {} 对）".format(p4["within_6_pairs"]))
    for r in p4["by_threshold"]:
        print(f"  门槛 {r['title_threshold']}: 会判重 {r['would_flag_pairs']} 对")

    ok = (p1["drift"] == 0 and not p3["failed"]
          and len(p2["false_positive_pairs"]) == 0)
    print(f"\n当前规则：距离<={SIMHASH_MAX_DIST} 且标题>={TITLE_NEAR}，"
          f"或标题>={TITLE_SAME}")
    print(f"结论：{'通过' if ok else '有问题需要处理'}　"
          f"（一致性漂移 {p1['drift']}／场景失败 {len(p3['failed'])}／"
          f"区分度误判 {len(p2['false_positive_pairs'])}）")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"dedup_matrix_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "docs": len(docs),
         "consistency": p1, "separation": p2, "scenarios": p3, "thresholds": p4,
         "parse": notes}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("证据 ->", path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
