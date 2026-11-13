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
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import LLMError, gateway
from app.core.logger import get_logger
from app.rag import glossary
from app.db.models import Chunk, Document, Translation
from app.db.session import session_scope
from app.rag.chunker import strip_carry as _strip_carry

log = get_logger(__name__)

# 整篇翻译的后台任务进度（doc_id -> {total, done, running, error}）
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------- 段落重建
# overlap 剥离统一走 app.rag.chunker.strip_carry：
# 检索层、证据展示、中英对照必须共用同一份实现，否则三处会各自漂移。


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
    "3. 术语用学术通行译法，全篇保持一致；英文原词只在两种情况下保留："
    "缩写与专名（如 Transformer、BLEU、LoRA、LLM）首次出现时可加括号，"
    "以及原文本身用括号给出英文时；普通名词短语（如 sequence transduction、"
    "attention mechanism）一律译成中文，不加英文括号注释；\n"
    "4. 数字、公式、变量名、引用编号（如 [27]）原样保留；\n"
    "5. 图注译成「图 N：…」，表注译成「表 N：…」；\n"
    "6. 不省略、不总结、不添加原文没有的内容。"
)

# 中译英（官方 1.6.4）：目标是能直接拿去投稿/放预印本的学术英文，
# 所以既不能留中文术语，也不能有"机翻腔"（逐字直译的语序）。
_SYS_EN = (
    "你是学术论文翻译。输入是一个 JSON 数组，每个元素是一段中文；"
    "请逐段译成可直接用于投稿的规范学术英文，输出 JSON：{\"out\": [\"First translated paragraph\", ...]}。\n"
    "Rules:\n"
    "1. The number of output items must equal the input exactly — no merging, no splitting;\n"
    "2. Write formal academic English (third person, no colloquial or machine-translated phrasing);\n"
    "3. Use the standard English term for each technical term and keep it consistent across the batch; "
    "do not leave Chinese characters in the output;\n"
    "4. Keep numbers, formulas, variable names and citation markers (e.g. [27]) verbatim;\n"
    "5. Translate captions as \"Figure N: ...\" and \"Table N: ...\";\n"
    "6. Do not omit, summarize, or add content that is not in the source;\n"
    "7. Mind English grammar details: article agreement with numbers (\"an 11-millisecond delay\", "
    "\"a 11-point gain\" is wrong), consistent tense, and singular/plural agreement."
)

# 本地小模型（3B 级）用精简提示词。
# 实测：完整 _SYS_EN 有 700 余字符、7 条规则，qwen2.5:3b 处理不了，直接陷入复读，
# Ollama 抛 500 "prediction aborted, token repeat limit reached"，重试三次全挂；
# 换成 3 句话的短指令后 0.3 秒正常出结果。这不是"模型不行"，是提示词没为它适配。
_SYS_LITE = (
    "你是学术翻译。输入是 JSON 数组，逐段译成中文，输出同样长度的 JSON："
    '{"out": ["..."]}。术语用通行译法，保留公式、数字与 [n] 角标，不要增删内容。'
)
_SYS_EN_LITE = (
    "You are an academic translator. Input is a JSON array of Chinese paragraphs. "
    "Translate each into formal academic English, return JSON "
    '{"out": ["..."]} with exactly the same number of items. '
    "Keep formulas, numbers and [n] markers unchanged, and leave no Chinese characters."
)

# 模型偶尔会把输入序号带进译文（"[1] Mistral 7B 采用…"）
_RE_LEAD_NUM = re.compile(r"^\s*[\[\(（]?\d{1,2}[\]\)）]?[.、:：]?\s+")
# 个别段落在传入前已经被上一段的重叠尾句污染
_RE_WS = re.compile(r"[ \t]{2,}")


def _clean_zh(text: str) -> str:
    return _RE_WS.sub(" ", _RE_LEAD_NUM.sub("", str(text).strip())).strip()


_RE_CAPTION = re.compile(r"^\s*(图|表)\s*([0-9]+(?:\.[0-9]+)*)\s*([：:\.、]?)\s*")


def _fix_caption(zh: str) -> str:
    """图注/表注统一成「图 N：…」「表 N：…」（官方 1.3.3.2 的格式要求）。

    为什么值得写死：提示词里已经写了这条规则，但实测 qwen-flash 仍然输出
    「图 2.1 线性回归模型。」——冒号丢了。这类偏差模型自己不会稳定遵守，
    而它纯粹是标点问题，用一行正则兜住比反复调提示词可靠。
    """
    m = _RE_CAPTION.match(zh)
    if not m:
        return zh
    label, num, sep = m.group(1), m.group(2), m.group(3)
    if sep in ("：", ":"):
        return zh
    return f"{label} {num}：{zh[m.end():].strip()}"


def _degenerate(src: str, zh: str) -> bool:
    """译文是否"塌了"——短得不像话，直接丢弃等下次重译。

    实测来源：LayerNorm 第 5 页有一段译文落库成 `"5"`，段数校验通过了
    （一批四段，模型对其中一段只回了一个页码），于是这句被当成"已译"永久缓存，
    用户在对照视图里看到右侧只有一个 5，而且再也不会重试。
    只拦"原文很长而译文极短"这一种：标题、表注本来就短（"表 1：模型架构。"），
    用绝对长度卡会把它们误伤。
    """
    return len(zh.strip()) < 15 and len(src.strip()) >= 80


def _hq_model() -> str | None:
    """整篇翻译用的加强模型；留空或与 trans_model 相同则返回 None（走默认）。"""
    m = (settings.trans_model_hq or "").strip()
    return m if m and m != settings.trans_model else None


def _translate_batch(texts: list[str], to: str = "zh", model: str | None = None) -> list[str]:
    """一次译若干段，返回严格等长的译文列表；失败返回空列表由调用方降级。

    `to="en"` 走中译英提示词（官方 1.6.4）。两个方向的输出预算不同：
    英译中每字符产出约 1.5 token，中译英约 0.9 token 但英文更长，取 1.5 倍余量。
    """
    import json as _json

    chars = sum(len(t) for t in texts)
    budget = min(7000 if to == "en" else 6000, 400 + int(chars * 1.5))
    # 本地路由换精简提示词：3B 级模型扛不住云端那版长指令（见 _SYS_LITE 注释）
    if gateway.route() == "local":
        sys_prompt = _SYS_EN_LITE if to == "en" else _SYS_LITE
    else:
        sys_prompt = _SYS_EN if to == "en" else _SYS

    # 只注入本批真正出现的术语（最多 12 条）。整份表塞进去会多花 200 token/批，
    # 而且模型会开始"为了用术语而用术语"，把通顺的句子改坏。
    # 中译英不注入：方向相反时这张表等于反向词表，而模型对英文术语本来就熟。
    if to == "zh":
        terms = glossary.for_text("\n".join(texts))
        if terms:
            sys_prompt = sys_prompt + "\n\n" + glossary.prompt_block(terms)
    try:
        obj = gateway.chat_json(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": _json.dumps(texts, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=budget,
            model=model or settings.trans_model,
        )
    except LLMError as exc:
        log.warning("翻译请求失败：%s", exc)
        return []

    arr = obj.get("out") or obj.get("zh") or obj.get("data") or obj.get("translations") or []
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
    out: list[str] = []
    for src_text, raw in zip(texts, arr):
        zh = _clean_zh(raw)
        if _degenerate(src_text, zh):
            log.warning("译文退化（%d 字，原文 %d 字），丢弃待重译：%r",
                        len(zh), len(src_text), zh[:20])
            zh = ""
        out.append(zh)
    if to == "zh":
        # 只统计、不改写：实测把"隐单元"正则替换成"隐藏单元"会改坏
        # "隐单元的维度"这类正常表达，得不偿失。
        bad = glossary.compliance(list(zip(texts, out)))
        if bad:
            log.info("本批术语不一致：%s", ", ".join(f"{b['en']}({b['ok']}/{b['n']})" for b in bad[:4]))
    return out


def _save(db: Session, doc_id: str, pairs: list[tuple[Chunk, str]], model: str = "") -> int:
    if not pairs:
        return 0
    for chunk, zh in pairs:
        db.merge(
            Translation(
                chunk_id=chunk.id,
                doc_id=doc_id,
                page=chunk.page,
                zh=zh,
                model=model or settings.trans_model,
            )
        )
    db.commit()
    return len(pairs)


_RE_MATH = re.compile(
    r"\\begin\{|\\end\{|\\(?:frac|sum|prod|int|partial|nabla|alpha|beta|gamma|delta|theta|lambda"
    r"|sigma|omega|mathbf|mathrm|mathbb|mathcal|text|cdot|times|leq|geq|approx|left|right|hat|bar)\b"
)
_RE_CODE = re.compile(
    r"(?:^\s*(?:def|class|import|from|return|for|while|if|elif|else|with|try|except)\b)"
    r"|(?:\bself\.\w+)|(?:^\s*[{}();]+\s*$)|(?:\w+\s*=\s*\w+\()",
    re.M,
)


def keep_verbatim(kind: str, text: str) -> bool:
    """这一段是否原样保留、不送翻译（官方 1.3.3.2.c：代码和公式保留英文）。

    不能只看 `kind`：PDF 里的多行公式和代码块经常被归到 text，
    而正文里偶尔出现一个行内 `$\\alpha$` 又不该整段跳过。
    判据用"字母密度 + 结构特征"：公式与代码的英文字母占比明显低于自然语言
    （正文约 0.8，公式约 0.5，纯数字表格不会命中 LaTeX 结构所以不受影响）。
    """
    if kind == "reference":
        return True
    t = (text or "").strip()
    if kind == "formula":
        # 解析器会把"夹着希腊字母/符号的整句正文"也标成 formula（实测 61 条原样保留里 5 条
        # 是完整句子，例如 "We used the Adam optimizer [18] with β1 = 0.9, β2 = 0.98…"）。
        # 真正该原样保留的公式没有成串的小写英文词，用它区分，比只看 kind 可靠。
        low = sum(1 for w in re.findall(r"[A-Za-z]{3,}", t) if w.islower())
        return low < 8
    if len(t) < 12:
        return False
    letters = sum(1 for ch in t if ch.isalpha())
    ratio = letters / len(t)
    if _RE_MATH.search(t) and (t.count("$") >= 2 or t.count("\\") >= 3) and ratio < 0.62:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 3 and len(_RE_CODE.findall(t)) >= 3 and ratio < 0.78:
        return True
    return False


def translate_pages(db: Session, doc_id: str, page_from: int, page_to: int,
                    hq: bool = False) -> int:
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
    pending = [(c, prev) for c, prev in _with_prev(db, chunks) if c.id not in done and c.kind != "reference"]
    # 公式/代码原样入库（zh = 原文）而不是跳过：跳过的话对照视图右侧会空一块、
    # 导出还得再开一条"缺失"分支；原样写进去，三处看到的内容是一致的。
    verbatim: list[tuple[Chunk, str]] = []
    todo: list[tuple[Chunk, Chunk | None]] = []
    for c, prev in pending:
        txt = _strip_carry(prev, c.text).strip()
        if not txt:
            continue
        if keep_verbatim(c.kind, txt):
            verbatim.append((c, txt))
        else:
            todo.append((c, prev))
    if verbatim:
        _save(db, doc_id, verbatim, model="verbatim")
        log.info("跳过翻译并原样保留 %d 段公式/代码", len(verbatim))
    if not todo:
        return len(verbatim)

    model = _hq_model() if hq else None
    step = max(1, settings.trans_batch)
    batches = [todo[i : i + step] for i in range(0, len(todo), step)]
    payloads = [[_strip_carry(prev, c.text) for c, prev in b] for b in batches]

    results: list[list[str]] = [[] for _ in batches]
    workers = max(1, min(settings.trans_workers, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_translate_batch, texts, "zh", model): i
                   for i, texts in enumerate(payloads)}
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
                one = _translate_batch([t], model=model)
                out.append(one[0] if one else "")
        pairs = [(c, _fix_caption(zh) if c.kind in {"figure", "table"} else zh)
                 for (c, _), zh in zip(batch, out) if zh]
        total += _save(db, doc_id, pairs, model=model or "")
    return total + len(verbatim)


def export_zh(db: Session, doc_id: str) -> dict:
    """把已缓存的中文译文拼成一篇 Markdown（官方 1.3.3.2 的"输出可读性"）。

    只读 translations 表、**不调用模型**：
    - 用户在对照视图里滚过的页都已落库，导出是 0 成本、秒出；
    - 重复点导出不会重复烧额度，也不用等几分钟。

    结构刻意与「中译英」的导出对齐（`{doc_id}_en.md`），两份可以对着看：
    标题 → 作者/年份/覆盖率 → 按章节分节 → 正文 → References 原文。
    """
    import json as _json

    doc = db.get(Document, doc_id)
    if not doc:
        return {"ok": False, "error": "文档不存在"}

    chunks = list(
        db.scalars(
            select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.page, Chunk.idx)
        ).all()
    )
    if not chunks:
        return {"ok": False, "error": "这篇还没有解析出正文"}

    zh_map = {
        t.chunk_id: (t.zh or "")
        for t in db.scalars(select(Translation).where(Translation.doc_id == doc_id)).all()
    }

    body_chunks = [c for c in chunks if c.kind != "reference"]
    total = len(body_chunks)
    translated = sum(1 for c in body_chunks if zh_map.get(c.id))

    try:
        authors = _json.loads(doc.authors or "[]")
    except Exception:  # noqa: BLE001
        authors = []

    head = [f"# {doc.title or doc.filename}", ""]
    meta = [", ".join(authors[:6])] if authors else []
    if doc.year:
        meta.append(str(doc.year))
    pct = round(translated / total * 100) if total else 0
    meta.append(f"中文译本 · {translated}/{total} 段已译（{pct}%）")
    head += [" · ".join([m for m in meta if m]), "", "---", ""]

    body: list[str] = []
    cur_sec = None
    verbatim = 0
    missing = 0
    for c in body_chunks:
        sec = (c.section_path or "").split(">")[-1].strip()
        if sec and sec != cur_sec and len(sec) < 60:
            body += [f"## {sec}", ""]
            cur_sec = sec
        src = (c.text or "").strip()
        if not src:
            continue
        if keep_verbatim(c.kind, src):
            # 公式/代码放进围栏块：Markdown 会把 `$...$` 当数学渲染，
            # 原样围起来才能保证读者看到的就是原文
            verbatim += 1
            body += ["```", src, "```", ""]
            continue
        zh = zh_map.get(c.id, "")
        if not zh:
            # 未译段落保留原文而不是留空：宁可中英混排，也不能让内容凭空消失。
            # 用 HTML 注释说明原因，渲染时不可见、源码里可查。
            missing += 1
            body += [src, "", "<!-- 未译：滚到这一页触发翻译，或点「整篇翻译」，即可导出完整译本 -->", ""]
        else:
            body += [zh, ""]

    refs = [c.text.strip() for c in chunks if c.kind == "reference" and (c.text or "").strip()]
    if refs:
        body += ["## References（原文保留，不翻译）", ""] + [f"- {r}" for r in refs]

    text = "\n".join(head + body).strip() + "\n"
    exp = Path(settings.db_path).parent / "exports"
    exp.mkdir(parents=True, exist_ok=True)
    path = exp / f"{doc_id}_zh.md"
    path.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "doc_id": doc_id,
        "title": doc.title or doc.filename,
        "markdown": text,
        "chars": len(text),
        "total": total,
        "translated": translated,
        "missing": missing,
        "verbatim": verbatim,
        "ratio": round(translated / total, 3) if total else 0.0,
        "path": str(path),
    }


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
                translate_pages(db, doc_id, p, p, hq=True)
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


_RE_CJK = re.compile(r"[\u4e00-\u9fff]")


def detect_lang(text: str) -> str:
    """粗判语种：汉字占比 > 18% 视为中文。

    不追求严谨的语言识别——划词片段常常只有十几个字，任何统计模型都不稳；
    但"这段是中文还是英文"只需要看有没有汉字。18% 能容忍中英混排
    （如「Transformer 的自注意力机制」），又不会把纯英文术语误判成中文。
    """
    s = (text or "").strip()
    if not s:
        return "en"
    return "zh" if len(_RE_CJK.findall(s)) / len(s) > 0.18 else "en"


def translate_selection(text: str, to: str = "") -> dict:
    """划词/悬停翻译：英译中（官方 1.3.3.1）与中译英（官方 1.6.4）共用一条通道。

    `to` 为空时按源语言自动选方向：中文→英文、英文→中文。
    与整篇翻译共用同一套模型，区别只在于：
    - 不落库（选中的是临时片段，缓存没有复用价值，还会污染译文统计）；
    - 超长选区先截断，避免一次划到整页拖垮交互（用户等 30 秒会以为坏了）。
    """
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "error": "选区为空", "text": ""}
    src = detect_lang(raw)
    target = to if to in {"zh", "en"} else ("en" if src == "zh" else "zh")
    truncated = False
    if len(raw) > 4000:
        raw = raw[:4000]
        truncated = True
    out = _translate_batch([raw], to=target)
    if not out or not out[0]:
        return {"ok": False, "error": "翻译服务暂时不可用，请稍后重试",
                "text": "", "to": target, "truncated": truncated}
    # `zh` 保留旧字段名（前端历史上读的是它），`text` 是与方向无关的通用字段
    return {"ok": True, "src": src, "to": target, "text": out[0], "zh": out[0],
            "chars": len(raw), "truncated": truncated}


# ---------------------------------------------------------------- 整篇中译英（1.6.4）
def _en_key(doc_id: str) -> str:
    """与"英译中整篇"共用 _JOBS，但必须分开 key，否则两边进度互相覆盖。"""
    return f"{doc_id}:en"


def _batch_by_chars(pairs: list[tuple[int, str]], limit: int = 1200,
                    max_items: int = 6) -> list[list[tuple[int, str]]]:
    """按字符数分批：中译英的输出比原文长，一批塞太多会被输出预算截断。"""
    batches: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    size = 0
    for idx, txt in pairs:
        if cur and (size + len(txt) > limit or len(cur) >= max_items):
            batches.append(cur)
            cur, size = [], 0
        cur.append((idx, txt))
        size += len(txt)
    if cur:
        batches.append(cur)
    return batches


def start_en_job(db: Session, doc_id: str) -> dict:
    """启动整篇中译英（后台线程），立刻返回当前进度供前端轮询。"""
    key = _en_key(doc_id)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.get("running"):
            return {"ok": True, "started": False, "running": True,
                    "total": job.get("total", 0), "done": job.get("done", 0)}
        _JOBS[key] = {"total": 0, "done": 0, "running": True, "error": "", "text": "",
                      "path": "", "segments": 0, "skipped": 0, "ms": 0}
    threading.Thread(target=_run_en_job, args=(doc_id,), daemon=True).start()
    return {"ok": True, "started": True, "running": True, "total": 0, "done": 0}


def _run_en_job(doc_id: str) -> None:
    import json as _json
    import time as _time

    t0 = _time.time()
    key = _en_key(doc_id)
    db = session_scope()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            raise ValueError("文档不存在")
        chunks = list(
            db.scalars(
                select(Chunk)
                .where(Chunk.doc_id == doc_id, Chunk.kind != "reference")
                .order_by(Chunk.page, Chunk.idx)
            ).all()
        )
        segs: list[tuple[Chunk, str]] = []
        for c, prev in _with_prev(db, chunks):
            txt = _strip_carry(prev, c.text).strip()
            if txt:
                segs.append((c, txt))
        if not segs:
            raise ValueError("这篇没有可翻译的正文段落")

        # 中文论文里常混着英文摘要、公式块、代码——这些原样保留，不送模型：
        # 既省时间，也避免模型把好好的英文"重写"一遍。
        todo = [(i, t) for i, (c, t) in enumerate(segs) if detect_lang(t) == "zh"]
        _JOBS[key]["total"] = len(todo)
        _JOBS[key]["skipped"] = len(segs) - len(todo)
        if not todo:
            raise ValueError("这篇没有中文正文，无需译成英文")

        out_map: dict[int, str] = {}
        hq = _hq_model()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_translate_batch, [t for _, t in batch], "en", hq): batch
                       for batch in _batch_by_chars(todo)}
            for fut in as_completed(futures):
                batch = futures[fut]
                try:
                    res = fut.result()
                except Exception:  # noqa: BLE001 单批失败不能丢掉整篇
                    res = []
                for (idx, _), out in zip(batch, res or []):
                    out_map[idx] = out
                _JOBS[key]["done"] = len(out_map)

        # 大标题和小节标题也要译：英文正文里顶着「## 2 方法」很别扭。
        # 标题短、数量少，跟正文一起批量译一次就够，不额外增加一轮调用。
        heads: list[str] = []
        if detect_lang(doc.title or "") == "zh":
            heads.append(doc.title.strip())
        for c, _ in segs:
            sec_raw = (c.section_path or "").split(">")[-1].strip()
            if sec_raw and len(sec_raw) < 60 and detect_lang(sec_raw) == "zh" and sec_raw not in heads:
                heads.append(sec_raw)
        head_map: dict[str, str] = {}
        if heads:
            for k, v in zip(heads[:14], _translate_batch(heads[:14], "en", hq) or []):
                head_map[k] = v

        authors = []
        try:
            authors = _json.loads(doc.authors or "[]")
        except Exception:  # noqa: BLE001
            authors = []
        title_en = head_map.get((doc.title or "").strip(), doc.title or doc.filename)
        head = [f"# {title_en}", ""]
        meta = [", ".join(authors[:6])] if authors else []
        if doc.year:
            meta.append(str(doc.year))
        meta.append(f"English translation · {len(out_map)}/{len(segs)} paragraphs")
        head += [" · ".join([m for m in meta if m]), "", "---", ""]

        body: list[str] = []
        cur_sec = None
        for i, (c, src) in enumerate(segs):
            sec = (c.section_path or "").split(">")[-1].strip()
            if sec and sec != cur_sec and len(sec) < 60:
                body += [f"## {head_map.get(sec, sec)}", ""]
                cur_sec = sec
            # 没译出的段落回退原文：宁可中英混排，也不能让内容凭空消失
            body += [out_map.get(i) or src, ""]

        text = "\n".join(head + body).strip() + "\n"
        exp = Path(settings.db_path).parent / "exports"
        exp.mkdir(parents=True, exist_ok=True)
        path = exp / f"{doc_id}_en.md"
        path.write_text(text, encoding="utf-8")

        _JOBS[key].update({
            "text": text, "path": str(path), "segments": len(segs),
            "ms": int((_time.time() - t0) * 1000), "running": False,
        })
        log.info("整篇译英完成：%s，%d/%d 段，%.1fs", doc_id, len(out_map), len(segs), _time.time() - t0)
    except Exception as exc:  # noqa: BLE001 后台线程不能让异常逃逸
        log.exception("整篇译英失败：%s", doc_id)
        _JOBS[key].update({"error": str(exc)[:200], "running": False})
    finally:
        db.close()


def en_status(db: Session, doc_id: str) -> dict:
    """整篇译英进度。没跑过时返回 null 状态，让前端决定是否显示按钮。"""
    j = _JOBS.get(_en_key(doc_id)) or {}
    text = j.get("text") or ""
    if not j:
        # 进程重启后内存里的 job 没了，但产出的 Markdown 还在——
        # 重新读出来比让用户再花一次模型额度重译一次要合理
        path = Path(settings.db_path).parent / "exports" / f"{doc_id}_en.md"
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                text = ""
    return {
        "running": bool(j.get("running")),
        "total": j.get("total", 0),
        "done": j.get("done", 0),
        "segments": j.get("segments", 0),
        "skipped": j.get("skipped", 0),
        "chars": len(text),
        "error": j.get("error", ""),
        "ms": j.get("ms", 0),
        "preview": text[:6000],
        "path": j.get("path", ""),
        "started": bool(j),
    }
