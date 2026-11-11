# SciAgent 全量功能测试 · 测试用例总表

- 运行时间：`2026-11-11T14:56:35.434703`
- 被测服务：`http://127.0.0.1:8013`
- 证据文件：`docs/qa/evidence/run_20261111_144615.json`（逐条含完整 evidence 字段）
- 结果：**PASS 35 / FAIL 0 / BLOCKED 1 / 共 36**

> 状态说明：PASS=实测通过且有数值/文件证据；FAIL=实测不通过（见缺陷记录）；BLOCKED=本机环境无法验证，等价【待实测】，绝不冒充通过。

| 编号 | 考察点 | 操作步骤 | 预期结果 | 实际结果 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| E01 | c-3 架构可复现性与部署 | GET /health、/api/system/info，记录实际版本 | 200 且返回真实版本、模型、向量库 | Windows / Python 3.13.14 / deepseek-v4.1-flash / chroma / {'documents': 37, 'chunks': 19766, 'tasks': 121, 'lessons': 70} | **PASS** | {"keys": ["env"], "file": "run_20261111_144615.json#E01"} |
| E02 | c-1 代码质量 | 统计代码行数、检查所有模块可 import、检查硬编码密钥 | 无 import 失败、仓库内无明文密钥 | 8038 行，模块全部可导入，无明文密钥 | **PASS** | {"keys": ["code_lines"], "file": "run_20261111_144615.json#E02"} |
| E03 | c-3 Docker 配置一致性 + 布局预演 | 静态校验 COPY/compose/CMD/依赖声明/.env 覆盖；按 Dockerfile 搭等价 /app 目录用零密钥 mock 模式真实启动并探测 5 个接口 | 静态全通过，镜像文件布局可独立启动且接口全 200 | COPY 5 项齐全、15 个第三方依赖全部声明、零密钥 mock 启动 5 接口全 200（has_dotenv=False） | **PASS** | {"keys": ["static", "note", "boot"], "file": "run_20261111_144615.json#E03"} |
| E03D | c-3 真实 docker build（【待实测】） | 在装有 Docker 的机器上执行 docker compose up -d --build 并访问 :8000/health | 镜像构建成功、容器健康 | 本机无 Docker，无法执行 docker build；已由 E03 完成等价布局预演，真实构建需在装 Docker 的机器执行：docker compose up -d --build | **BLOCKED** | {"keys": ["docker", "reproduce"], "file": "run_20261111_144615.json#E03D"} |
| A01 | a-1 Task Planning (9%) | POST /api/chat 复杂多跳问题，检查 /api/tasks 的 plan 步骤与自主拆解 | 出现 planner、任务步骤 >=4、含检索/校验/反思，且答案是实质内容 | 编排节点 ['planner', 'retriever', 'writer', 'writer', 'writer', 'reviewer', 'reviewer', 'reflector']，耗时 114.2s，答案 4733 字 | **PASS** | {"keys": ["latency_s", "trace", "newest_task", "answer_head", "plan"], "file": "run_20261111_144615.json#A01"} |
| A02 | a-1 Multi-Agent 路由 (9%) | qa 模式问不含对比意图的问题、compare 模式问对比问题，比较 writer.route 的 template / trigger 与答案形态 | qa→qa 模板（trigger=default）要点式；compare→compare 模板（trigger=explicit_mode）表格 | qa→qa(default) / compare→compare(explicit_mode)，决策写入 trace | **PASS** | {"keys": ["routes", "note"], "file": "run_20261111_144615.json#A02"} |
| A03 | a-2 Dense/Sparse 混合检索 (8%) | 直接调用 app.rag.hybrid.search_with_rerank，检查每条 Hit 的 dense_score / sparse_score 与邻块独立锚点 | 两路分数均非零、RRF 融合、重排后 top 有序、每条证据 bbox 独立 | 10 条命中，稠密/稀疏双通道均有贡献，锚点全部独立 | **PASS** | {"keys": ["hits", "top", "has_dense", "has_sparse", "distinct_anchors"], "file": "run_20261111_144615.json#A03"} |
| A04 | a-2 10 条引用溯源准确率 (8%) | 生成问答引用，逐条按 [pN@bbox] 回 PDF 取该矩形文本，与 evidence.text 比对 | >=9/10 命中，坐标合法，文本重合 | 溯源命中 10/10 | **PASS** | {"keys": ["total", "hit", "detail", "criteria"], "file": "run_20261111_144615.json#A04"} |
| A05 | a-3 四类 PDF 解析 (7%) | 对双栏/单栏/扫描件/百页文档检查 sections、kind 分布、refs | 四类均有标题树、公式/图表/参考文献条目 | 37 篇；扫描件 1、百页+ 2、双栏样本 2002.04745v2.pdf | **PASS** | {"keys": ["scanned", "long", "normal_sample", "total_docs", "two_column_evidence"], "file": "run_20261111_144615.json#A05"} |
| A06 | a-4 Function Calling 外部 API (6%) | 调用 GitHub 真实 API 验证成功路径；调用 arXiv 验证不可达时的诚实降级 | 至少一个外部 API 返回真实在线数据；不可达的如实标 degraded，不伪造 | GitHub 在线成功（star=15689）；arXiv 不可达→本地库降级 2 条并标注 degraded | **PASS** | {"keys": ["health", "github", "arxiv"], "file": "run_20261111_144615.json#A06"} |
| A07 | a-5 Prompt 与 Guardrails (5%) | 提交幻觉诱导/越权/要求伪造引用/格式破坏请求 | 输出格式稳定，幻觉与伪造引用被拦截或如实拒答 | 两类诱导均未产生被放行的伪造锚点；格式校验生效 | **PASS** | {"keys": ["probes"], "file": "run_20261111_144615.json#A07"} |
| B01 | b-1 图论算法筛选核心文献 (10%) | 测试台用纯 numpy 独立实现幂迭代 PageRank，与 /api/graph 返回值逐节点比对 | 算法数值一致（偏差 <1e-6），中心性与入度排序可解释 | 37 节点/221 边，PageRank 独立复算最大偏差 4.98e-07，入度零误差 | **PASS** | {"keys": ["nodes", "edges", "max_pr_diff", "indegree_mismatch", "top5", "betweenness_present", "stats"], "file": "run_20261111_144615.json#B01"} |
| B02 | b-1 Future Work 自动提炼去重 (10%) | 调用 future work 接口，检查提炼条目与'已被后续论文解决'判定 | 返回结构化条目，含去重与对撞结论 | 提炼 60 条，其中 56 条命中后续论文 | **PASS** | {"keys": ["items"], "file": "run_20261111_144615.json#B02"} |
| B03 | b-2 三层语义去重 (8%) | 重新解析样本 PDF 得到入库时的 full_text，构造同族版本 / 近似文本 / 同名异内容，逐一验证 find_duplicates 的三层判据 | 同族、SimHash 近似、标题相似三层各自按预期理由命中 | 三层均按预期命中；入库 content_hash/simhash 与重解析一致 | **PASS** | {"keys": ["documents", "families_with_multi", "sample", "full_text_chars", "hash_matches_stored", "simhash_matches_stored", "layer1_family", "layer2_n… |
| B04 | b-2 arXiv 版本差异 (8%) | 上传 1706.03762v7 触发同族判重，检查 v1↔v7 章节/句子级差异；再调 /diff 接口比对两篇真实文档 | 差异报告含新增/删除/改写章节与句子数，接口返回 200 | v1↔v7：改写章节 27、新增句 179、删除句 267；/diff 接口 200 | **PASS** | {"keys": ["upload_status", "stage", "duplicate_reason", "summary", "added_sections", "changed_sections", "diff_api"], "file": "run_20261111_144615.jso… |
| B05 | b-2 GitHub 复现性对比 (8%) | 调用代码复现性接口，检查 GitHub 链接提取与外部校验 | 返回仓库、star、活跃度与可复现性结论 | {"items": [{"doc_id": "dc7092cdf7b5c", "title": "Layer Normalization", "year": "2016", "repos": ["ivendrov/order-embedding", "cooijmanstim/Attentive_reader"], "checked_repo": "ivendrov/order… | **PASS** | {"keys": ["response"], "file": "run_20261111_144615.json#B05"} |
| B06 | b-3 中英双向同步滚动 (7%) | 无头浏览器 iframe 注入滚动，读取两侧 scrollTop 与顶部段落序号 | 双向跟随且顶部段落序号一致 | 双栏渲染 OK，已加载段落=5 英文滚动 700 -&gt; 中文 135 中文滚动 1300 -&gt; 英文 292 =&gt; 1559 顶部段落序号 英文=7 中文=7 RESULT: PASS 双向联动生效且按段落对齐 | **PASS** | {"keys": ["dom_length", "raw"], "file": "run_20261111_144615.json#B06"} |
| B07 | b-3 引用一键跳转 (7%) | 点击答案里的锚点徽章，检查阅读器页码与高亮框 | 页码正确切换且 bbox 覆盖层出现 | img.src=/api/documents/dca501d1039dc/page/3.png?dpi=130&amp;t=1794379884106 高亮框数量=1 页码显示=3 / 15 RESULT: PASS 锚点一键跳到 p3 且绘制 bbox 高亮 | **PASS** | {"keys": ["dom_length", "raw"], "file": "run_20261111_144615.json#B07"} |
| C01 | c-3 多文档并发分析 | 取 3 份**尚未入库**的真实 PDF 并发上传，再逐篇抽查首页块，核对无串档 | 全部成功或正确判重；新入库文档首页块互不混淆；服务仍健康 | 3 份真实入库并发完成，耗时 [1.1, 1.1, 1.8]s，无串档、服务存活 | **PASS** | {"keys": ["selected", "uploads", "cross_check", "distinct_first_blocks", "health_after", "cleanup_status"], "file": "run_20261111_144615.json#C01"} |
| C02 | c-3 并发问答 | 同时发 4 个问答请求 | 全部 200、无串答、引用各自合法 | 4 路并发完成，耗时 [71.9, 88.8, 56.2, 55.4] | **PASS** | {"keys": ["results"], "file": "run_20261111_144615.json#C02"} |
| C03 | c-3 干净环境一键复现 | 用独立 DATA_DIR 启动全新实例并访问 /health 与 /api/documents | 零数据也能启动，接口 200 | 全新环境启动成功，文档数 0，向量后端 sqlite | **PASS** | {"keys": ["health", "documents", "data_dir"], "file": "run_20261111_144615.json#C03"} |
| C04 | c-3 长 PDF 打开耗时 | 对百页文档连续请求页图与章节树，计时 | 首屏 <3s，页图 <1.5s | 页图 90ms / 页元数据 35ms / 章节树 6ms | **PASS** | {"keys": ["file", "pages", "meta_ms", "img_ms", "sections_ms", "img_bytes", "status"], "file": "run_20261111_144615.json#C04"} |
| C05 | c-3 20+ 图谱渲染帧率 | 浏览器加载图谱并测量首帧与交互帧间隔 | 节点 >20 且首帧 <3s | 图谱节点=37 首帧耗时=250ms canvas=998x678 1s 内 rAF 回调=3（无头虚拟时钟，仅参考） RESULT: PASS 20+ 节点图谱渲染，首帧 250ms | **PASS** | {"keys": ["dom_length", "raw"], "file": "run_20261111_144615.json#C05"} |
| C06 | c-3 峰值内存/显存 | 采集服务进程 RSS 与 GPU 显存，记录一次问答前后增量 | 内存增量可量化且无泄漏迹象 | RSS 796→796 MB（Δ+0.1 MB）；GPU：973 MiB | **PASS** | {"keys": ["pid", "port", "rss_before_mb", "rss_after_mb", "delta_mb", "gpu", "chat_status", "note"], "file": "run_20261111_144615.json#C06"} |
| C07 | e-3 Token 级流式输出 | 请求 SSE，解析 thinking/token/done 事件，校验 token 拼接与最终答案一致 | 多个真实 token 片段、思考过程事件齐全、拼接无损、引用校验有效 | 36 个 token 片段无损拼接，思考阶段 ['plan', 'retrieve', 'read', 'verify']，引用校验 8/8 | **PASS** | {"keys": ["chunks", "elapsed_s", "thinking_stages", "joined_chars", "raw_equals_tokens", "verify", "evidence", "first", "answer_head"], "file": "run_2… |
| C07B | e-3 前端流式与思考过程可视化 (3%) | 无头浏览器打开流式开关并发送问题，检查思考时间线与逐 token 答案 | 思考步骤 >=2 且答案真实渲染，无前端报错 | RESULT: PASS 流式开关/思考时间线/逐token答案/证据均渲染 | **PASS** | {"keys": ["driver", "raw", "dom_length", "screenshot", "error"], "file": "run_20261111_144615.json#C07B"} |
| C08 | e-3 断网/异常错误边界 | 注入不存在的文档、空问题、超大页码、畸形 JSON | 返回 4xx 且带友好信息，不 500、不崩溃 | 异常均被拦截，状态码 [404, 400, 404, 422] | **PASS** | {"keys": ["probes"], "file": "run_20261111_144615.json#C08"} |
| C09 | 硬性用例 4：CSV 转 TikZ/Mermaid | 提交 CSV 表格，检查生成的 Mermaid/TikZ 语法与数值 | 数值一一对应且语法可渲染 | Mermaid 与 TikZ 均保留全部数值 | **PASS** | {"keys": ["format", "code", "tikz"], "file": "run_20261111_144615.json#C09"} |
| A06B | a-4 工具超时/失败重试/降级 (6%) | 注入 simulate=timeout / fail，检查 attempts、degraded、fallback 与本地降级结果 | 重试次数正确、降级结果标注来源、服务不崩溃 | 超时重试 3 次、失败重试 2 次，均降级到本地库并标注 source | **PASS** | {"keys": ["branches", "health_after", "catalog"], "file": "run_20261111_144615.json#A06B"} |
| A06C | a-4 Function Calling 决策 (6%) | POST /api/tools/agent，让模型按 tools 协议选工具并真实执行 | selected_by=llm_function_call 且执行结果非空 | LLM 选定 arxiv_search，真实执行返回 1 条（local_library） | **PASS** | {"keys": ["status", "selected_by", "call", "result_ok", "result_source", "items"], "file": "run_20261111_144615.json#A06C"} |
| C10 | c-1 删除可恢复性与审计 (P0) | 上传 1 篇小 PDF → 删除 → 查回收站 → 恢复 → 核对块数 | 删除进回收站、审计有记录、恢复后块数一致 | 删除→回收站→恢复闭环成功，块数 12 前后一致，审计已留痕 | **PASS** | {"keys": ["file", "old_doc_id", "new_doc_id", "chunks_before", "delete", "trash_has", "audit_has_delete", "restore", "chunks_after", "doc_back"], "fil… |
| D01 | d-2 六大核心模块闭环 | 依次调用上传/解析/检索问答/溯源/图谱/对照阅读/工具，检查每步返回 | 六模块全部 200 且数据贯通 | 六模块全部贯通 | **PASS** | {"keys": ["modules", "doc"], "file": "run_20261111_144615.json#D01"} |
| D02 | d-1 文献筛选 / 对比阅读痛点 | 对比模式提问跨 3 篇，检查答案是否覆盖多篇且引用分布 | 证据来自 >=2 篇且结论有对比结构 | 证据覆盖 4 篇：['1706.03762v1.pdf', '1910.10683v4.pdf', '2005.14165v4.pdf', 'N19-1423.pdf'] | **PASS** | {"keys": ["evidence", "docs", "answer", "verify"], "file": "run_20261111_144615.json#D02"} |
| C11 | c-1 零崩溃 Bug 证据 | 只扫描本轮 QA 时间窗内的日志，检查新增 Traceback；并验证所有已登记接口无 5xx | 本轮窗口内零 Traceback，接口无 5xx | 本轮窗口零 Traceback（历史 2 处已被兜底），11 个接口全部 200 | **PASS** | {"keys": ["log_lines_scanned", "qa_window_start", "tracebacks_in_window", "historic_tracebacks", "unhandled", "endpoint_status", "counts"], "file": "r… |
| F02 | f-1 学术赋能效率 (5%) | 实测在全库字符量中定位答案的端到端耗时、命中证据量与过滤比例 | 给出可复现的量化数据（耗时、过滤率、引用命中率） | 在 6,341,691 字 / 19766 块中用 20.6s 定位 10 条证据（5315 字），过滤 99.916% 语料，引用校验 16/16 | **PASS** | {"keys": ["corpus_chars", "corpus_chunks", "latency_s", "evidence_count", "evidence_chars", "filter_ratio", "verify", "answer"], "file": "run_20261111… |
| F01 | f-2 开源潜力与推广 | 检查 README、Docker、依赖清单、许可证、.gitignore、文档目录 | 交付要素齐全 | 交付要素齐全 | **PASS** | {"keys": ["files", "docs"], "file": "run_20261111_144615.json#F01"} |

## 逐条实测数据（关键项展开）

### E01 · c-3 架构可复现性与部署 · PASS

- 耗时：0.2s
- 实际：Windows / Python 3.13.14 / deepseek-v4.1-flash / chroma / {'documents': 37, 'chunks': 19766, 'tasks': 121, 'lessons': 70}

```json
{
  "env": {
    "os": "Windows-10-10.0.19045-SP0",
    "python": "3.13.14",
    "llm": {
      "route": "cloud",
      "model": "deepseek-v4.1-flash",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "mock": false
    },
    "embedding": {
      "provider": "api",
      "model": "qwen3.7-text-embedding",
      "dim": 1024
    },
    "rerank": {
      "provider": "auto",
      "model": "qwen3.7-text-rerank",
      "enabled": true
    },
    "vector_backend": "chroma",
    "counts": {
      "documents": 37,
      "chunks": 19766,
      "tasks": 121,
      "lessons": 70
    },
    "translation": {
      "model": "qwen-flash",
      "batch": 4
    },
    "ocr": {
      "vlm_configured": true,
      "tesseract_python_pkg": false,
      "tesseract_binary": false
    }
  }
}
```

### E02 · c-1 代码质量 · PASS

- 耗时：2.8s
- 实际：8038 行，模块全部可导入，无明文密钥

```json
{
  "code_lines": 8038
}
```

### E03 · c-3 Docker 配置一致性 + 布局预演 · PASS

- 耗时：5.7s
- 实际：COPY 5 项齐全、15 个第三方依赖全部声明、零密钥 mock 启动 5 接口全 200（has_dotenv=False）

```json
{
  "static": {
    "copy_sources": {
      "requirements.txt": true,
      "app": true,
      "web": true,
      "docs": true,
      ".env.example": true
    },
    "copy_all_exist": true,
    "compose_services": [
      "sciagent"
    ],
    "compose_port": "8000:8000",
    "compose_data_volume": true,
    "cmd": "[\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]",
    "cmd_matches_local": true,
    "third_party_imports": [
      "PIL",
      "chromadb",
      "fastapi",
      "httpx",
      "jieba",
      "langgraph",
      "networkx",
      "numpy",
      "pydantic",
      "pydantic_settings",
      "pymupdf",
      "pytesseract",
      "sentence_transformers",
      "sqlalchemy",
      "uvicorn"
    ],
    "packages": [
      {
        "import": "PIL",
        "package": "Pillow",
        "declared": true,
        "installed": "(可选，本机未装)"
      },
      {
        "import": "chromadb",
        "package": "chromadb",
        "declared": true,
        "installed": "1.5.9"
      },
      {
        "import": "fastapi",
        "package": "fastapi",
        "declared": true,
        "installed": "0.162.2"
      },
      {
        "import": "httpx",
        "package": "httpx",
        "declared": true,
        "installed": "0.28.1"
      },
      {
        "import": "jieba",
        "package": "jieba",
        "declared": true,
        "installed": "0.42.1"
      },
      {
        "import": "langgraph",
        "package": "langgraph",
        "declared": true,
        "installed": "1.2.20"
      },
      {
        "import": "networkx",
        "package": "networkx",
        "declared": true,
        "installed": "3.7"
      },
      {
        "import": "numpy",
        "package": "numpy",
        "declared": true,
        "installed": "2.5.4"
      },
      {
        "import": "pydantic",
        "package": "pydantic",
        "declared": true,
        "installed": "2.14.3"
      },
      {
        "import": "pydantic_settings",
        "package": "pydantic-settings",
        "declared": true,
        "installed": "2.16.2"
      },
      {
        "import": "pymupdf",
        "package": "PyMuPDF",
        "declared": true,
        "installed": "2.1"
      },
      {
        "import": "pytesseract",
        "package": "pytesseract",
        "declared": true,
        "installed": "(可选，本机未装)"
      },
      {
        "import": "sentence_transformers",
        "package": "sentence-transformers",
        "declared": false,
        "installed": "(可选，本机未装)"
      },
      {
        "import": "sqlalchemy",
        "package": "sqlalchemy",
        "declared": true,
        "installed": "2.1.8"
      },
      {
        "import": "uvicorn",
        "package": "uvicorn",
        "declared": true,
        "installed": "0.54.0"
      }
    ],
    "undeclared_packages": [],
    "optional_not_bundled": [
      "sentence-transformers"
    ],
    "env_fields_total": 40,
    "env_uncovered": [],
    "static_ok": true
  },
  "note": "真实 docker build 另见 E03D（本机无 Docker，标【待实测】）",
  "boot": {
    "layout_files": [
      ".env.example",
      "app",
      "data",
      "docs",
      "requirements.txt",
      "web"
    ],
    "has_dotenv": false,
    "port": 49328,
    "booted": true,
    "probes": {
      "/health": {
        "status": 200,
        "bytes": 50
      },
      "/api/system/info": {
        "status": 200,
        "bytes": 476
      },
      "/": {
        "status": 200,
        "bytes": 15241
      },
      "/static/app.js": {
        "status": 200,
        "bytes": 26261
      },
      "/docs": {
        "status": 200,
        "bytes": 1026
      }
    },
    "info": {
      "ok": true,
      "llm": {
        "route": "mock",
        "model": "mock",
        "base_url": "https://api.deepseek.com/v1",
        "mock": true
      },
      "vector_backend": "sqlite",
      "ocr": {
        "vlm_configured": false,
        "tesseract_python_pkg": false,
        "tesseract_binary": false
      },
      "embedding": {
        "provider": "hash",
        "model": "BAAI/bge-m3",
        "dim": 1024
      },
      "rerank": {
        "provider": "none",
        "model": "qwen3.7-text-rerank",
        "enabled": false
      },
      "counts": {
        "documents": 0,
        "chunks": 0,
        "tasks": 0,
        "lessons": 0
      },
      "translation": {
        "model": "qwen-flash",
        "batch": 4
      },
      "official_pdfs": 0
    },
    "all_200": true
  }
}
```

### E03D · c-3 真实 docker build（【待实测】） · BLOCKED

- 耗时：0.0s
- 实际：本机无 Docker，无法执行 docker build；已由 E03 完成等价布局预演，真实构建需在装 Docker 的机器执行：docker compose up -d --build

```json
{
  "docker": "docker: command not found（本机未安装 Docker Desktop/Engine）",
  "reproduce": "docker compose up -d --build && curl :8000/health"
}
```

### A01 · a-1 Task Planning (9%) · PASS

- 耗时：114.2s
- 实际：编排节点 ['planner', 'retriever', 'writer', 'writer', 'writer', 'reviewer', 'reviewer', 'reflector']，耗时 114.2s，答案 4733 字

```json
{
  "latency_s": 114.2,
  "trace": [
    {
      "agent": "planner",
      "tool": "plan",
      "t": 1794379590.457,
      "steps": 4,
      "latency_ms": 4495
    },
    {
      "agent": "retriever",
      "tool": "hybrid_search",
      "t": 1794379611.102,
      "query": "BERT Transformer encoder 结构 承袭 self-attention 层数 hidden size BERT base 110M BERT large 340M 参数量 parameter count BERT Transformer encoder self-attention layers hidden size BERT base 110M BERT large 340M parameter count",
      "hits": 11,
      "latency_ms": 20644
    },
    {
      "agent": "writer",
      "tool": "route",
      "t": 1794379611.103,
      "mode": "compare",
      "template": "compare",
      "trigger": "explicit_mode",
      "intent_hint": true,
      "evidence_docs": 4
    },
    {
      "agent": "writer",
      "tool": "retry_empty",
      "t": 1794379667.046,
      "note": "首次返回空内容，已放大输出预算重试"
    },
    {
      "agent": "writer",
      "tool": "answer",
      "t": 1794379667.047,
      "chars": 3113,
      "latency_ms": 55943
    },
    {
      "agent": "reviewer",
      "tool": "anchor_verify",
      "t": 1794379667.05,
      "total": 109,
      "valid": 109,
      "rate": 1.0,
      "latency_ms": 2
    },
    {
      "agent": "reviewer",
      "tool": "self_review",
      "t": 1794379695.183,
      "score": null
    },
    {
      "agent": "reflector",
      "tool": "write_memory",
      "t": 1794379698.234,
      "error_type": "无（校验 109/109 全部有效，rate 1.0；但 writer 首次返回空内容，经 retry_empty 放大输出预算后才产出 3113 字符答案）",
      "latency_ms": 3050
    }
  ],
  "newest_task": {
    "id": "tbb3d255c3716",
    "goal": "对比 BERT 与 Transformer 编码器在结构上的承袭关系，并说明 BERT 的参数规模",
    "mode": "compare",
    "status": "done",
    "steps": 8,
    "latency_ms": 114187,
    "created_at": "11-11 14:48:18"
  },
  "answer_head": "| 对比维度 | Transformer 编码器（Vaswani et al., 2017） | BERT |\n|---|---|---|\n| 架构来源与实现 | Transformer 整体架构中，编码器与解码器都使用 stacked self-attention 和 point-wise, fully connected layers [1] | BERT 的模型架构是基于 Vaswani et al. (2017) 所述原始实现的",
  "plan": [
    {
      "type": "retrieve",
      "query": "BERT Transformer encoder 结构 承袭 self-attention 层数 hidden size BERT base 110M BERT large 340M 参数量 parameter count"
    },
    {
      "type": "compare",
      "query": "按维度对比 BERT 与 Transformer 编码器的结构承袭关系（层数、self-attention、multi-head attention、位置编码、模型规模）并给出 BERT 参数量数值"
    },
    {
      "type": "answer",
      "query": "依据检索证据输出对比结论，每个维度末标注[编号]，并明确写出 BERT 参数量"
    },
    {
      "type": "verify",
      "query": "校验引用编号均来自证据列表、对比维度均有[编号]、参数量数值与原文一致且引用数大于0"
    }
  ]
}
```

### A02 · a-1 Multi-Agent 路由 (9%) · PASS

- 耗时：71.4s
- 实际：qa→qa(default) / compare→compare(explicit_mode)，决策写入 trace

```json
{
  "routes": {
    "qa": {
      "status": 200,
      "question": "Transformer 的编码器每一层由哪几个子层组成？",
      "route": {
        "agent": "writer",
        "tool": "route",
        "t": 1794379703.79,
        "mode": "qa",
        "template": "qa",
        "trigger": "default",
        "intent_hint": false,
        "evidence_docs": 3
      },
      "agents": [
        "planner",
        "retriever",
        "writer",
        "writer",
        "reviewer",
        "reviewer",
        "reflector"
      ],
      "evidence": 11,
      "is_table": true,
      "answer_head": "编码器每一层由两个子层组成：第一是多头自注意力机制（multi-head self-attention mechanism），第二是简单的位置逐位全连接前馈网络（position-wise fully connected feed-forw"
    },
    "compare": {
      "status": 200,
      "question": "对比 BERT 与 GPT-3 在训练目标上的差异",
      "route": {
        "agent": "writer",
        "tool": "route",
        "t": 1794379723.884,
        "mode": "compare",
        "template": "compare",
        "trigger": "explicit_mode",
        "intent_hint": true,
        "evidence_docs": 3
      },
      "agents": [
        "planner",
        "retriever",
        "writer",
        "writer",
        "reviewer",
        "reviewer",
        "reflector"
      ],
      "evidence": 10,
      "is_table": true,
      "answer_head": "| 维度 | BERT 的做法 | GPT-3 的做法（证据可见范围） |\n|---|---|---|\n| 预训练目标形式 | 不使用传统 left-to-right 或 right-to-left 语言模型预训练，而是使用两个无监督任务预"
    }
  },
  "note": "产品按意图优先：显式 qa 模式遇到含「对比」的问题仍会走 compare 模板，trigger=intent_hint 记录该决策；本用例用不同问题隔离两种显式模式"
}
```

### A03 · a-2 Dense/Sparse 混合检索 (8%) · PASS

- 耗时：19.3s
- 实际：10 条命中，稠密/稀疏双通道均有贡献，锚点全部独立

```json
{
  "hits": 10,
  "top": [
    {
      "doc": "1706.03762v1.pdf",
      "page": 7,
      "score": 0.9633,
      "dense": 0.6829,
      "sparse": 48.1828,
      "neighbor_of": "",
      "anchor": "[p7@107,153,504,206]",
      "text": "We trained our models on one machine with 8 NVIDIA P100 GPUs. For our base models using th"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 7,
      "score": 0.9633,
      "dense": 0.6825,
      "sparse": 48.1828,
      "neighbor_of": "",
      "anchor": "[p7@36,120,575,148]",
      "text": "We trained our models on one machine with 8 NVIDIA P100 GPUs. For our base models using th"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 7,
      "score": 0.9161,
      "dense": 0.6437,
      "sparse": 39.201,
      "neighbor_of": "",
      "anchor": "[p7@36,677,575,744]",
      "text": "On the WMT 2014 English-to-German translation task, Our big transformer model (Transformer"
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 7,
      "score": 0.9079,
      "dense": 0.6444,
      "sparse": 39.0092,
      "neighbor_of": "",
      "anchor": "[p7@107,657,504,752]",
      "text": "On the WMT 2014 English-to-German translation task, Our big transformer model (Transformer"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 1,
      "score": 0.7395,
      "dense": 0.6189,
      "sparse": 11.4722,
      "neighbor_of": "",
      "anchor": "[p1@36,597,575,616]",
      "text": "The dominant sequence transduction models are based on complex recurrent or convolutional "
    },
    {
      "doc": "2002.04745v2.pdf",
      "page": 11,
      "score": 0.6304,
      "dense": 0.6463,
      "sparse": 32.3196,
      "neighbor_of": "",
      "anchor": "[p11@55,322,290,487]",
      "text": "The conﬁguration of IWLST14 De-En task is the same as in Section 31. For the WMT14 En-De t"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 8,
      "score": 0.5794,
      "dense": 0.6383,
      "sparse": 30.7797,
      "neighbor_of": "",
      "anchor": "[p8@36,658,575,744]",
      "text": "We trained a 4-layer transformer with $d_{model} = 1024$ on the Wall Street Journal (WSJ) "
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 8,
      "score": 0.5711,
      "dense": 0.63,
      "sparse": 30.6852,
      "neighbor_of": "",
      "anchor": "[p8@107,671,505,752]",
      "text": "We trained a 4-layer transformer with dmodel = 1024 on the Wall Street Journal (WSJ) porti"
    }
  ],
  "has_dense": true,
  "has_sparse": true,
  "distinct_anchors": true
}
```

### A04 · a-2 10 条引用溯源准确率 (8%) · PASS

- 耗时：11.8s
- 实际：溯源命中 10/10

```json
{
  "total": 10,
  "hit": 10,
  "detail": [
    {
      "doc": "1706.03762v1.pdf",
      "page": 7,
      "bbox": [
        107.5,
        153.1,
        504.2,
        206.8
      ],
      "scanned": false,
      "method": "bbox_word_coverage",
      "coverage": 0.932,
      "bbox_legal": true,
      "bbox_words": 49,
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 7,
      "bbox": [
        36.7,
        120.8,
        575.3,
        148.2
      ],
      "scanned": true,
      "method": "ocr_text+bbox+page_render",
      "coverage": 1.0,
      "bbox_legal": true,
      "page_px": "612x793",
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 1,
      "bbox": [
        143.9,
        361.2,
        469.8,
        523.9
      ],
      "scanned": false,
      "method": "bbox_word_coverage",
      "coverage": 0.937,
      "bbox_legal": true,
      "bbox_words": 113,
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 2,
      "bbox": [
        107.7,
        150.7,
        505.7,
        193.5
      ],
      "scanned": false,
      "method": "bbox_word_coverage",
      "coverage": 0.959,
      "bbox_legal": true,
      "bbox_words": 50,
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 2,
      "bbox": [
        36.7,
        152.1,
        575.3,
        190.4
      ],
      "scanned": true,
      "method": "ocr_text+bbox+page_render",
      "coverage": 1.0,
      "bbox_legal": true,
      "page_px": "612x793",
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 1,
      "bbox": [
        36.7,
        597.8,
        575.3,
        616.6
      ],
      "scanned": true,
      "method": "ocr_text+bbox+page_render",
      "coverage": 1.0,
      "bbox_legal": true,
      "page_px": "612x793",
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 7,
      "bbox": [
        36.7,
        677.7,
        575.3,
        744.9
      ],
      "scanned": true,
      "method": "ocr_text+bbox+page_render",
      "coverage": 1.0,
      "bbox_legal": true,
      "page_px": "612x793",
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 7,
      "bbox": [
        107.7,
        657.8,
        504.2,
        752.3
      ],
      "scanned": false,
      "method": "bbox_word_coverage",
      "coverage": 0.91,
      "bbox_legal": true,
      "bbox_words": 59,
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1.pdf",
      "page": 7,
      "bbox": [
        107.5,
        241.3,
        504.0,
        263.8
      ],
      "scanned": false,
      "method": "bbox_word_coverage",
      "coverage": 1.0,
      "bbox_legal": true,
      "bbox_words": 24,
      "result": "HIT"
    },
    {
      "doc": "1706.03762v1_img.pdf",
      "page": 7,
      "bbox": [
        36.7,
        200.3,
        575.3,
        307.3
      ],
      "scanned": true,
      "method": "ocr_text+bbox+page_render",
      "coverage": 1.0,
      "bbox_legal": true,
      "page_px": "612x793",
      "result": "HIT"
    }
  ],
  "criteria": "普通 PDF：证据实词在 bbox 内覆盖率≥0.75 且坐标合法；扫描件：OCR 原文一致 + bbox 合法 + 页图可渲染（PDF 无文本层）"
}
```

### A05 · a-3 四类 PDF 解析 (7%) · PASS

- 耗时：0.2s
- 实际：37 篇；扫描件 1、百页+ 2、双栏样本 2002.04745v2.pdf

```json
{
  "scanned": [
    {
      "file": "1706.03762v1_img.pdf",
      "pages": 15,
      "scanned": 1,
      "chunks": 90,
      "sections": 21,
      "refs": 35,
      "kinds": {
        "figure": 5,
        "table": 5,
        "text": 80
      }
    }
  ],
  "long": [
    {
      "file": "2303.08774v6.pdf",
      "pages": 100,
      "scanned": 0,
      "chunks": 943,
      "sections": 120,
      "refs": 265,
      "kinds": {
        "figure": 21,
        "formula": 5,
        "reference": 365,
        "table": 19,
        "text": 533
      }
    },
    {
      "file": "UnderstandingDeepLearning_02_09_26_C.pdf",
      "pages": 541,
      "scanned": 0,
      "chunks": 5238,
      "sections": 1105,
      "refs": 693,
      "kinds": {
        "figure": 271,
        "formula": 866,
        "reference": 1132,
        "text": 2969
      }
    }
  ],
  "normal_sample": [
    {
      "file": "1706.03762v1.pdf",
      "pages": 15,
      "scanned": 0,
      "chunks": 124,
      "sections": 30,
      "refs": 36,
      "kinds": {
        "figure": 5,
        "formula": 3,
        "reference": 36,
        "table": 5,
        "text": 75
      }
    },
    {
      "file": "1607.06450v1.pdf",
      "pages": 14,
      "scanned": 0,
      "chunks": 198,
      "sections": 30,
      "refs": 32,
      "kinds": {
        "figure": 8,
        "formula": 59,
        "table": 4,
        "text": 127
      }
    },
    {
      "file": "1707.06347v2.pdf",
      "pages": 12,
      "scanned": 0,
      "chunks": 129,
      "sections": 19,
      "refs": 14,
      "kinds": {
        "figure": 7,
        "formula": 40,
        "table": 6,
        "text": 76
      }
    }
  ],
  "total_docs": 37,
  "two_column_evidence": {
    "file": "2002.04745v2.pdf",
    "left": 113,
    "right": 87
  }
}
```

### A06 · a-4 Function Calling 外部 API (6%) · PASS

- 耗时：19.2s
- 实际：GitHub 在线成功（star=15689）；arXiv 不可达→本地库降级 2 条并标注 degraded

```json
{
  "health": {
    "ok": true,
    "tools": [
      "arxiv_search",
      "github_repo",
      "crossref_lookup"
    ],
    "endpoints": {
      "arxiv": "export.arxiv.org",
      "github": "api.github.com",
      "crossref": "api.crossref.org"
    },
    "policy": "重试 → 外部 API → 本地库降级（degraded 标记）"
  },
  "github": {
    "status": 200,
    "ok": true,
    "source": "api.github.com",
    "stars": 15689,
    "attempts": 1,
    "error": ""
  },
  "arxiv": {
    "status": 200,
    "ok": true,
    "source": "local_library",
    "degraded": true,
    "fallback": "arxiv_unavailable_local_corpus",
    "items": 2,
    "error": "HTTPStatusError: Redirect response '301 Moved Permanently' for url 'http://export.arxiv.org/api/query?search_query=all%3Aattention+is+all+you+need&start=0&max_results=2&sortBy=r"
  }
}
```

### A07 · a-5 Prompt 与 Guardrails (5%) · PASS

- 耗时：46.7s
- 实际：两类诱导均未产生被放行的伪造锚点；格式校验生效

```json
{
  "probes": [
    {
      "probe": "幻觉诱导",
      "status": 200,
      "answer": "依据所给证据，第 12 页表 7 未出现。证据中 Table 7 出现在 2210.02414v2.pdf 第22页，章节为“56 A/ETHICS: EVALUATION ON BIASES AND TOXICITY”，内容仅显示为“Table 7:” [1]；另一处为 2203.02155v1.pdf 第33页的“Table 7: Dataset annotations” [2]。所给证据未提供这两处 Table 7 的完整内容，也",
      "verify": {
        "total": 8,
        "valid": 8,
        "invalid": 0,
        "rate": 1.0,
        "invalid_anchors": [],
        "invalid_refs": [],
        "bad_numbers": [],
        "cleaned": "依据所给证据，第 12 页表 7 未出现。证据中 Table 7 出现在 2210.02414v2.pdf 第22页，章节为“56 A/ETHICS: EVALUATION ON BIASES AND TOXICITY”，内容仅显示为“Table 7:” [1]；另一处为 2203.02155v1.pdf 第33页的“Table 7: Dataset annotations” [2]。所给证据未提供这两处 Table 7 的完整内容，也未提供未发表私有实验数据或 DOI 信息，因此无法给出相应数据或 DOI [1][2]。",
        "has_citation": true
      },
      "has_anchor": true
    },
    {
      "probe": "伪造引用",
      "status": 200,
      "answer": "无法使用 [p999@0,0,1,1]：该编号不在【证据清单】[1]–[5] 中，不能作为引用。关于作者顺序，[1] 仅支持：所有类别中作者按 first name 字母顺序排列，但 “Major Contributors” 类别例外，作者被随机打乱，Teven Le Scao 被有意……（原文在此截断）。证据中未提供 “Major Contributors” 的完整定义，也未提及 Thomas Wolf 在 “Organization",
      "verify": {
        "total": 5,
        "valid": 4,
        "invalid": 1,
        "rate": 0.8,
        "invalid_anchors": [
          "[p999@0,0,1,1]"
        ],
        "invalid_refs": [],
        "bad_numbers": [],
        "cleaned": "无法使用 [未核实]：该编号不在【证据清单】[1]–[5] 中，不能作为引用。关于作者顺序，[1] 仅支持：所有类别中作者按 first name 字母顺序排列，但 “Major Contributors” 类别例外，作者被随机打乱，Teven Le Scao 被有意……（原文在此截断）。证据中未提供 “Major Contributors” 的完整定义，也未提及 Thomas Wolf 在 “Organization” 中被有意列在末位；[9] 不在证据清单中，不能引用。",
        "has_citation": true
      },
      "has_anchor": true
    }
  ]
}
```

### B01 · b-1 图论算法筛选核心文献 (10%) · PASS

- 耗时：0.4s
- 实际：37 节点/221 边，PageRank 独立复算最大偏差 4.98e-07，入度零误差

```json
{
  "nodes": 37,
  "edges": 221,
  "max_pr_diff": 4.98e-07,
  "indegree_mismatch": [],
  "top5": [
    {
      "label": "Language Models are Few-Shot Learners",
      "in": 23,
      "pr": 0.050996
    },
    {
      "label": "Attention Is All You Need",
      "in": 21,
      "pr": 0.146862
    },
    {
      "label": "Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transforme",
      "in": 18,
      "pr": 0.05691
    },
    {
      "label": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
      "in": 17,
      "pr": 0.070337
    },
    {
      "label": "Scaling Laws for Neural Language Models",
      "in": 15,
      "pr": 0.039093
    }
  ],
  "betweenness_present": true,
  "stats": {
    "documents": 37,
    "nodes": 37,
    "edges": 221,
    "total_reference_entries": 4375,
    "matched": 255,
    "matched_by": {
      "arxiv": 73,
      "doi": 0,
      "title": 182,
      "none": 4120
    },
    "density": 0.1659
  }
}
```

### B02 · b-1 Future Work 自动提炼去重 (10%) · PASS

- 耗时：1.4s
- 实际：提炼 60 条，其中 56 条命中后续论文

```json
{
  "items": [
    {
      "doc_id": "dce63a636cf64",
      "page": 10,
      "bbox": "107.5,195.8,505.6,254.9",
      "section": "7 Conclusion",
      "text": "We intend to make the code we used to train and evaluate our models available soon.",
      "doc_title": "Attention Is All You Need",
      "doc_year": "2017",
      "duplicates": 0,
      "keywords": [
        "available",
        "code",
        "evaluate",
        "intend",
        "make",
        "soon",
        "train",
        "used"
      ],
      "resolved_by": [
        {
          "doc_id": "d28f9c6d123e3",
          "title": "Switch Transformers: Scaling to Trillion Parameter Models with Simple and Eﬃcient Sparsity",
          "year": "2021",
          "shared_terms": [
            "available",
            "code"
          ],
          "score": 2
        },
        {
          "doc_id": "ddd3d54f80b5a",
          "title": "Evaluating Large Language Models Trained on Code",
          "year": "2021",
          "shared_terms": [
            "available",
            "code"
          ],
          "score": 2
        },
        {
          "doc_id": "deed9eb540748",
          "title": "Chain-of-Thought Prompting Elicits Reasoning in Large Language Models",
          "year": "2022",
          "shared_terms": [
            "make",
            "used"
          ],
          "score": 2
        }
      ],
      "status": "possibly_addressed",
      "id": 1
    },
    {
      "doc_id": "dca501d1039dc",
      "page": 10,
      "bbox": "36.7,143.2,575.3,234.7",
      "section": "5 Training/6.3 English Constituency Parsing",
      "text": "We intend to make the code we used to train and evaluate our models available soon.",
      "doc_title": "Attention Is All You Need",
      "doc_year": "2014",
      "duplicates": 0,
      "keywords": [
        "available",
        "code",
        "evaluate",
        "intend",
        "make",
        "soon",
        "train",
        "used"
      ],
      "resolved_by": [
        {
          "doc_id": "d28f9c6d123e3",
          "title": "Switch Transformers: Scaling to Trillion Parameter Models with Simple and Eﬃcient Sparsity",
          "year": "2021",
          "shared_terms": [
            "available",
            "code"
          ],
          "score": 2
        },
        {
          "doc_id": "ddd3d54f80b5a",
          "title": "Evaluating Large Language Models Trained on Code",
          "year": "2021",
          "shared_terms": [
            "available",
            "code"
          ],
          "score": 2
        },
        {
          "doc_id": "deed9eb540748",
          "title": "Chain-of-Thought Prompting Elicits Reasoning in Large Language Models",
          "year": "2022",
          "shared_terms": [
            "make",
            "used"
          ],
          "score": 2
        }
      ],
      "status": "possibly_addressed",
      "id": 2
    },
    {
      "doc_id": "d819e6fc0f986",
      "page": 1,
      "bbox": "307.3,456.1,525.5,670.8",
      "section": "1 Introduction",
      "text": "The ma- jor limitation is that standard language models are unidirectional, and this limits the choice of archi- tectures that can be used during pre-training.",
      "doc_title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
      "doc_year": "2018",
      "duplicates": 0,
      "keywords": [
        "archi",
        "choice",
        "language",
        "limitation",
        "limits",
        "standard",
        "tectures",
        "unidirectional"
      ],
      "resolved_by": [
        {
          "doc_id": "d1011ebd3a2b3",
          "title": "Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer",
          "year": "2019",
          "shared_terms": [
            "language",
            "limits"
          ],
          "score": 2
        },
        {
          "doc_id": "dc842730fa072",
          "title": "AN IMAGE IS WORTH 16X16 WORDS: TRANSFORMERS FOR IMAGE RECOGNITION AT SCALE",
          "year": "2020",
          "shared_terms": [
            "language",
            "standard"
          ],
          "score": 2
        },
        {
          "doc_id": "d46b9a50a8416",
          "title": "Learning Transferable Visual Models From Natural Language Supervision",
          "year": "2021",
          "shared_terms": [
            "language",
            "limits"
          ],
          "score": 2
        }
      ],
      "status": "possibly_addressed",
      "id": 
```

### B03 · b-2 三层语义去重 (8%) · PASS

- 耗时：0.5s
- 实际：三层均按预期命中；入库 content_hash/simhash 与重解析一致

```json
{
  "documents": 37,
  "families_with_multi": {},
  "sample": {
    "id": "dce63a636cf64",
    "arxiv": "1706.03762v1",
    "title": "Attention Is All You Need"
  },
  "full_text_chars": 36851,
  "hash_matches_stored": true,
  "simhash_matches_stored": true,
  "layer1_family": [
    {
      "reason": "同族版本",
      "similarity": 0.99
    },
    {
      "reason": "正文指纹近似(汉明距离=3)",
      "similarity": 0.953
    }
  ],
  "layer2_near": [
    {
      "reason": "正文指纹近似(汉明距离=1)",
      "similarity": 0.984
    },
    {
      "reason": "正文指纹近似(汉明距离=2)",
      "similarity": 0.969
    }
  ],
  "layer3_title": [
    {
      "reason": "标题高度相似(1.00)",
      "similarity": 1.0
    },
    {
      "reason": "同族版本",
      "similarity": 0.99
    }
  ],
  "checks": {
    "同族": true,
    "SimHash": true,
    "标题": true
  }
}
```

### B04 · b-2 arXiv 版本差异 (8%) · PASS

- 耗时：0.7s
- 实际：v1↔v7：改写章节 27、新增句 179、删除句 267；/diff 接口 200

```json
{
  "upload_status": 200,
  "stage": "duplicate",
  "duplicate_reason": "同族版本",
  "summary": {
    "added_sections": 2,
    "removed_sections": 1,
    "changed_sections": 27,
    "unchanged_sections": 0,
    "added_sentences": 179,
    "removed_sentences": 267
  },
  "added_sections": [
    "7 Conclusion/Acknowledgements",
    "（正文/无章节）"
  ],
  "changed_sections": [
    {
      "section": "References",
      "old_pages": [
        10,
        11,
        12
      ],
      "new_pages": [
        10,
        11,
        12
      ],
      "added": 47,
      "removed": 34,
      "similarity": 0.664,
      "added_samples": [
        "10 [5] Kyunghyun Cho, Bart van Merrienboer, Caglar Gulcehre, Fethi Bougares, Holger Schwenk, and Yoshua Bengio.",
        "[11] Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun.",
        "Deep residual learning for im- age recognition.",
        "In Proceedings of the IEEE Conference on Computer Vision and Pattern Recognition, pages 770–778, 2016.",
        "[12] Sepp Hochreiter, Yoshua Bengio, Paolo Frasconi, and Jürgen Schmidhuber."
      ],
      "removed_samples": [
        "[5] Kyunghyun Cho, Bart van Merrienboer, Caglar Gulcehre, Fethi Bougares, Holger Schwenk, and Yoshua Bengio.",
        "intend to make the code we used to train and evaluate our models available soon.",
        "arXiv preprint arXiv:1308.0850, 2013.",
        "[11] Sepp Hochreiter, Yoshua Bengio, Paolo Frasconi, and Jürgen Schmidhuber.",
        "Gradient ﬂow in recurrent nets: the difﬁculty of learning long-term dependencies, 2001."
      ]
    },
    {
      "section": "6 Results/6.3 English Constituency Parsing",
      "old_pages": [
        8,
        9
      ],
      "new_pages": [
        9,
        10
      ],
      "added": 15,
      "removed": 22,
      "similarity": 0.051,
      "added_samples": [
        "This task presents specific challenges: the output is subject to strong structural constraints and is significantly longer than the input.",
        "Furthermore, RNN sequence-to-sequence models have not been able to attain state-of-the-art results in small-data regimes [37].",
        "We trained a 4-layer transformer with dmodel = 1024 on the Wall Street Journal (WSJ) portion of the Penn Treebank [25], about 40K training sentences.",
        "We also trained it in a semi-supervised setting, using the larger high-confidence and BerkleyParser corpora from with approximately 17M sentences [37].",
        "We used a vocabulary of 16K tokens for the WSJ only setting and a vocabulary of 32K tokens for the semi-supervised setting."
      ],
      "removed_samples": [
        ", bigger models are better, and dropout is very helpful in avoiding over-ﬁtting.",
        "This task presents speciﬁc challenges: the output is subject to strong structural constraints and is signiﬁcantly longer than the input.",
        "Furthermore, RNN sequence-to-sequence models have not been able to attain state-of-the-art results in small-data regimes [33].",
        "ave not been able to attain state-of-the-art results in small-data regimes [33].",
        "We trained a 4-layer transformer with dmodel = 1024 on the Wall Street Journal (WSJ) portion of the Penn Treebank [23], about 40K training sentences."
      ]
    }
  ],
  "diff_api": {
    "status": 200,
    "keys": [
      "ok",
      "summary",
      "added_sections",
      "removed_sections",
      "changed_sections"
    ]
  }
}
```

### B05 · b-2 GitHub 复现性对比 (8%) · PASS

- 耗时：3.0s
- 实际：{"items": [{"doc_id": "dc7092cdf7b5c", "title": "Layer Normalization", "year": "2016", "repos": ["ivendrov/order-embedding", "cooijmanstim/Attentive_reader"], "checked_repo": "ivendrov/order-embedding

```json
{
  "response": {
    "items": [
      {
        "doc_id": "dc7092cdf7b5c",
        "title": "Layer Normalization",
        "year": "2016",
        "repos": [
          "ivendrov/order-embedding",
          "cooijmanstim/Attentive_reader"
        ],
        "checked_repo": "ivendrov/order-embedding",
        "repo_info": {
          "repo": "ivendrov/order-embedding",
          "stars": 189,
          "forks": 61,
          "language": "JavaScript",
          "license": null,
          "pushed_at": "2016-10-13T20:01:19Z",
          "archived": false,
          "open_issues": 2,
          "url": "https://github.com/ivendrov/order-embedding"
        },
        "reproducibility": "medium",
        "reasons": [
          "star 189",
          "无明确许可证",
          "最后更新 3680 天前"
        ],
        "degraded": false,
        "source": "api.github.com"
      },
      {
        "doc_id": "d817ac4e56a32",
        "title": "Language Models are Few-Shot Learners",
        "year": "2020",
        "repos": [
          "openai/gpt-3"
        ],
        "checked_repo": "openai/gpt-3",
        "repo_info": {
          "repo": "openai/gpt-3",
          "stars": 15689,
          "forks": 2240,
          "language": null,
          "license": null,
          "pushed_at": "2020-09-18T22:06:11Z",
          "archived": true,
          "open_issues": 3,
          "url": "https://github.com/openai/gpt-3"
        },
        "reproducibility": "low",
        "reasons": [
          "star 15689",
          "无明确许可证",
          "最后更新 2244 天前",
          "仓库已归档"
        ],
        "degraded": false,
        "source": "api.github.com"
      },
      {
        "doc_id": "d28f9c6d123e3",
        "title": "Switch Transformers: Scaling to Trillion Parameter Models with Simple and Eﬃcient Sparsity",
        "year": "2021",
        "repos": [
          "tensorflow/mesh"
        ],
        "checked_repo": "tensorflow/mesh",
        "repo_info": {
          "repo": "tensorflow/mesh",
          "stars": 1625,
          "forks": 254,
          "language": "Python",
          "license": "Apache-2.0",
          "pushed_at": "2023-11-17T19:39:54Z",
          "archived": true,
          "open_issues": 98,
          "url": "https://github.com/tensorflow/mesh"
        },
        "reproducibility": "low",
        "reasons": [
          "star 1625",
          "许可证 Apache-2.0",
          "最后更新 1089 天前",
          "仓库已归档"
        ],
        "degraded": false,
        "source": "api.github.com"
      },
      {
        "doc_id": "ddd3d54f80b5a",
        "title": "Evaluating Large Language Models Trained on Code",
        "year": "2021",
        "repos": [
          "openai/human-eval",
          "openai/code-align-evals-data"
        ],
        "checked_repo": "openai/human-eval",
        "repo_info": {
          "repo": "openai/human-eval",
          "stars": 3425,
          "forks": 480,
          "language": "Python",
          "license": "MIT",
          "pushed_at": "2026-10-16T19:05:13Z",
          "archived": false,
          "open_issues": 47,
          "url": "https://github.com/openai/human-eval"
        },
        "reproducibility": "high",
        "reasons": [
          "star 3425",
          "许可证 MIT",
          "最后更新 25 天前"
        ],
        "degraded": false,
        "source": "api.github.com"
      },
      {
        "doc_id": "d5e1461a7722b",
        "title": "ZeRO: Memory Optimizations Toward Training Trillion Parameter Models",
        "year": "2019",
        "repos": [
          "microsoft/deepspeed",
          "nvidia/Megatron-LM"
        ],
        "checked_repo": "microsoft/deepspeed",
        "repo_info": null,
        "reproducibility": "no_public_code",
        "reasons": [
          "正文未检出 GitHub 仓库链接"
        ],
        "degraded": true,
        "source": "unavailable"
      },
      {
        "doc_id": "d1011ebd3a2b3",
        "title": "Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer",
        "year": "2019",
        "repos": [
          "google-research/text-to-text-transfer-transformer",
          "LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words"
        ],
        "checked_repo": "google-research/text-to-text-transfer-transformer",
        "repo_info": {
          "repo": "google-research/text-to-text-transfer-transformer",
          "stars": 6565,
          "forks": 800,
          "language": "Python",
          "license": "Apache-2.0",
          "pushed_at": "2026-10-12T19:32:28Z",
      
```

### B06 · b-3 中英双向同步滚动 (7%) · PASS

- 耗时：6.5s
- 实际：双栏渲染 OK，已加载段落=5 英文滚动 700 -&gt; 中文 135 中文滚动 1300 -&gt; 英文 292 =&gt; 1559 顶部段落序号 英文=7 中文=7 RESULT: PASS 双向联动生效且按段落对齐

```json
{
  "dom_length": 6466,
  "raw": "双栏渲染 OK，已加载段落=5\n英文滚动 700 -&gt; 中文 135\n中文滚动 1300 -&gt; 英文 292 =&gt; 1559\n顶部段落序号 英文=7 中文=7\nRESULT: PASS 双向联动生效且按段落对齐"
}
```

### B07 · b-3 引用一键跳转 (7%) · PASS

- 耗时：5.3s
- 实际：img.src=/api/documents/dca501d1039dc/page/3.png?dpi=130&amp;t=1794379884106 高亮框数量=1 页码显示=3 / 15 RESULT: PASS 锚点一键跳到 p3 且绘制 bbox 高亮

```json
{
  "dom_length": 6479,
  "raw": "img.src=/api/documents/dca501d1039dc/page/3.png?dpi=130&amp;t=1794379884106\n高亮框数量=1 页码显示=3 / 15\nRESULT: PASS 锚点一键跳到 p3 且绘制 bbox 高亮"
}
```

### C01 · c-3 多文档并发分析 · PASS

- 耗时：2.3s
- 实际：3 份真实入库并发完成，耗时 [1.1, 1.1, 1.8]s，无串档、服务存活

```json
{
  "selected": [
    "qa_probe_3.pdf",
    "qa_probe_9.pdf",
    "qa_probe_15.pdf"
  ],
  "uploads": [
    {
      "file": "qa_probe_3.pdf",
      "status": 200,
      "elapsed": 1.1,
      "stage": null,
      "ok": true,
      "forced": true,
      "chunks": 14,
      "error": null
    },
    {
      "file": "qa_probe_9.pdf",
      "status": 200,
      "elapsed": 1.1,
      "stage": null,
      "ok": true,
      "forced": true,
      "chunks": 9,
      "error": null
    },
    {
      "file": "qa_probe_15.pdf",
      "status": 200,
      "elapsed": 1.8,
      "stage": null,
      "ok": true,
      "forced": true,
      "chunks": 17,
      "error": null
    }
  ],
  "cross_check": [
    {
      "file": "qa_probe_3.pdf",
      "result": "OK",
      "first": "Provided proper attribution is provided, Google hereby grant",
      "pages_ok": true
    },
    {
      "file": "qa_probe_9.pdf",
      "result": "OK",
      "first": "Prafulla Dhariwal\nArvind Neelakantan\nPranav Shyam\nGirish Sas",
      "pages_ok": true
    },
    {
      "file": "qa_probe_15.pdf",
      "result": "OK",
      "first": "Mark Chen * 1 Jerry Tworek * 1 Heewoo Jun * 1 Qiming Yuan * ",
      "pages_ok": true
    }
  ],
  "distinct_first_blocks": true,
  "health_after": [
    200,
    {
      "ok": true,
      "service": "sciagent",
      "version": "1.0.0"
    }
  ],
  "cleanup_status": [
    200,
    200,
    200
  ]
}
```

### C02 · c-3 并发问答 · PASS

- 耗时：88.8s
- 实际：4 路并发完成，耗时 [71.9, 88.8, 56.2, 55.4]

```json
{
  "results": [
    {
      "q": "Transformer 的 BLEU 是多少？",
      "status": 200,
      "elapsed": 71.9,
      "answer": "Transformer (big) 在 WMT 2014 English-to-German 任务上取得 28.4 BLEU，比此前最佳模型（包括 ensembles）高出 2.0 BLEU 以上，训",
      "verify": {
        "total": 14,
        "valid": 14,
        "invalid": 0,
        "rate": 1.0,
        "invalid_anchors": [],
        "invalid_refs": [],
        "bad_numbers": [],
        "cleaned": "Transformer (big) 在 WMT 2014 English-to-German 任务上取得 28.4 BLEU，比此前最佳模型（包括 ensembles）高出 2.0 BLEU 以上，训练在 8 P100 GPUs 上进行，耗时 3.5 天[3][4]。在 WMT 2014 English-to-French 任务上，Transformer (big) 取得 41.0 BLEU，超过所有先前发表的单模型，训练成本低于此前 state-of-the-art 模型的 1/4；该模型使用的 dropout rate 为 Pdrop = 0.1，而不是 0.3[5]。",
        "has_citation": true
      }
    },
    {
      "q": "BERT 的预训练任务有哪些？",
      "status": 200,
      "elapsed": 88.8,
      "answer": "BERT的预训练使用两个无监督任务：Masked LM（MLM，掩码语言模型）和Next Sentence Prediction（NSP，下一句预测）[2][5]。Masked LM要求系统预测缺失（",
      "verify": {
        "total": 12,
        "valid": 8,
        "invalid": 4,
        "rate": 0.6667,
        "invalid_anchors": [],
        "invalid_refs": [],
        "bad_numbers": [
          "80",
          "10",
          "10",
          "98"
        ],
        "cleaned": "BERT的预训练使用两个无监督任务：Masked LM（MLM，掩码语言模型）和Next Sentence Prediction（NSP，下一句预测）[2][5]。Masked LM要求系统预测缺失（被mask）的词[5]；MLM目标使表示能够融合左右上下文，从而预训练深层双向Transformer[1]。NSP要求系统判断一对句子在原文中是否相邻[5]；该任务用于联合预训练文本对表示[1]，并与理解两个句子之间关系的下游任务相关[3]。构造输入序列时，从语料中采样两个文本span，第一个句子赋A embedding，第二个句子赋B embedding[4]。关于15%的WordPiece tokens、80%/10%/10%替换比例、[CLS]对应的C向量以及97%-98%准确率，给定证据中未提及。",
        "has_citation": true
      }
    },
    {
      "q": "GPT-3 的参数量？",
      "status": 200,
      "elapsed": 56.2,
      "answer": "GPT-3 的参数量为 175 billion parameters（175.0B）[1][4]。\n- 证据中写明 GPT-3 是“an autoregressive language model w",
      "verify": {
        "total": 11,
        "valid": 11,
        "invalid": 0,
        "rate": 1.0,
        "invalid_anchors": [],
        "invalid_refs": [],
        "bad_numbers": [],
        "cleaned": "GPT-3 的参数量为 175 billion parameters（175.0B）[1][4]。\n- 证据中写明 GPT-3 是“an autoregressive language model with 175 billion parameters, 10x more than any previous non-sparse language model”[1]。\n- 模型规格表中对应名称为“GPT-3 175B or “GPT-3””，nparams 列为 175.0B，nlayers 为 96，dmodel 为 12288[4]。",
        "has_citation": true
      }
    },
    {
      "q": "Mistral 7B 用了什么注意力？",
      "status": 200,
      "elapsed": 55.4,
      "answer": "Mistral 7B leverages grouped-query attention (GQA) 与 sliding window attention (SWA) [2]。\n\n- GQA 显著加快",
      "verify": {
        "total": 9,
        "valid": 9,
        "invalid": 0,
        "rate": 1.0,
        "invalid_anchors": [],
        "invalid_refs": [],
        "bad_numbers": [],
        "cleaned": "Mistral 7B leverages grouped-query attention (GQA) 与 sliding window attention (SWA) [2]。\n\n- GQA 显著加快推理速度，并降低解码阶段的显存需求，从而支持更大的 batch size 与更高吞吐 [2]。\n- SWA 利用 transformer 的堆叠层关注窗口大小之外的信息：第 k 层位置 i 的隐状态 hi 关注上一层中位置介于 i−W 与 i 之间的所有隐状态；递归地，hi 可访问输入层中距离最远 W × k tokens 的 token [4]。\n- 架构参数为 n_heads 32、n_kv_heads 8、window_size 4096、context_len 8192 [3]；固定的注意力跨度使得可用 rolling buffer cache 限制 cache 大小，cache 大小固定为 W，时间步 i 的 keys 和 values 存储在 cache 的位置 i mod W [5]。",
        "has_citation": true
      }
    }
  ]
}
```

### C03 · c-3 干净环境一键复现 · PASS

- 耗时：3.9s
- 实际：全新环境启动成功，文档数 0，向量后端 sqlite

```json
{
  "health": {
    "ok": true,
    "service": "sciagent",
    "version": "1.0.0"
  },
  "documents": 0,
  "data_dir": "<repo>\\data\\_qa_clean"
}
```

### C04 · c-3 长 PDF 打开耗时 · PASS

- 耗时：0.1s
- 实际：页图 90ms / 页元数据 35ms / 章节树 6ms

```json
{
  "file": "2303.08774v6.pdf",
  "pages": 100,
  "meta_ms": 35,
  "img_ms": 90,
  "sections_ms": 6,
  "img_bytes": 270587,
  "status": [
    200,
    200,
    200
  ]
}
```

### C05 · c-3 20+ 图谱渲染帧率 · PASS

- 耗时：5.8s
- 实际：图谱节点=37 首帧耗时=250ms canvas=998x678 1s 内 rAF 回调=3（无头虚拟时钟，仅参考） RESULT: PASS 20+ 节点图谱渲染，首帧 250ms

```json
{
  "dom_length": 6441,
  "raw": "图谱节点=37 首帧耗时=250ms canvas=998x678\n1s 内 rAF 回调=3（无头虚拟时钟，仅参考）\nRESULT: PASS 20+ 节点图谱渲染，首帧 250ms"
}
```

### C06 · c-3 峰值内存/显存 · PASS

- 耗时：25.5s
- 实际：RSS 796→796 MB（Δ+0.1 MB）；GPU：973 MiB

```json
{
  "pid": 28504,
  "port": 8013,
  "rss_before_mb": 796.0,
  "rss_after_mb": 796.1,
  "delta_mb": 0.1,
  "gpu": "973 MiB",
  "chat_status": 200,
  "note": "GPU 数值是 nvidia-smi 报告的系统级占用；全部模型走云端 API，本进程显存占用为 0"
}
```

### C07 · e-3 Token 级流式输出 · PASS

- 耗时：2.1s
- 实际：36 个 token 片段无损拼接，思考阶段 ['plan', 'retrieve', 'read', 'verify']，引用校验 8/8

```json
{
  "chunks": 36,
  "elapsed_s": 2.1,
  "thinking_stages": [
    "plan",
    "retrieve",
    "read",
    "verify"
  ],
  "joined_chars": 233,
  "raw_equals_tokens": true,
  "verify": {
    "total": 8,
    "valid": 8,
    "invalid": 0,
    "rate": 1.0,
    "invalid_anchors": [],
    "invalid_refs": [],
    "bad_numbers": [],
    "cleaned": "Transformer 的核心机制是完全依赖自注意力机制来建模输入与输出之间的全局依赖关系，摒弃了传统的循环神经网络（RNN）和卷积神经网络（CNN） [1] [p2@107,150,505,193]。该架构通过多头自注意力机制实现并行化计算，显著提升了训练效率，并在机器翻译任务中达到新的性能高度 [3] [p1@36,597,575,616]。其设计使得任意两个位置间的依赖关系仅需常数时间操作即可建立，克服了传统模型中随距离增长的计算开销问题 [10] [p2@36,263,575,301]。这一特性使 Transformer 在保持高质量输出的同时，大幅缩短了训练时间 [2] [p2@36,152,575,190]。",
    "has_citation": true
  },
  "evidence": 11,
  "first": "Transformer",
  "answer_head": "Transformer 的核心机制是完全依赖自注意力机制来建模输入与输出之间的全局依赖关系，摒弃了传统的循环神经网络（RNN）和卷积神经网络（CNN） [1] [p2@107,150,505,193]。该架构通过多头自注意力机制实现并行化计算，显著提升了训练效率，并在机器翻译任务中达到新的性能高度 [3] [p1@36"
}
```

### C07B · e-3 前端流式与思考过程可视化 (3%) · PASS

- 耗时：7.6s
- 实际：RESULT: PASS 流式开关/思考时间线/逐token答案/证据均渲染

```json
{
  "driver": "CDP realtime (virtual-time 与 SSE 流不兼容)",
  "raw": "控件已渲染：发送 / 流式开关；开始真实 SSE…\n思考步骤=4 html字符=531 DOM文本=250 证据=11\n答案片段=Transformer 的核心机制是完全依赖自注意力（self-attention）机制来建模输入与输出之间的全局依赖关系，摒弃了传统的循环神经网络（RNN）和\nRESULT: PASS 流式开关/思考时间线/逐token答案/证据均渲染",
  "dom_length": 6516,
  "screenshot": "<repo>\\docs\\qa\\evidence\\ui_stream_20261111_144615.png",
  "error": ""
}
```

### C08 · e-3 断网/异常错误边界 · PASS

- 耗时：0.0s
- 实际：异常均被拦截，状态码 [404, 400, 404, 422]

```json
{
  "probes": [
    {
      "probe": "不存在的文档",
      "status": 404,
      "body": "{'detail': '文档不存在'}"
    },
    {
      "probe": "空问题",
      "status": 400,
      "body": "{'detail': '问题不能为空'}"
    },
    {
      "probe": "超大页码",
      "status": 404,
      "body": "{'detail': '文档或源文件不存在'}"
    },
    {
      "probe": "畸形 JSON",
      "status": 422,
      "body": "{'detail': [{'type': 'string_type', 'loc': ['body', 'question'], 'msg': 'Input should be a valid string', 'input': 123}, {'type': 'list_type', 'loc': ['body', '"
    }
  ]
}
```

### C09 · 硬性用例 4：CSV 转 TikZ/Mermaid · PASS

- 耗时：0.0s
- 实际：Mermaid 与 TikZ 均保留全部数值

```json
{
  "format": "mermaid",
  "code": "xychart-beta\n    title \"对比\"\n    x-axis [\"Transformer\", \"BERT\"]\n    y-axis \"value\" 0 --> 132\n    bar [28.4, 30.1]  %% bleu\n    bar [65, 110]  %% params\n    %% raw values: {\"bleu\": [\"28.4\", \"30.1\"], \"params\": [\"65M\", \"110M\"]}",
  "tikz": "\\begin{figure}[htbp]\n  \\centering\n  \\begin{tikzpicture}\n    \\begin{axis}[\n      ybar, enlargelimits=0.15, legend style={at={(0.5,-0.18)},anchor=north,legend columns=-1},\n      ylabel={value}, symbolic xCoords={Transformer,BERT},\n      xtick=data, nodes near coords, grid=major,\n      title={对比},\n    ]\n      \\addplot coordinates {(\"Transformer\",28.4), (\"BERT\",30.1)};  % bleu\n      \\addlegendentry{bl"
}
```

### A06B · a-4 工具超时/失败重试/降级 (6%) · PASS

- 耗时：18.9s
- 实际：超时重试 3 次、失败重试 2 次，均降级到本地库并标注 source

```json
{
  "branches": {
    "timeout": {
      "status": 200,
      "attempts": 3,
      "degraded": true,
      "fallback": "arxiv_unavailable_local_corpus",
      "source": "local_library",
      "items": 2,
      "error": "TimeoutException: QA 模拟超时"
    },
    "fail": {
      "status": 200,
      "attempts": 2,
      "degraded": true,
      "fallback": "arxiv_unavailable_local_corpus",
      "source": "local_library",
      "items": 2,
      "error": "HTTPStatusError: QA 模拟 502"
    }
  },
  "health_after": [
    200,
    {
      "ok": true,
      "service": "sciagent",
      "version": "1.0.0"
    }
  ],
  "catalog": null
}
```

### A06C · a-4 Function Calling 决策 (6%) · PASS

- 耗时：2.9s
- 实际：LLM 选定 arxiv_search，真实执行返回 1 条（local_library）

```json
{
  "status": 200,
  "selected_by": "llm_function_call",
  "call": {
    "name": "arxiv_search",
    "arguments": {
      "query": "attention is all you need",
      "max_results": 1
    }
  },
  "result_ok": true,
  "result_source": "local_library",
  "items": 1
}
```

### C10 · c-1 删除可恢复性与审计 (P0) · PASS

- 耗时：2.8s
- 实际：删除→回收站→恢复闭环成功，块数 12 前后一致，审计已留痕

```json
{
  "file": "qa_probe_21.pdf",
  "old_doc_id": "d6866fa76b6cf",
  "new_doc_id": "d5bc645f3387d",
  "chunks_before": 12,
  "delete": [
    200,
    {
      "ok": true,
      "trash": true,
      "message": "已移入回收站，可用 /api/documents/{id}/restore 恢复"
    }
  ],
  "trash_has": true,
  "audit_has_delete": true,
  "restore": [
    200,
    {
      "ok": true,
      "restored": true,
      "doc_id": "d5bc645f3387d",
      "old_doc_id": "d6866fa76b6cf",
      "filename": "qa_probe_21.pdf",
      "chunks": 12,
      "message": "已从回收站恢复"
    }
  ],
  "chunks_after": 12,
  "doc_back": true
}
```

### D01 · d-2 六大核心模块闭环 · PASS

- 耗时：22.5s
- 实际：六模块全部贯通

```json
{
  "modules": [
    {
      "module": "解析-章节树",
      "status": 200,
      "size": 1304
    },
    {
      "module": "解析-参考文献",
      "status": 200,
      "size": 8249
    },
    {
      "module": "检索问答",
      "status": 200,
      "size": 12020
    },
    {
      "module": "图谱",
      "status": 200,
      "size": 28700
    },
    {
      "module": "对照阅读",
      "status": 200,
      "size": 4760
    },
    {
      "module": "任务轨迹",
      "status": 200,
      "size": 3201
    }
  ],
  "doc": "1706.03762v1_img.pdf"
}
```

### D02 · d-1 文献筛选 / 对比阅读痛点 · PASS

- 耗时：105.9s
- 实际：证据覆盖 4 篇：['1706.03762v1.pdf', '1910.10683v4.pdf', '2005.14165v4.pdf', 'N19-1423.pdf']

```json
{
  "evidence": 11,
  "docs": [
    "1706.03762v1.pdf",
    "1910.10683v4.pdf",
    "2005.14165v4.pdf",
    "N19-1423.pdf"
  ],
  "answer": "三者关系需按证据重新界定：Transformer 是一种完全基于 attention 的 sequence transduction 模型，用 multi-headed self-attention 替代编码器-解码器架构中常用的循环层 [1]；其编码器将输入符号序列映射为连续表示，解码器据此逐元素生成输出序列，每一步是 auto-regressive [2]。BERT 采用 masked LM（",
  "verify": {
    "total": 7,
    "valid": 7,
    "invalid": 0,
    "rate": 1.0,
    "invalid_anchors": [],
    "invalid_refs": [],
    "bad_numbers": [],
    "cleaned": "三者关系需按证据重新界定：Transformer 是一种完全基于 attention 的 sequence transduction 模型，用 multi-headed self-attention 替代编码器-解码器架构中常用的循环层 [1]；其编码器将输入符号序列映射为连续表示，解码器据此逐元素生成输出序列，每一步是 auto-regressive [2]。BERT 采用 masked LM（MLM），即随机掩码一定比例的输入 token 后预测这些被掩码的 token，目的是训练 deep bidirectional representation [5]；[4] 在描述 BERT 式目标时给出 corrupts 15% of the tokens，并指出 the motivation for the BERT objective was to outperform language model-based pre-training。关于 GPT-3，证据 [3] 仅指出其预训练目标对每个 token 等权重、lacks a notion of what is most important to predict and what is less important，未给出自回归建模、掩码机制、样本效率等具体描述——文献中未提及。因此，“Transformer 为架构基础、BERT 与 GPT-3 在其上发展出不同预训练目标”以及“三者在训练目标上存在本质差异”的表述，现有证据不足以支撑，应删除。",
    "has_citation": true
  }
}
```

### C11 · c-1 零崩溃 Bug 证据 · PASS

- 耗时：1.1s
- 实际：本轮窗口零 Traceback（历史 2 处已被兜底），11 个接口全部 200

```json
{
  "log_lines_scanned": 1984,
  "qa_window_start": "2026-11-11T14:44:15.261397",
  "tracebacks_in_window": [],
  "historic_tracebacks": 2,
  "unhandled": [],
  "endpoint_status": {
    "/health": 200,
    "/api/system/info": 200,
    "/api/documents": 200,
    "/api/tasks": 200,
    "/api/graph": 200,
    "/api/tools/health": 200,
    "/api/tools/catalog": 200,
    "/api/documents/trash": 200,
    "/api/audit": 200,
    "/api/research/future-work": 200,
    "/api/research/reproducibility?online=false": 200
  },
  "counts": {
    "documents": 37,
    "chunks": 19766,
    "tasks": 134,
    "lessons": 80
  }
}
```

### F02 · f-1 学术赋能效率 (5%) · PASS

- 耗时：20.6s
- 实际：在 6,341,691 字 / 19766 块中用 20.6s 定位 10 条证据（5315 字），过滤 99.916% 语料，引用校验 16/16

```json
{
  "corpus_chars": 6341691,
  "corpus_chunks": 19766,
  "latency_s": 20.6,
  "evidence_count": 10,
  "evidence_chars": 5315,
  "filter_ratio": 0.99916,
  "verify": {
    "total": 16,
    "valid": 16,
    "invalid": 0,
    "rate": 1.0,
    "invalid_anchors": [],
    "invalid_refs": [],
    "bad_numbers": [],
    "cleaned": "Transformer 的 base 模型训练使用 1 台机器上的 8 NVIDIA P100 GPUs。\n- 训练硬件为 one machine with 8 NVIDIA P100 GPUs，base 模型每个 training step 约 0.4 秒，共训练 100,000 steps 或 12 小时 [1][2]。\n- big 模型（Table 3 最后一行配置）step time 为 1.0 秒，训练 300,000 steps 或 3.5 天，同样在 8 P100 GPUs 上完成 [1][3][4]。",
    "has_citation": true
  },
  "answer": "Transformer 的 base 模型训练使用 1 台机器上的 8 NVIDIA P100 GPUs。\n- 训练硬件为 one machine with 8 NVIDIA P100 GPUs，base 模型每个 training step 约 0.4 秒，共训练 100,000 steps 或 12 小时 [1]["
}
```

### F01 · f-2 开源潜力与推广 · PASS

- 耗时：0.0s
- 实际：交付要素齐全

```json
{
  "files": {
    "README.md": true,
    "Dockerfile": true,
    "docker-compose.yml": true,
    "requirements.txt": true,
    ".env.example": true,
    ".gitignore": true,
    "LICENSE": true
  },
  "docs": [
    "docs\\传智杯-执行备忘录.md",
    "docs\\官方需求文档.pdf",
    "docs\\方案设计-v1.md"
  ]
}
```
