"""论文自动下载与推荐（官方 1.4）。

一条 arXiv 通道撑起三块能力，彼此复用、不各写一套：

- **1.4.1 基本下载**：给定标题检索 → 下载 PDF → 走既有 `ingest`（去重、解析、
  向量化、章节树、引用图全部原样复用）。另有「按参考文献补全」：把库内论文的
  References 条目逐条匹配 arXiv，按**正文被引次数**排序——只引一次的多半是
  "为引用而引用"，排后面，这正是官方要求的"筛掉不重要引用"。
- **1.4.2 追踪最新**：以库内主题词 + `submittedDate` 窗口查 arXiv，只留可能相关的新论文。
- **1.4.3 推荐**：相关性打分 → 模型逐篇给中文推荐理由 → 检测 GitHub 代码（有代码优先）
  → 赞/踩反馈改写后续排序。

为什么不用官方提到的 sickle：它只是 OpenSearch 的一层薄封装，项目里已经有
httpx + ElementTree 的 Atom 解析（`app/tools/external.py`），多一个依赖不划算，
而且这里需要日期窗口、标题精确匹配、批量并发这些更细的控制。
"""
from __future__ import annotations

import math
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import gateway
from app.core.logger import get_logger
from app.db.models import Chunk, Document, RecommendFeedback
from app.pipeline.ingest import ingest, save_upload

log = get_logger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_PDF = "https://arxiv.org/pdf/{id}"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# 泛词不进主题词表：它们出现在几乎每篇 ML 论文标题里，拿去做检索只会捞回一堆噪音
STOP = set("""
a an the of for and or to in on with via from by is are was were be been as at we our this that these those
using based towards toward into under over between through can may not new novel approach approaches method
methods model models result results paper study analysis large small its their it he she they
learning language neural network networks deep data training representation representations understanding
efficient image images graph graphs vision speech text texts token tokens towards good better
open report reports technical preliminary extended version preprint workshop findings
need needs want wants
""".split())
# 这些词把"专业主题"稀释成"人工智能"：库里 30 多篇都含 learning/language，
# 拿它们做 AND 检索会捞回一整片无关新论文（实测推荐出遥感、曲线拟合）。
# 真正的主题词应该是 transformer/attention/pretraining 这种能区分方向的名词。

# 批量导入是分钟级操作，同步返回会让前端超时；用进程内 job 表 + 轮询。
# 单进程 uvicorn 部署，进程重启丢进度只会发生在容器重启，可接受。
_JOBS: dict[str, dict] = {}
# 参考文献 → arXiv 匹配结果缓存：同一条 References 在多次点开时不必反复打 API
_REF_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------- arXiv 基础
def _fetch(params: dict, timeout: float = 25.0) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(ARXIV_API, params=params)
        resp.raise_for_status()
        return resp.text


def _parse(xml: str, limit: int) -> list[dict]:
    root = ET.fromstring(xml)
    out: list[dict] = []
    for e in root.findall("a:entry", NS)[:limit]:
        ident = (e.findtext("a:id", "", NS) or "").strip()
        m = re.search(r"abs/(.+?)(v\d+)?$", ident)
        aid = m.group(1) if m else ""
        if not aid:
            continue
        summary = " ".join((e.findtext("a:summary", "", NS) or "").split())
        comment = " ".join((e.findtext("arxiv:comment", "", NS) or "").split())
        out.append({
            "arxiv_id": aid,
            "title": " ".join((e.findtext("a:title", "", NS) or "").split()),
            "authors": [a.findtext("a:name", "", NS) for a in e.findall("a:author", NS)][:6],
            "published": (e.findtext("a:published", "", NS) or "")[:10],
            "updated": (e.findtext("a:updated", "", NS) or "")[:10],
            "summary": summary,
            "comment": comment,
            "category": (e.find("arxiv:primary_category", NS).get("term")
                         if e.find("arxiv:primary_category", NS) is not None else ""),
            "abs_url": f"https://arxiv.org/abs/{aid}",
            "pdf_url": ARXIV_PDF.format(id=aid),
        })
    return out


def search_arxiv(query: str, limit: int = 8, *, sort: str = "relevance") -> list[dict]:
    """按 arXiv 查询语法检索。query 为原始表达式（`ti:"..."` / `all:...` / 组合式）。"""
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max(1, min(limit, 40)),
        "sortBy": sort,
        "sortOrder": "descending" if sort == "submittedDate" else "descending",
    }
    try:
        return _parse(_fetch(params), limit)
    except Exception as exc:  # noqa: BLE001 外部服务抖动不能把整个页面打挂
        log.warning("arXiv 检索失败（%s）：%s", query[:60], exc)
        return []


def _clean_query_text(title: str) -> str:
    """标题里常带冒号、括号、连字符，直接塞进 `ti:"..."` 会让查询语法报错。"""
    t = re.sub(r"[^A-Za-z0-9\s\-]", " ", title or "")
    return " ".join(t.split())[:160]


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def download_pdf(arxiv_id: str) -> bytes:
    """下载 arXiv PDF，能处理"不带版本号就 404"的那些老论文。

    实测 `https://arxiv.org/pdf/1202.4918` 返回 **404**（不是重定向到最新版），
    而 `.../1202.4918v1` 是 200。新论文带不带版本号都能下，所以这个坑一直没暴露，
    直到有人在「论文发现」里点了 2012 年那篇《Quantum decision making by social agents》。
    现在按「原编号 → v1/v2/v3 → export 镜像」依次试，并校验真的拿到 PDF
    （arXiv 出错时会给一页 HTML，存下来会变成一篇打不开的文献）。
    """
    aid = (arxiv_id or "").strip()
    if not aid:
        raise ValueError("缺少 arXiv 编号")
    base = re.sub(r"v\d+$", "", aid, flags=re.I)
    cands = [aid]
    if not re.search(r"v\d+$", aid, flags=re.I):
        cands += [f"{base}v1", f"{base}v2", f"{base}v3"]

    tried: list[str] = []
    with httpx.Client(timeout=90, follow_redirects=True,
                      headers={"User-Agent": "SciAgent/1.0 (+arxiv-pdf-fetch)"}) as client:
        for cid in cands:
            for host in ("https://arxiv.org/pdf", "https://export.arxiv.org/pdf"):
                url = f"{host}/{cid}"
                try:
                    resp = client.get(url)
                except Exception as exc:  # noqa: BLE001 网络异常换下一个地址
                    tried.append(f"{url} → {type(exc).__name__}")
                    continue
                if resp.status_code >= 400:
                    tried.append(f"{url} → HTTP {resp.status_code}")
                    continue
                data = resp.content
                if not data.startswith(b"%PDF-"):
                    tried.append(f"{url} → 不是 PDF（{resp.headers.get('content-type')}）")
                    continue
                log.info("arXiv %s 下载成功：%s（%d KB）", aid, url, len(data) // 1024)
                return data
    raise ValueError("no-pdf：" + "；".join(tried[:6]))


def _base_id(aid: str) -> str:
    """arXiv 检索返回 `1706.03762`，库里存的却可能是 `1706.03762v1`，比对前统一去版本号。"""
    return re.sub(r"v\d+$", "", (aid or "").strip(), flags=re.I)


def library_index(db: Session) -> dict[str, Document]:
    """base_id → Document。库内 37 篇里有 5 篇 `arxiv_id` 字段为空（扫描件/手工导入），
    编号只写在文件名里，所以字段为空时再从文件名捞一次——否则"已在库"永远标不出来。"""
    out: dict[str, Document] = {}
    for d in db.scalars(select(Document)).all():
        keys = [_base_id(d.arxiv_id)]
        m = re.search(r"(\d{4}\.\d{4,5})", d.filename or "")
        if m:
            keys.append(m.group(1))
        for k in keys:
            if k:
                out.setdefault(k, d)
    return out


# ---------------------------------------------------------------- 1.4.1 下载入库
def import_paper(db: Session, arxiv_id: str, title: str = "", *, force: bool = False) -> dict:
    """下载一篇 arXiv 论文并走完整入库流水线。"""
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return {"ok": False, "stage": "input", "error": "缺少 arXiv 编号"}

    exists = library_index(db).get(_base_id(arxiv_id))
    if exists and not force:
        return {
            "ok": False, "stage": "exists", "doc_id": exists.id,
            "title": exists.title or exists.filename,
            "message": f"库里已有这篇（{exists.filename}），无需重复下载",
        }

    t0 = time.time()
    try:
        data = download_pdf(arxiv_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("下载 arXiv %s 失败：%s", arxiv_id, exc)
        detail = str(exc)
        if detail.startswith("no-pdf"):
            # 这不是网络问题，是 arXiv 上确实没有可下的 PDF（撤稿/仅摘要/编号写错）。
            # 把"重试也没用"和"网络抖动"分开说，否则用户会一直点重试。
            return {"ok": False, "stage": "download",
                    "error": f"arXiv 上没有 {arxiv_id} 的公开 PDF。可以点「arXiv 页面」自己下载后再上传。"}
        return {"ok": False, "stage": "download", "error": f"下载失败：{detail[:160]}"}

    slug = re.sub(r"[^A-Za-z0-9]+", "_", (title or arxiv_id))[:60].strip("_") or arxiv_id
    path = save_upload(data, f"{arxiv_id}_{slug}.pdf")
    try:
        res = ingest(db, path, path.name, force=force)
    except Exception as exc:  # noqa: BLE001
        log.exception("入库失败：%s", arxiv_id)
        return {"ok": False, "stage": "ingest", "error": str(exc)[:200]}

    res["arxiv_id"] = arxiv_id
    res["bytes"] = len(data)
    res["ms"] = int((time.time() - t0) * 1000)
    # ingest 自带去重：库里存在同族（如已有 v7 再来 v1）时返回 stage=duplicate，
    # 这不是失败，原样透传给前端让它弹"覆盖 / 保留"选择
    if res.get("ok"):
        res["title"] = res.get("title") or title or arxiv_id
    return res


def create_job(arxiv_ids: list[str], titles: dict[str, str] | None = None) -> str:
    """只建任务、立刻返回 id；真正下载由后台任务逐个跑。

    单篇入库 10–30 秒，8 篇串行要 3 分钟——同步返回必然超时。
    接口层用 `BackgroundTasks` 挂 `run_job`，前端拿 job_id 轮询进度。
    """
    job_id = uuid.uuid4().hex[:12]
    queue = [a for a in dict.fromkeys(arxiv_ids or []) if a][:20]
    _JOBS[job_id] = {
        "id": job_id, "queue": queue, "titles": titles or {},
        "total": len(queue), "done": 0, "ok": 0, "failed": 0,
        "current": "", "items": [], "state": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    return job_id


def run_job(job_id: str) -> None:
    """后台执行批量下载。必须自己开 session：请求的 db 在响应结束时已关闭。"""
    from app.db.session import session_scope

    job = _JOBS.get(job_id)
    if not job:
        return
    titles: dict[str, str] = job.get("titles") or {}
    db = session_scope()
    try:
        for aid in job.get("queue") or []:
            job["current"] = titles.get(aid) or aid
            try:
                r = import_paper(db, aid, titles.get(aid, ""))
            except Exception as exc:  # noqa: BLE001 单篇失败不能拖垮整批
                r = {"ok": False, "stage": "exception", "error": str(exc)[:160]}
            job["items"].append({
                "arxiv_id": aid, "ok": bool(r.get("ok")),
                "stage": r.get("stage", ""), "error": r.get("error", ""),
                "doc_id": r.get("doc_id", ""), "title": r.get("title", "") or titles.get(aid, ""),
            })
            job["done"] += 1
            job["ok" if r.get("ok") else "failed"] += 1
    finally:
        db.close()
    job["state"] = "done"
    job["current"] = ""


def job_status(job_id: str) -> dict:
    return _JOBS.get(job_id) or {"state": "missing", "total": 0, "done": 0, "items": []}


# ---------------------------------------------------------------- 1.4.1 参考文献补全
def _bracket_counts(db: Session, doc_id: str) -> dict[str, int]:
    """统计正文里 `[n]` 出现次数——这是"哪条参考文献真的重要"最便宜的信号。"""
    counts: dict[str, int] = defaultdict(int)
    rows = db.execute(
        select(Chunk.text).where(Chunk.doc_id == doc_id, Chunk.kind != "reference")
    ).all()
    for (text,) in rows:
        for m in re.finditer(r"\[(\d{1,3})(?:[,\]–\-]|$)", text or ""):
            counts[m.group(1)] += 1
    return counts


def _guess_title(entry: str) -> str:
    """从一条 References 条目里猜标题。

    覆盖两种最常见的排版（英文论文 + GB/T 7714）：
      Vaswani A, et al. Attention is all you need[C]//NeurIPS. 2017.
      Ashish Vaswani, Noam Shazeer, ... Attention is all you need. In NeurIPS, 2017.
    判据：按句号切段后，跳过会议/期刊开头、纯年份、作者列表（逗号多或含 et al），
    剩下的取最长一段——标题几乎总是最长且不含机构名的那一段。
    """
    parts = [p.strip() for p in re.split(r"[.。]\s+|\.$", entry or "") if p.strip()]
    best = ""
    for p in parts:
        low = p.lower()
        if re.match(r"^(in |proceedings|advances|arxiv|ieee|acm|conference|journal|corr|preprint)", low):
            continue
        if re.fullmatch(r"(19|20)\d\d[a-z]?", p.strip()):
            continue
        if "et al" in low or p.count(",") >= 3:
            continue
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", p) if w.lower() not in STOP]
        if len(words) < 3:
            continue
        if len(p) > len(best):
            best = p
    return best.strip()[:180]


def _match_reference(entry: str) -> dict | None:
    """把一条参考文献匹配到 arXiv：先看条目里有没有写 arXiv 编号，再退回标题检索。"""
    key = entry[:220]
    if key in _REF_CACHE:
        return _REF_CACHE[key]

    m = re.search(r"arxiv[:\s]*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})", entry or "", re.I)
    title = _guess_title(entry)
    if m:
        out = {"arxiv_id": m.group(1), "title": title, "via": "arxiv_id"}
        _REF_CACHE[key] = out
        return out

    q = _clean_query_text(title)
    if len(q) < 12:
        _REF_CACHE[key] = None
        return None
    hits = search_arxiv(f'ti:"{q}"', 3)
    for h in hits:
        if _sim(title, h["title"]) >= 0.72:
            out = {"arxiv_id": h["arxiv_id"], "title": h["title"] or title, "via": "title_search"}
            _REF_CACHE[key] = out
            return out
    _REF_CACHE[key] = None
    return None


def reference_candidates(db: Session, doc_id: str, limit: int = 8, min_cites: int = 2) -> dict:
    """库内论文的参考文献 → 按正文被引次数排序 → 标注哪些能一键补全。"""
    doc = db.get(Document, doc_id)
    if not doc:
        return {"ok": False, "error": "文档不存在", "items": []}

    counts = _bracket_counts(db, doc_id)
    refs = db.scalars(
        select(Chunk).where(Chunk.doc_id == doc_id, Chunk.kind == "reference").order_by(Chunk.idx)
    ).all()
    parsed: list[dict] = []
    for c in refs:
        m = re.match(r"\s*\[(\d{1,3})\]", c.text or "")
        idx = m.group(1) if m else ""
        parsed.append({
            "index": idx,
            "text": (c.text or "").strip(),
            "cites": counts.get(idx, 0),
        })
    if not parsed:
        return {"ok": True, "doc_id": doc_id, "items": [], "note": "这篇没有解析出参考文献条目"}

    # 被引次数高的排前面：正文提了 7 次的条目，比只在 Related Work 里出现一次的更值得补
    parsed.sort(key=lambda x: (-x["cites"], int(x["index"] or 999)))

    lib_idx = library_index(db)
    lib_titles = [(d.title or "", d.id) for d in db.scalars(select(Document)).all()]

    picked = [p for p in parsed if p["cites"] >= min_cites][:limit]
    if len(picked) < limit:
        # 被引都只有 1 次时不要交白卷：补足名额，但仍然排在被引多的后面
        picked += [p for p in parsed if p not in picked][: limit - len(picked)]

    def work(item: dict) -> dict:
        entry = item["text"]
        base = {
            "index": item["index"], "cites": item["cites"],
            "text": entry[:300], "title": _guess_title(entry),
            "arxiv_id": "", "in_library": False, "doc_id": "", "via": "",
        }
        m = re.search(r"arxiv[:\s]*([a-z\-]+/\d{7}|\d{4}\.\d{4,5})", entry, re.I)
        if m and m.group(1) in lib_idx:
            d = lib_idx[m.group(1)]
            base.update({"arxiv_id": m.group(1), "in_library": True,
                         "doc_id": d.id, "title": d.title or base["title"]})
            return base
        # 库内已有同题（哪怕没写 arXiv 编号）
        for t, did in lib_titles:
            if t and _sim(t, base["title"]) >= 0.8:
                base.update({"in_library": True, "doc_id": did, "title": t})
                return base
        hit = _match_reference(entry)
        if hit:
            base.update({"arxiv_id": hit["arxiv_id"], "via": hit["via"],
                         "title": hit["title"] or base["title"]})
            base["in_library"] = hit["arxiv_id"] in lib_idx
        return base

    # 4 路并发：每条要打一次 arXiv 标题检索，串行 8 条要 30 秒以上
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(work, picked))

    matched = sum(1 for i in items if i["arxiv_id"])
    todo = [i for i in items if i["arxiv_id"] and not i["in_library"]]
    return {
        "ok": True, "doc_id": doc_id, "doc_title": doc.title or doc.filename,
        "total_refs": len(parsed), "matched": matched,
        "downloadable": len(todo),
        "items": items,
    }


# ---------------------------------------------------------------- 1.4.2 / 1.4.3 追踪与推荐
def topics_from_library(db: Session, doc_id: str = "", limit: int = 5) -> list[str]:
    """从标题里提主题词：选中文档优先（3 倍权重），否则全库词频。"""
    docs = db.scalars(select(Document)).all()
    if not docs:
        return []
    counter: Counter[str] = Counter()
    for d in docs:
        words = {w for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", (d.title or "").lower())
                 if w not in STOP and len(w) > 3}
        weight = 3 if doc_id and d.id == doc_id else 1
        counter.update({w: weight for w in words})

    # transformer / transformers 这类单复数会被算成两个主题，占掉一半名额，
    # 而且 `abs:"transformers"` 检索不到只写 transformer 的论文——统一折回单数。
    out: list[str] = []
    for w, _ in counter.most_common(limit * 4):
        base = w[:-1] if w.endswith("s") and len(w) > 5 else w
        if base in out:
            continue
        out.append(base)
        if len(out) >= limit:
            break
    return out


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z\-]{2,}", (text or "").lower()) if w not in STOP]


def _interest_profile(db: Session, doc_id: str = "") -> tuple[dict[str, float], dict[str, float]]:
    """库内兴趣画像：全部文献（标题 + 摘要）的 TF-IDF 质心。

    为什么不用"命中几个主题词"：命中 `transformer` 的论文里有一大半是拿它做
    医学影像、WiFi 定位的应用文，词命中了但方向完全不同（实测推荐出了 fMRI
    音频扩散模型）。TF-IDF 质心比较的是**词的重要性分布**，泛词权重自动被压掉，
    这才是"跟库里论文相关"的可用近似，而且纯本地计算、零调用成本。
    """
    docs = db.scalars(select(Document)).all()
    texts = [((d.title or "") + " " + (d.abstract or "")) for d in docs]
    if not texts:
        return {}, {}
    if doc_id:
        seed = [t for d, t in zip(docs, texts) if d.id == doc_id]
        texts = texts + seed * 3          # 用户正在读的那篇，权重放大
    df: Counter[str] = Counter()
    for t in texts:
        df.update(set(_tokens(t)))
    n = len(texts)
    idf = {w: math.log((1 + n) / (1 + c)) + 1.0 for w, c in df.items()}

    vec: dict[str, float] = defaultdict(float)
    for t in texts:
        tf = Counter(_tokens(t))
        total = sum(tf.values()) or 1
        for w, c in tf.items():
            vec[w] += (c / total) * idf[w]
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {w: v / norm for w, v in vec.items()}, idf


def _cosine(text: str, profile: dict[str, float], idf: dict[str, float]) -> float:
    tf = Counter(_tokens(text))
    if not tf or not profile:
        return 0.0
    total = sum(tf.values())
    v = {w: (c / total) * idf.get(w, 1.0) for w, c in tf.items() if w in profile}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return sum(profile.get(w, 0.0) * x for w, x in v.items()) / norm


def _gen_reasons(items: list[dict], topics: list[str], lib_titles: list[str]) -> dict[str, str]:
    """让模型给每篇写一句中文推荐理由；失败就退回模板，不让推荐页空白。"""
    if not items:
        return {}
    payload = [{"id": i["arxiv_id"], "title": i["title"], "summary": i["summary"][:280]}
               for i in items]
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": (
                    "你是科研文献推荐助手。用户库里已有的研究方向关键词：" + "、".join(topics) +
                    "。请为给定的候选论文各写一句中文推荐理由，30–45 字，必须具体："
                    "说明它可能解决了什么问题、与用户哪个方向相关；"
                    "理由必须落到候选摘要中的具体方法、任务或数据集，不得只凭 transformer/scaling "
                    "等泛词建立关联；不要空话（如“值得一读”），不要编造实验结果。"
                    "严格输出 JSON：{\"reasons\":[{\"id\":\"...\",\"reason\":\"...\"}]}"
                )},
                {"role": "user", "content": __import__("json").dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.3,
            max_tokens=1200,
            model=settings.write_model,
        )
        out = {}
        for r in obj.get("reasons") or []:
            rid = str(r.get("id") or "").strip()
            rsn = str(r.get("reason") or "").strip()
            if rid and rsn:
                out[rid] = rsn[:120]
        return out
    except Exception as exc:  # noqa: BLE001 理由生成失败不该让推荐页失败
        log.warning("推荐理由生成失败：%s", exc)
        out = {}
        for i in items:
            hit = [t for t in topics if t in (i["title"] + " " + i["summary"]).lower()][:2]
            out[i["arxiv_id"]] = (
                f"候选论文研究“{i['title']}”；摘要所述任务与库内"
                f"{('、'.join(hit) if hit else topics[0] if topics else '相关主题')}方法方向直接相关。"
            )
        return out


def _semantic_scores(profile_text: str, items: list[dict]) -> list[float]:
    """库内摘要画像与候选摘要的向量余弦；与词法相关度共同把关。"""
    if not items:
        return []
    from app.rag.embed import embed_texts
    mat = embed_texts([profile_text[:5500]] + [f"{i['title']} {i['summary']}" for i in items])
    return [float(x) for x in (mat[1:] @ mat[0])]


_DOMAIN_RULES = {
    "nlp": re.compile(r"natural language|language model|question answering|\bNLP\b|\bLLM\b|"
                      r"\bBERT\b|\bGPT\b|pre-?train|fine-?tun|translation|reasoning|instruction|text generation", re.I),
    "vision": re.compile(r"\bvision\b|visual|image|object|recognition|segmentation|multimodal|video", re.I),
    "rl": re.compile(r"reinforcement|reward model|policy|actor.critic|decision transformer|robot|control", re.I),
    "speech": re.compile(r"speech|audio|spoken|whisper", re.I),
    "graph": re.compile(r"knowledge graph|graph neural|entity relation", re.I),
    "optimization": re.compile(r"optimizer|optimization|\bAdam\b|normalization|gradient|scaling law", re.I),
    "systems": re.compile(r"attention.*(?:memory|IO|efficient)|quantization|low.rank|LoRA|"
                          r"distributed training|memory optimization|compute.optimal|inference latency", re.I),
}


_NEGATIVE_DOMAIN = re.compile(
    r"tennis|sport|next-shot|temporal graph|time-series|neuroscience|pyramidal|"
    r"biological|neuromorphic|remote sensing|fMRI|WiFi", re.I,
)


def _domains(text: str) -> set[str]:
    """具体任务/方法领域；Transformer、attention、scaling 泛词不单独成域。"""
    return {name for name, rule in _DOMAIN_RULES.items() if rule.search(text or "")}


def _disliked(db: Session) -> set[str]:
    rows = db.scalars(
        select(RecommendFeedback.arxiv_id).where(RecommendFeedback.action == "dislike")
    ).all()
    return {r for r in rows}


def _liked(db: Session) -> set[str]:
    rows = db.scalars(
        select(RecommendFeedback.arxiv_id).where(RecommendFeedback.action == "like")
    ).all()
    return {r for r in rows}


def recommend(db: Session, limit: int = 6, days: int = 180, doc_id: str = "") -> dict:
    """按库内兴趣主题找近期 arXiv 新论文，附推荐理由与 GitHub 代码标记。"""
    t0 = time.time()
    topics = topics_from_library(db, doc_id)
    if not topics:
        return {"ok": False, "error": "库里还没有文献，先导入几篇再推荐", "items": []}

    docs = db.scalars(select(Document)).all()
    lib_titles = [d.title or d.filename for d in docs if (d.title or d.filename)]
    profile, idf = _interest_profile(db, doc_id)
    lib_idx = library_index(db)
    disliked, liked = _disliked(db), _liked(db)

    end = datetime.now()
    start = end - timedelta(days=max(7, min(days, 3650)))
    window = f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"

    # 三级检索，一级比一级宽，但**每一级都做本地关键词校验**：
    # arXiv 的 `all:"a" OR all:"b"` 配上 sortBy=submittedDate 时，实测会把最近
    # 提交的整片论文倒出来（第一版推荐出了遥感图像、B 样条曲线这种完全无关的），
    # 所以宁可少推，也不能把噪音当推荐。
    items: list[dict] = []
    seen: set[str] = set()
    plans = [
        (" AND ".join(f'abs:"{t}"' for t in topics[:2]), 2, True),   # 精度优先
        (" OR ".join(f'all:"{t}"' for t in topics[:3]), 2, True),    # 放宽到或，但仍要命中 2 词
        (" OR ".join(f'all:"{t}"' for t in topics[:2]), 1, False),   # 不限日期，至少命中 1 词
    ]
    for q, need, use_window in plans:
        full = f"({q}) AND {window}" if use_window else q
        got = search_arxiv(full, limit=limit * 4,
                           sort="submittedDate" if use_window else "relevance")
        for g in got:
            if g["arxiv_id"] in seen:
                continue
            text = (g["title"] + " " + g["summary"]).lower()
            if sum(1 for t in topics if t in text) < need:
                continue
            seen.add(g["arxiv_id"])
            items.append(g)
        if len(items) >= limit * 2:
            break
    if not items:
        return {"ok": True, "topics": topics, "items": [], "ms": int((time.time() - t0) * 1000),
                "note": "近期 arXiv 没有命中主题词的新论文，可加大时间窗口或换一篇文献做种子"}

    fresh: list[dict] = []
    lib_domains = set().union(*(_domains(f"{d.title or ''} {d.abstract or ''}") for d in docs)) if docs else set()
    for it in items:
        if it["arxiv_id"] in lib_idx or it["arxiv_id"] in disliked:
            continue
        # 标题表达主任务；摘要里的“受语言模型启发”只是背景。标题无法
        # 判断领域时宁可不推荐，避免 PDE/体育/时序图等靠背景词混入。
        if _NEGATIVE_DOMAIN.search(it["title"]):
            continue
        it["domains"] = sorted(_domains(it["title"]))
        if not set(it["domains"]) & lib_domains:
            continue
        it["github"] = ""
        m = re.search(r"github\.com/([\w.-]+/[\w.-]+)",
                      (it["summary"] + " " + it["comment"]).replace(" ", ""))
        if m:
            it["github"] = m.group(1).rstrip(".,)")
        it["relevance"] = round(_cosine(it["title"] + " " + it["summary"], profile, idf) * 10, 2)
        it["liked"] = it["arxiv_id"] in liked
        it["in_library"] = False
        fresh.append(it)

    # 有代码的加分而不是硬排序：官方要求"有代码优先推荐"，但一条遥感论文有 GitHub
    # 也不该压过相关性 6.0 的 transformer 论文，所以是加权、不是分档。
    for it in fresh:
        it["score"] = round(it["relevance"] + (0.8 if it["github"] else 0.0)
                            + (0.5 if it["liked"] else 0.0), 2)
    fresh.sort(key=lambda x: x["score"], reverse=True)
    # 词法与摘要语义双门槛。实测网球负例 semantic=0.376、遥感负例=0.412，
    # 长文档 RAG 正例=0.499；低于阈值时宁可返回空，不再回退到弱相关候选。
    profile_text = " ".join(f"{d.title or ''} {(d.abstract or '')[:240]}" for d in docs)
    semantic = _semantic_scores(profile_text, fresh)
    for it, score in zip(fresh, semantic):
        it["semantic"] = round(score, 4)
    strong = [it for it in fresh if it["relevance"] >= 0.8 and it.get("semantic", 0.0) >= 0.45]
    fresh = strong[:limit]

    reasons = _gen_reasons(fresh, topics, lib_titles)
    for it in fresh:
        it["reason"] = reasons.get(it["arxiv_id"], "")
        it["summary"] = it["summary"][:320]

    return {
        "ok": True,
        "topics": topics,
        "window_days": days,
        "items": fresh,
        "ms": int((time.time() - t0) * 1000),
        "source": "arxiv.org",
    }


def feedback(db: Session, arxiv_id: str, action: str, reason: str = "", actor: str = "") -> dict:
    """赞/踩：写入后立即影响下一次推荐的排序与过滤。"""
    action = (action or "").lower().strip()
    if action not in {"like", "dislike", "click"}:
        return {"ok": False, "error": "action 只能是 like / dislike / click"}
    if not arxiv_id:
        return {"ok": False, "error": "缺少 arXiv 编号"}
    db.add(RecommendFeedback(arxiv_id=arxiv_id, action=action,
                             reason=reason[:300], actor=actor))
    db.commit()
    return {"ok": True, "arxiv_id": arxiv_id, "action": action}


def feedback_stats(db: Session) -> dict:
    rows = db.execute(
        select(RecommendFeedback.action, RecommendFeedback.arxiv_id)
    ).all()
    by_action: dict[str, set[str]] = defaultdict(set)
    for action, aid in rows:
        by_action[action].add(aid)
    return {
        "like": len(by_action["like"]),
        "dislike": len(by_action["dislike"]),
        "click": len(by_action["click"]),
        "disliked_ids": sorted(by_action["dislike"]),
        "liked_ids": sorted(by_action["like"]),
    }
