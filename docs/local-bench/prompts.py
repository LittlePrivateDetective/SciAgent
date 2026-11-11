"""本地模型 vs 云端模型：40 组固定测试集。

为什么自己写而不是用现成 benchmark：这里要回答的不是"模型谁更聪明"，
而是"这个项目把模型换成 3B 本地模型之后，哪些功能还能用、哪些会坏"。
所以题目全部取自项目真实用途——RAG 问答、学术翻译、结构化输出、长文摘要，
每类都有明确的通过判据（`expect`），而不是靠人读一遍觉得"还行"。

字段：
    id       编号，报告里引用它
    cat      qa / en2zh / zh2en / json / long
    prompt   直接发给模型的用户消息
    max_tokens  上限，控制云端跑测试的成本与时间
    expect   自动判据：zh / en / json / any
"""
from __future__ import annotations

# ---------------------------------------------------------------- A. 中文问答（10）
# 给一段论文片段 + 一个具体问题。判据是"答得对不对"靠人工，这里只卡输出形态（中文、非空）。
_QA = [
    (
        "A01",
        "Transformer 的核心机制是什么？只依据下面这段回答，不超过 80 字。\n\n"
        "【片段】We propose a new simple network architecture, the Transformer, based solely on "
        "attention mechanisms, dispensing with recurrence and convolutions entirely. The encoder "
        "and decoder are both composed of a stack of identical layers, each with a multi-head "
        "self-attention sub-layer and a position-wise fully connected feed-forward network.",
    ),
    (
        "A02",
        "BERT 的预训练用了哪两个任务？用中文回答，100 字以内。\n\n"
        "【片段】BERT is pre-trained with two unsupervised tasks: a masked language model (MLM) "
        "and next sentence prediction (NSP). In MLM, some tokens are masked at random and the model "
        "predicts their original vocabulary id; in NSP, the model predicts whether two sentences "
        "were consecutive in the original corpus.",
    ),
    (
        "A03",
        "根据片段，GPT-3 在 few-shot 场景下是否更新模型参数？为什么？\n\n"
        "【片段】GPT-3 is an autoregressive language model with 175 billion parameters. We evaluate "
        "it in the few-shot setting, where the model is given a handful of demonstrations as part of "
        "the prompt, and no gradient updates or fine-tuning are performed for the downstream task.",
    ),
    (
        "A04",
        "对比片段中两种检索策略的差别，用中文写 150–250 字。\n\n"
        "【片段】Sparse retrieval such as BM25 matches exact terms and is strong on rare entities, "
        "but fails when the query and document share no lexical overlap. Dense retrieval encodes "
        "queries and passages into a shared vector space and can match paraphrases, yet it often "
        "misses long-tail entities that never appeared during training. Hybrid methods combine "
        "the two scores, typically through weighted sum or reciprocal rank fusion, and reranking "
        "with a cross-encoder further improves the top-k precision at the cost of latency.",
    ),
    (
        "A05",
        "片段说扫描件解析的难点是什么？用中文回答，120 字以内。\n\n"
        "【片段】Scanned PDFs contain no text layer; every page is a single bitmap image. Parsing "
        "therefore requires OCR, which is sensitive to skew, low DPI, multi-column layouts and "
        "equation regions. Text extracted from OCR also lacks reliable coordinates, so citation "
        "anchors cannot be attached to the original glyphs.",
    ),
    (
        "A06",
        "为什么说 chunk 的大小是一个权衡？结合片段说明，150 字左右。\n\n"
        "【片段】Chunk size controls the granularity of retrieval. Small chunks give precise "
        "embeddings and cheaper prompts, but they fragment context and often lose the antecedent "
        "of a pronoun. Large chunks preserve context yet dilute the embedding, and force the "
        "generator to read irrelevant text. Most systems settle between 256 and 1024 tokens, "
        "sometimes with overlapping windows or sentence-aware boundaries.",
    ),
    (
        "A07",
        "片段中提到的两个评估指标分别衡量什么？用中文回答，不超过 120 字。\n\n"
        "【片段】We report exact match (EM) and F1. EM measures the percentage of predictions that "
        "match a gold answer string exactly after normalization; F1 treats prediction and gold as "
        "bags of tokens and computes their harmonic mean, thus giving partial credit to answers "
        "that overlap substantially with the gold span.",
    ),
    (
        "A08",
        "如果要把这段方法复现出来，最少需要哪些组件？列成 3–5 条。\n\n"
        "【片段】Our pipeline has three stages. First, documents are parsed into sections and split "
        "into overlapping chunks of 512 tokens. Second, each chunk is embedded with a bi-encoder and "
        "indexed in a vector store; a BM25 index is built over the same chunks. Third, at query time "
        "we retrieve the top 20 from each index, fuse with reciprocal rank fusion, rerank the top 50 "
        "with a cross-encoder, and pass the top 5 chunks to the generator.",
    ),
    (
        "A09",
        "片段里作者为什么选择 pgfplots 而不是直接放图片？用中文回答。\n\n"
        "【片段】Figures are generated as pgfplots code rather than raster images because vector "
        "output stays sharp at any print size, the fonts match the body text, and reviewers can "
        "verify the plotted values directly from the source. The trade-off is compile time and a "
        "steeper learning curve for authors unfamiliar with TikZ.",
    ),
    (
        "A10",
        "用一句话概括片段的核心结论。\n\n"
        "【片段】Across all eight benchmarks, the hybrid retriever with cross-encoder reranking "
        "improves answer F1 by 4.2 points over dense-only retrieval, while adding 180 ms of median "
        "latency; on the two entity-centric datasets the gain disappears entirely, suggesting the "
        "improvement comes from paraphrase robustness rather than better entity matching.",
    ),
]

# ---------------------------------------------------------------- B. 英译中（8）
_EN2ZH = [
    ("B01", "We propose a new simple network architecture, the Transformer, based solely on attention "
            "mechanisms, dispensing with recurrence and convolutions entirely."),
    ("B02", "Masked language modeling enables the representation to fuse the left and right context, "
            "which allows us to pre-train a deep bidirectional Transformer."),
    ("B03", "Retrieval-augmented generation combines a parametric generator with a non-parametric "
            "memory, improving factual accuracy on knowledge-intensive tasks."),
    ("B04", "The model achieves 28.4 BLEU on WMT 2014 English-to-German translation, outperforming "
            "the best previously reported results by more than 2 BLEU."),
    ("B05", "Our ablation shows that removing the reranking stage degrades top-5 precision by 11.3 "
            "points, while halving the median latency."),
    ("B06", "Chunk-level embeddings are computed with a bi-encoder, and the resulting vectors are "
            "indexed using approximate nearest neighbor search for sub-linear retrieval."),
    ("B07", "Contrary to prior work, we find that increasing context length beyond 8k tokens yields "
            "diminishing returns once the retriever is well tuned."),
    ("B08", "All experiments are conducted on a single RTX 2060 with 6 GB of memory, using mixed "
            "precision and gradient checkpointing to fit the 3B parameter model."),
]

# ---------------------------------------------------------------- C. 中译英（8）
_ZH2EN = [
    ("C01", "本文提出一种基于块稀疏注意力的长文档检索增强生成方法，在降低显存占用的同时保持检索质量。"),
    ("C02", "实验表明，混合检索配合交叉编码器重排可将答案 F1 提升 4.2 个点，代价是增加 180 毫秒延迟。"),
    ("C03", "我们将每个文档切分为 512 个词元的重叠块，并用双塔模型编码后写入向量库。"),
    ("C04", "扫描件缺少文本层，因此引用锚点无法附着在原始字形上，只能退化为整块近似框。"),
    ("C05", "该方法的局限在于依赖高质量的参考文献解析，对格式混乱的预印本鲁棒性不足。"),
    ("C06", "我们在一块 6GB 显存的消费级显卡上完成了全部实验，模型参数量为 30 亿。"),
    ("C07", "消融实验显示，去掉重排阶段会使前五命中率下降 11.3 个百分点。"),
    ("C08", "未来工作将探索把这套流水线迁移到多模态文档，并支持公式的 LaTeX 重建。"),
]

# ---------------------------------------------------------------- D. 结构化输出（8）
# 判据：能否被 json.loads 直接解析（不许带 ``` 包裹、不许带解释文字）
_JSON = [
    (
        "D01",
        "为下面这组数据写一条学术图注，只输出 JSON，字段为 caption_zh、caption_en、trend。\n"
        "数据：Transformer 28.4 BLEU；BERT 34.5；GPT-3 41.0。",
    ),
    (
        "D02",
        "把这段话抽成论文架构图的节点与边，只输出 JSON：\n"
        '{"nodes":[{"id","label","kind","group"}],"edges":[{"from","to","label"}]}\n'
        "内容：用户上传 PDF，系统解析成章节与分块，编码后写入向量库；提问时先检索再重排，"
        "最后交给生成模型输出答案并做引用校验。",
    ),
    (
        "D03",
        "给这个研究想法生成三行大纲，只输出 JSON："
        '{"title": "...", "abstract": "...", "keywords": ["...", "..."]}。\n'
        "想法：用块稀疏注意力降低长文档 RAG 的显存占用。",
    ),
    (
        "D04",
        "把这两个术语整理成中英对照表，只输出 JSON 数组，每项含 zh、en 两个字段："
        "混合检索、交叉编码器重排。",
    ),
    (
        "D05",
        "判断下面这段是否包含实验数据，只输出 JSON：{\"has_data\": true/false, \"numbers\": [...]}\n"
        "文本：我们在三个数据集上进行了实验，分别取得 28.4、34.5 和 41.0 的分数。",
    ),
    (
        "D06",
        "把下面这句话拆成「方法-数据-结论」三部分，只输出 JSON："
        '{"method": "...", "data": "...", "result": "..."}\n'
        "句子：We fine-tune BERT on SQuAD and obtain 88.5 F1, surpassing the previous best by 1.5 points.",
    ),
    (
        "D07",
        "为一张柱状图生成 LaTeX 三线表的表头，只输出 JSON："
        '{"caption": "...", "columns": ["...", "..."]}\n'
        "数据列：Model、BLEU、Params。",
    ),
    (
        "D08",
        "列出这段摘要里提到的三个贡献点，只输出 JSON 数组，每项是字符串。\n"
        "摘要：本文提出块稀疏注意力，将长文档编码的显存降低 42%；设计两阶段重排，"
        "把前五命中率提升 11.3 个点；并在 6GB 显卡上完成全部实验。",
    ),
]

# ---------------------------------------------------------------- E. 长文本（6）
_LONG_BODY = (
    "检索增强生成把外部知识引入生成模型，从而缓解参数化记忆的时效性与幻觉问题。"
    "典型流水线包含文档解析、分块、向量化、索引、检索、重排与生成七个阶段。"
    "文档解析需要处理双栏、扫描件、公式与表格；分块要在上下文完整性与检索精度之间权衡，"
    "常见做法是 256 到 1024 个词元的重叠窗口。向量化通常使用双塔编码器，"
    "把查询与段落映射到同一空间，再用近似最近邻搜索实现亚线性检索。"
    "稀疏检索（如 BM25）在长尾实体上表现更稳，但对同义改写不敏感；"
    "混合检索用加权求和或倒数排名融合两路得分，通常能带来稳定增益。"
    "重排阶段使用交叉编码器对候选段落逐对打分，精度更高但延迟显著增加，"
    "因此多见于对前五十个候选做精排。生成阶段需要把检索到的段落拼进提示词，"
    "并约束模型只依据给定材料作答，同时输出可溯源的引用锚点。"
) * 1  # 单段约 300 字，下面按需重复拼接

# 每条材料开头必须**不一样**：Ollama 按最长公共前缀复用 KV cache，
# 六条 prompt 若共用同一个开头，后面五次请求的 prefill 会被整段跳过，
# 长文本 TTFT 会从 ~950ms 掉到 ~160ms——测出来的是缓存，不是显卡。
_LONG = [
    ("E01", "【材料一 · 检索增强生成综述】请把它压缩成三条要点，每条不超过 40 字。\n\n"
            + _LONG_BODY * 5),
    ("E02", "【材料二 · 流水线阶段说明】请按顺序列出材料讲到的阶段，只写阶段名。\n\n"
            + _LONG_BODY * 5),
    ("E03", "【材料三 · 混合检索论证】请说明为什么混合检索比单一稠密检索更稳，100 字以内。\n\n"
            + _LONG_BODY * 5),
    ("E04", "【材料四 · 重排代价】材料里提到重排的代价是什么？用一句话回答。\n\n"
            + _LONG_BODY * 6),
    ("E05", "【材料五 · 方法综述】请把方法部分改写成 200 字左右的中文综述，不要分点。\n\n"
            + _LONG_BODY * 6),
    ("E06", "【材料六 · 幻觉来源】材料中哪些环节可能引入幻觉？列出 2–3 个并说明原因。\n\n"
            + _LONG_BODY * 7),
]


def all_cases() -> list[dict]:
    """展开成统一结构，按类别顺序返回。"""
    out: list[dict] = []
    for cid, text in _QA:
        out.append({"id": cid, "cat": "qa", "prompt": text, "max_tokens": 400, "expect": "zh"})
    for cid, text in _EN2ZH:
        out.append({"id": cid, "cat": "en2zh", "prompt":
                    "把下面这句学术英文翻译成中文，只输出译文，不要解释：\n" + text,
                    "max_tokens": 300, "expect": "zh"})
    for cid, text in _ZH2EN:
        out.append({"id": cid, "cat": "zh2en", "prompt":
                    "Translate the following Chinese academic sentence into English. "
                    "Output the translation only:\n" + text,
                    "max_tokens": 300, "expect": "en"})
    for cid, text in _JSON:
        # D02 要吐 8–10 个节点 + 边，320 token 装不下；第一轮三路全被截断，
        # 那是预算不足，不是模型不行——给够预算再测一次才公平
        out.append({"id": cid, "cat": "json", "prompt": text,
                    "max_tokens": 800 if cid == "D02" else 320, "expect": "json"})
    for cid, text in _LONG:
        out.append({"id": cid, "cat": "long", "prompt": text, "max_tokens": 400, "expect": "any"})
    return out


if __name__ == "__main__":
    cases = all_cases()
    print("cases", len(cases))
    from collections import Counter
    print(Counter(c["cat"] for c in cases))
    # 粗略估算输入长度，长文本组要保证真的够长
    for c in cases:
        if c["cat"] == "long":
            print(c["id"], "chars", len(c["prompt"]))
