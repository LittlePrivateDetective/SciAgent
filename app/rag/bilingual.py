"""中英对照双向联动阅读。

三个关键决策（答辩要讲清楚）：

1. **对齐单元复用 chunks**，不另建一套段落切分。
   译文和答案引用共用同一个 chunk_id → 同一个 `[p3@x0,y0,x1,y1]` 锚点。
   如果为了"看着顺眼"单独切段落，用户在译文里读到的一句话就没法点回原文的同一行，
   对照阅读和锚点溯源会变成两套互不认账的坐标系。

2. **翻译走独立的非推理模型**（默认 qwen-plus）。
   主模型 deepseek-v4.1-flash 带思维链，输出预算被推理吃掉——
   实测让它"只输出译文"会大面积返回空串。翻译这件事不需要推理，只需要忠实。

3. **按页懒翻译 + 落库缓存**。
   19766 个块全量预译在成本和时间上都不可接受；用户滚到哪译到哪，
   已译段落写进 translations 表，二次打开是 0 成本。
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMError, gateway
from app.core.logger import get_logger
from app.db.models import Chunk, Document, Translation
from app.db.session import session_scope

log = get_logger(__name__)

# 整篇翻译的后台任务进度（doc_id -> {total, done, running, error}）
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------- 段落重建
def _strip_carry(prev: str, cur: str, limit: int = 200) -> str:
    """去掉分块时的重叠前缀。

    切块为了"答案不被切在句子中间"，会把上一块的尾巴 80 字符贴到下一块开头
    （见 chunker._tail_overlap）。检索时这是优点，阅读时这是重影：
    用户会看到 "ollectively contribute to..." 这种从词中间开始的段落。

    这里用"上一块结尾 == 本块开头"做精确还原——比任何启发式都可靠，
    因为那段文字本来就是从上块末尾复制过来的。
    """
    if not prev or not cur:
        return cur
    best = 0
    for k in range(min(limit, len(cur)), 12, -1):
        if prev.endswith(cur[:k]):
            best = k
            break
    if not best:
        return cur
    rest = cur[best:].lstrip("\n ")
    # 重叠恰好卡在词中间时，rest 会以半截单词开头（如 "ned from ..."），
    # 丢掉这个残词：它在前一段已经完整出现过。
    m = re.match(r"^([a-z]{1,6})\s+", rest)
    if m and prev.endswith(m.group(1)):
        rest = rest[m.end():]
    return rest or cur


def _load_chunks(db: Session, doc_id: str, page_from: int, page_to: int) -> list[Chunk]:
    return list(
        db.scalars(
            select(Chunk)
            .where(Chunk.doc_id == doc_id, Chunk.page >= page_from, Chunk.page <= page_to)
            .order_by(Chunk.page, Chunk.idx)
        ).all()
    )


def _prev_text(db: Session, chunk: Chunk) -> str:
    """取排序上的前一块文本，用于剥离重叠前缀。"""
    return (
        db.scalars(
            select(Chunk.text)
            .where(
                Chunk.doc_id == chunk.doc_id,
                (Chunk.page < chunk.page) | ((Chunk.page == chunk.page) & (Chunk.idx < chunk.idx)),
            )
            .order_by(Chunk.page.desc(), Chunk.idx.desc())
            .limit(1)
        ).first()
        or ""
    )


def _with_prev(db: Session, chunks: list[Chunk]) -> list[tuple[Chunk, str]]:
    """给整段区间配上前一块文本。

    只对区间的第一块查一次库，其余靠列表顺序推进——
    逐块查上一次 `_prev_text` 在整篇翻译时是 N 次全表倾向查询，会明显拖慢。
    """
    if not chunks:
        return []
    carry = _prev_text(db, chunks[0])
    out: list[tuple[Chunk, str]] = []
    for c in chunks:
        out.append((c, carry))
        carry = c.text
    return out


def build_segments(db: Session, doc_id: str, page_from: int, page_to: int) -> list[dict]:
    """按页取段落（英文 + 已缓存的译文），保持文档阅读顺序。"""
    chunks = _load_chunks(db, doc_id, page_from, page_to)
    if not chunks:
        return []

    zh_map = {
        t.chunk_id: t.zh
        for t in db.scalars(
            select(Translation).where(Translation.chunk_id.in_([c.id for c in chunks]))
        ).all()
    }

    segs: list[dict] = []
    for c, prev in _with_prev(db, chunks):
        segs.append(
            {
                "chunk_id": c.id,
                "page": c.page,
                "bbox": c.bbox,
                "anchor": c.anchor,
                "kind": c.kind,
                "section": c.section_path,
                "en": _strip_carry(prev, c.text),
                "zh": zh_map.get(c.id, ""),
            }
        )
    return segs


# ---------------------------------------------------------------- 翻译
_SYS = (
    "你是学术论文翻译。输入是一个 JSON 数组，每个元素是一段英文；"
    "请逐段译成规范学术中文，输出 JSON：{\"zh\": [\"第一段译文\", \"第二段译文\", ...]}。\n"
    "规则：\n"
    "1. 输出的段数必须与输入**完全一致**，一段对一段，不合并不拆分；\n"
    "2. 译文里不要带原文序号或任何标记；\n"
    "3. 术语准确，首次出现的核心术语可在括号内保留英文原词；\n"
    "4. 数字、公式、变量名、引用编号（如 [27]）原样保留；\n"
    "5. 图注译成「图 N：…」，表注译成「表 N：…」；\n"
    "6. 不省略、不总结、不添加原文没有的内容。"
)

# 模型偶尔会把输入序号带进译文（"[1] Mistral 7B 采用…"）
_RE_LEAD_NUM = re.compile(r"^\s*[\[\(（]?\d{1,2}[\]\)）]?[.、:：]?\s+")
# 个别段落在传入前已经被上一段的重叠尾句污染
_RE_WS = re.compile(r"[ \t]{2,}")


def _clean_zh(text: str) -> str:
    return _RE_WS.sub(" ", _RE_LEAD_NUM.sub("", str(text).strip())).strip()


def _translate_batch(texts: list[str]) -> list[str]:
    """一次译若干段，返回严格等长的中文列表；失败返回空列表由调用方降级。"""
    import json as _json

    chars = sum(len(t) for t in texts)
    # 中文输出 token ≈ 英文词数 × 1.5，按字符粗略折算并留 1.4 倍余量
    budget = min(6000, 400 + int(chars * 1.5))
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": _json.dumps(texts, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=budget,
            model=settings.trans_model,
        )
    except LLMError as exc:
        log.warning("翻译请求失败：%s", exc)
        return []

    arr = obj.get("zh") or obj.get("data") or obj.get("translations") or []
    if isinstance(arr, dict):
        # 有的模型会返回 {"1": "...", "2": "..."}
        arr = [arr[k] for k in sorted(arr, key=lambda x: str(x))]
    if isinstance(arr, list) and len(texts) == 1 and len(arr) > 1:
        # 单段被模型拆成多段：这是"一大段里有表格/公式被当成分节"，
        # 直接拼回去，比丢掉重试快得多。
        arr = ["\n".join(str(x) for x in arr)]
    if not isinstance(arr, list) or len(arr) != len(texts):
        # 段数对不上就丢弃整批：错位的中英对照比没有对照更糟，
        # 用户会照着错的译文去理解原文。
        log.warning("译文段数不匹配：期望 %d 实得 %s", len(texts), len(arr) if isinstance(arr, list) else type(arr))
        return []
    return [_clean_zh(x) for x in arr]


def _save(db: Session, doc_id: str, pairs: list[tuple[Chunk, str]]) -> int:
    if not pairs:
        return 0
    for chunk, zh in pairs:
        db.merge(
            Translation(
                chunk_id=chunk.id,
                doc_id=doc_id,
                page=chunk.page,
                zh=zh,
                model=settings.trans_model,
            )
        )
    db.commit()
    return len(pairs)


def translate_pages(db: Session, doc_id: str, page_from: int, page_to: int) -> int:
    """翻译指定页范围内尚未翻译的段落，返回本次新增条数。

    批量任务**并发**发出（默认 3 路）：一页 8–10 段拆成 2–3 批并行，
    墙钟时间从"逐批串行 15 秒"降到"一批的 5 秒"，这是滚动阅读能不能用的分水岭。
    模型调用在 worker 线程里跑，**写库统一回到本线程**——SQLite 的多线程写
    会互相等锁，把 commit 收拢到一处比调大 timeout 更可靠。
    """
    chunks = _load_chunks(db, doc_id, page_from, page_to)
    if not chunks:
        return 0
    done = {
        t.chunk_id
        for t in db.scalars(
            select(Translation).where(Translation.chunk_id.in_([c.id for c in chunks]))
        ).all()
    }
    # 参考文献条目整段是作者名/期刊缩写，机翻只会更乱，直接跳过
    todo = [(c, prev) for c, prev in _with_prev(db, chunks) if c.id not in done and c.kind != "reference"]
    if not todo:
        return 0

    step = max(1, settings.trans_batch)
    batches = [todo[i : i + step] for i in range(0, len(todo), step)]
    payloads = [[_strip_carry(prev, c.text) for c, prev in b] for b in batches]

    results: list[list[str]] = [[] for _ in batches]
    workers = max(1, min(settings.trans_workers, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_translate_batch, texts): i for i, texts in enumerate(payloads)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001 单批失败不影响其他批
                log.warning("批次 %d 翻译异常：%s", idx, exc)
                results[idx] = []

    total = 0
    for batch, payload, out in zip(batches, payloads, results):
        if not out:
            # 整批失败（段数不匹配/超时）→ 退化为逐段重试，
            # 避免一段超长或格式异常拖垮整页
            out = []
            for t in payload:
                one = _translate_batch([t])
                out.append(one[0] if one else "")
        total += _save(db, doc_id, [(c, zh) for (c, _), zh in zip(batch, out) if zh])
    return total


# ---------------------------------------------------------------- 整篇翻译任务
def start_job(doc_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(doc_id)
        if job and job["running"]:
            return job

        db = session_scope()
        try:
            doc = db.get(Document, doc_id)
            if not doc:
                return {"running": False, "error": "文档不存在", "total": 0, "done": 0}
            pages = doc.page_count or 1
            if pages > settings.trans_max_pages:
                return {
                    "running": False,
                    "error": f"该文献 {pages} 页，超过整篇翻译上限 {settings.trans_max_pages} 页，"
                             f"请直接滚动阅读（滚到哪译到哪）",
                    "total": pages,
                    "done": 0,
                }
            job = {"running": True, "done": 0, "total": pages, "error": ""}
            _JOBS[doc_id] = job
        finally:
            db.close()

    def _run(doc_id: str) -> None:
        db = session_scope()
        try:
            doc = db.get(Document, doc_id)
            for p in range(1, (doc.page_count or 1) + 1):
                translate_pages(db, doc_id, p, p)
                with _JOBS_LOCK:
                    _JOBS[doc_id]["done"] = p
            with _JOBS_LOCK:
                _JOBS[doc_id]["running"] = False
        except Exception as exc:  # noqa: BLE001 后台任务不能把异常抛给用户线程
            log.exception("整篇翻译失败：%s", exc)
            with _JOBS_LOCK:
                _JOBS[doc_id].update({"running": False, "error": str(exc)[:200]})
        finally:
            db.close()

    threading.Thread(target=_run, args=(doc_id,), daemon=True).start()
    return _JOBS[doc_id]


def job_status(doc_id: str) -> dict:
    with _JOBS_LOCK:
        return dict(_JOBS.get(doc_id) or {"running": False, "done": 0, "total": 0, "error": ""})


def cached_ratio(db: Session, doc_id: str) -> dict:
    """已译比例，前端据此决定要不要显示"整篇翻译"。"""
    total = db.query(Chunk).filter(Chunk.doc_id == doc_id, Chunk.kind != "reference").count()
    done = db.query(Translation).filter(Translation.doc_id == doc_id).count()
    return {"total": total, "done": done, "ratio": round(done / total, 3) if total else 0.0}
