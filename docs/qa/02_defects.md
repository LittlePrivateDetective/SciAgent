# SciAgent QA 缺陷与修复记录

> 分级：P0=数据丢失/核心承诺失效/崩溃级；P1=功能缺失或可观测错误；P2=测试台或交互细节。
> 「回归结果」列引用 `03_regression.md` 中的用例编号，具体 PASS/FAIL 以最后一次
> `docs/qa/evidence/latest.json` 为准，不在本文件预填结论。

## 第一轮实测（2026-11-11 11:37–11:46，29 条：PASS 15 / FAIL 8 / BLOCKED 6）

| 编号 | 分级 | 问题（实测证据） | 根因 | 改法 | 回归结果 |
| --- | --- | --- | --- | --- | --- |
| D-01 | **P0** | 库内文献从 37 篇 / 19,766 块掉到 30 篇 / 12,656 块，7 篇、7,110 块丢失。日志 `data/logs/sciagent.log` 11:20:59–11:21:06 连续 7 条「已删除文档」 | `DELETE /api/documents/{id}` 直接物理删 chunks/sections/citations/document，**前端删除按钮无二次确认、无审计、无回收站**；一次误触不可逆 | 新增 `TrashEntry` / `AuditLog` 两表；`delete_document` 先写可恢复快照与审计；新增 `/api/documents/trash`、`/{id}/restore`、`/api/audit`；前端改 `ElMessageBox.confirm` 二次确认 + 回收站弹窗与恢复按钮；源文件始终保留在 `data/pdf` | C10；数据恢复见 `evidence/rescan_result.json`（39 篇扫描，6 篇重入库，0 失败）与 `restore_scanned.log`（两份都留流程），最终 37 篇 / 19,766 块 |
| D-02 | **P0** | A04 随机 10 条引用只有 **4/10** 能回原文核对（bbox 内 12-gram 覆盖率 ≥0.5）；典型证据 `text_len=999` 而 `bbox_chars=400` | `_stitch()` 把同页前后邻块文本**拼进 `h.text`**，但 `h.bbox` 仍是主块矩形——证据文本与锚点不再一一对应，点锚点看到的只是证据的一部分，直接违反「细粒度精准出处」 | `_stitch` 重写为**邻块独立 Hit**：同页紧邻后一块作为独立证据条目，带自己的 `chunk_id/bbox/anchor/neighbor_of`，主块 `text` 不再被改写；命中数 8→9，`to_dict` 增加 `neighbor_of` | A03（锚点全部独立）、A04（溯源命中率）；C07（流式结果同样带完整 evidence） |
| D-03 | **P0** | A01 复杂对比问题耗时 85.1s，最终答案是「原回答只是系统错误提示…无法在现有证据基础上修订」这类**元话语**，不是问题答案 | ① `deepseek-v4.1-flash` 是推理模型，JSON 包裹 + 长对比表格把 4096 输出预算吃光，`writer` 连续两次返回空串；② 兜底文案进入 `node_review`，审稿 Agent 把它当正文改写 | ① 新增第三次兜底：换非推理模型 `qwen-flash` 写**纯文本**答案（不套 JSON），预算 3000；② `node_review` 在上游 `error` / 文案过短 / 命中「模型未返回、系统错误」等元话语时**直接短路**，不再加工降级文案；③ `node_answer` 增加 `writer.route` 步骤记录 `mode/template/evidence_docs`，让路由可观测 | A01、A02 |
| D-04 | P1 | A06 / C07 / C09 / B02 / B05 全部 404；官方要求的 Function Calling、Token 流式、CSV 转 TikZ/Mermaid、Future Work 提炼、GitHub 复现性均无实现 | 上一阶段只完成解析/RAG/图谱/对照阅读，未按评分细则补齐工具层与科研增强 | 新增 `app/tools/external.py`（arXiv / GitHub / Crossref，OpenAI tools schema，`decide()` 用 LLM 真实选工具，重试→外部 API→本地库降级且带 `degraded` 标记）、`app/tools/chart.py`（无损数值搬运到 Mermaid/TikZ）、`app/research/future_work.py`（触发词候选句→4-gram 去重→关键词对撞）、`app/research/reproducibility.py`（GitHub REST 真实数据加权分级）、`/api/chat/stream` SSE + 前端思考时间线与逐 token 渲染 | A06 / A06B / A06C / B02 / B05 / C09 / C07 / C07B |
| D-05 | P1 | C08 中「不存在的文档」返回 **200 + 空列表** | `get_chunks` / `get_sections` / `get_references` 未校验 doc_id 是否存在 | 三个接口先 `db.get(Document, doc_id)`，不存在即 404；测试台同步收紧断言 | C08 |
| D-06 | P1 | F01 缺 `LICENSE`；README 写 Apache-2.0 与代码不一致 | 交付要素未闭环 | 新增 MIT `LICENSE`，README 许可证段同步为 MIT，并补齐新接口/QA/已知限制说明 | F01 |
| D-07 | P1 | E03 静态检查发现 `requirements.txt` 缺 `chromadb`：容器会静默退化成 SQLite+NumPy，与实测 Chroma 环境不一致 | 只在本机 venv 装过 chromadb，没回写依赖清单 | requirements 补 `chromadb>=0.5.0`；`.env.example` 补 `TRANS_*` 四项；Dockerfile 保持通过 compose 注入 `.env` | E03（docker build 本机仍【待实测】）、C03（干净环境等价复现） |
| D-08 | P2 | A01/A02/A03/B01 四条用例自身报错或断言不合理（`plan` vs 实际 `planner`；`retrieve` 函数不存在；`build_graph()` 缺 db 参数；要求两种模式 agent 序列必须不同） | 测试台按想象写断言，没先核对真实接口契约 | 全部改为核对真实契约：A03 改 `search_with_rerank` 并检查 `dense_score/sparse_score/anchor` 独立性；B01 用测试台内**纯 NumPy 独立幂迭代**复算 PageRank 与入度；A01 增加元话语检测；A02 改为核对 `writer.route.template` 与答案形态 | 对应编号用例 |
| D-09 | P2 | A05 报「无扫描件；长文档解析异常」 | 由 D-01 的数据丢失连带造成，不是解析代码 bug | 恢复 7 篇文献；扫描件因与 v1 同族被 rescan 判重，改走产品自带「上传→判重→两份都留」真实流程入库（`tests/restore_scanned.py`，193 秒，OCR 后 `is_scanned=true`） | A05、C04 |
| D-10 | P2 | C01「并发导入」实际只传 2 份、全部被判重，0.8 秒结束，没走解析/向量化 | 用例用 `data/uploads` 里的重复副本，且只检查 HTTP 200 | 改为用 PyMuPDF 从 3 篇不同论文各取前 2 页生成**互不相同**的探针 PDF，真正并发入库，再逐篇抽查首页块是否串档，测完移入回收站 | C01 |
| D-11 | P2 | B04「库内无多版本文献」无法测 | 用例假设库里存在 `version>1`，实际同族版本被判重后未入库 | 改为上传 `1706.03762v7.pdf` 触发**真实 v1↔v7 判重**，核对返回的章节/句子级差异报告，再调 `/diff` 接口 | B04 |

## 第二轮实测中新发现

| 编号 | 分级 | 问题（实测证据） | 根因 | 改法 | 回归结果 |
| --- | --- | --- | --- | --- | --- |
| D-12 | P2 | `round2_targeted.log`：A06 报「外部工具调用失败 HTTP 200」，返回体 `error=参数不匹配` | ① 用例按旧约定把 `query` 平铺在 body，而 `ToolIn` 要求 `args` 字典；② 路由 `{"ok": True, **ToolResult}` 中 ToolResult 自带的 `ok` **覆盖了** HTTP 层 ok，产生歧义结构 | `tools_invoke` 直接返回 `ToolResult`（自带 ok/degraded/source/attempts）；A06 改用本机可达的 GitHub API 做成功路径、arXiv 做诚实降级路径 | A06 / A06B / A06C |
| D-13 | P2 | B03 三层去重只命中同族与标题，SimHash 层未命中 | 用例把 `v9` 直接拼在 `v1` 后面（`1706.03762v1v9`），且用**前 60 个 chunk** 当 full_text，与入库时 `parsed.body_text()` 的指纹不同 | 用例改为 `parse_pdf` 重新解析得到真实 `body_text()`，验证 `content_hash/simhash` 与库内一致，再用「全文 + 一句附录」构造近似文本；同族用正则替换版本号 | B03 |
| D-14 | **P1** | C07B「未找到发送按钮」；`dump-dom` 显示 `.ask-bar` 里**只有 el-input，没有发送按钮**——界面从上线起就缺按钮，用户只能按回车 | Vue 模板写在 HTML 里，**自定义元素不能自闭合**：`<el-input ... />` 被浏览器解析成开始标签，后面的 `<el-button>发送</el-button>` 被吞进 el-input 的默认插槽，而 el-input 不渲染默认插槽。同类问题还有 2 个 `el-option`（「全部文献」选项被吞）、2 个 `el-switch`（联动/整篇翻译按钮被吞）、`el-checkbox` | 6 处全部改成显式闭合标签；`dump-dom` 复核：`el-button--primary` ×2、`发送</span>` ×1、`全部文献` ×1、`流式`/`回收站` 均在 | C07B、界面截图 `evidence/ui_main.png` |
| D-15 | P2 | `restore_document` 重新解析后生成**新的 doc_id**，但返回体没带回，前端/QA 拿旧 id 查询会 404 | 恢复走 `ingest(force=True)`，主键重新生成 | 返回体增加 `doc_id` / `old_doc_id`；C10 按新 id 复核块数并清理探针 | C10 |
| D-16 | P2 | C07B 在虚拟时钟 45s 预算下只输出 `running…` | 流式请求是真实网络，无头浏览器的 virtual-time-budget 在等待网络时不足以跑完 ready 轮询 | stream 类型预算提到 120s、子进程超时 260s；QA 页在点击发送后立刻写一条进度日志，便于超时定位 | C07B |

## 第三轮实测中新发现（2026-11-11 14:00–14:30）

| 编号 | 分级 | 问题（实测证据） | 根因 | 改法 | 回归结果 |
| --- | --- | --- | --- | --- | --- |
| D-17 | **P0** | A04 溯源修到 9/10 后，剩下 1 条扫描件证据 `evidence=589 字` 而锚点原文 `508 字`，差 81 字 | `_stitch()` 追加**邻块独立证据**时直接取 `Chunk.text` 原始值，开头带着 80 字 overlap；主检索通道已统一剥离，邻块路径漏了 | `_stitch` 一次查出主块+邻块，以主块**原始**文本为 prev 调 `strip_carry(main_raw, text)` | A04 **10/10**（`run_20261111_142756.json`）、A03、C07 校验 6/6 |
| D-18 | **P1** | 第二轮 A04 只有 **1/10**：普通 PDF 证据文本普遍比 bbox 内原文多 80 字 | `Chunk.text` 是带 overlap 的分块文本，但 `bbox` 只覆盖本块。首轮只修了 `_stitch` 拼接，没修语料加载本身 | 新增公共 `app/rag/chunker.strip_carry()`，在 `_load_corpus` 按文档顺序逐行剥离；`bilingual.py` 删重复实现改为复用 | A04 1/10→9/10→10/10；B06 对照阅读无重影 |
| D-19 | **P1** | B03 SimHash 层不命中：全文加两句附录，指纹翻转 **38 位**（阈值 6） | `simhash64()` 拿 `normalize(text)` 分词，而它把空格全删，36k 字变成 1 个巨型 token，整张指纹由一个 md5 决定 | 保留词边界，只在数字/标点层归一化，并加词频加权 | 加 1–2 句距离 0–1、删 10% 距离 3、完全不同 32；B03 PASS |
| D-20 | **P1** | A05 扫描件 `refs=0`，但 OCR 文本 p10–p12 含完整 `[1]–[36]` References 区 | `finalize_ocr_structure` 只认裸标题行，OCR 把章节号与 References 粘连（`10 References`）且条目是数字编号 | 放宽 OCR 参考区识别并按行序还原；写 `scripts/repair_corpus.py` 修补**已入库数据** | 扫描件补建 **35 条**参考文献；全库 simhash 37 篇重算；A05 PASS |
| D-21 | **P1** | 容器静态校验：`langgraph`（本机 1.2.20、编排核心）**未声明**；另有 5 个包被声明但代码从不 import | 依赖清单是早期规划遗留。容器里不装 langgraph 会让 `build_langgraph()` 静默降级成手写状态机 | 重写 `requirements.txt`：补 `langgraph>=0.2`/`PyYAML`，删 5 个冗余包并逐条注明理由；OCR 与本地向量档标为可选 | E03：15 个第三方 import 全部声明、零密钥布局启动 5 接口全 200 |
| D-22 | P2 | C07B 虚拟时钟 120s 下只输出「开始真实 SSE…」一行，连 catch 都不进 | 虚拟时钟快进 `setTimeout`，但 SSE 的 `reader.read()` 是真实网络 I/O，Promise 永不 resolve。后端 SSE 实测 116 行/2.5s 正常 | 新增 `tests/browser_cdp.py`：websocket-client 连 CDP 在**真实时间**驱动、轮询 `#out` 并截图；加 `--remote-allow-origins=*` 解决握手 403 | C07B PASS（思考步骤 4、DOM 315 字、证据 11 条），截图 `evidence/ui_stream_20261111_140650.png` |
| D-23 | P2 | A04 用例自身抛 `NameError: name 'Chunk' is not defined` | 改写判据时新增 `db.get(Chunk, ...)` 与 `select(...)` 却漏加导入 | 补 `from sqlalchemy import select` / `from app.db.models import Chunk, Document` | A04 正常执行 |
| D-24 | P2 | E03 预演触发批量删除保护（8991 文件）；随后 `UnboundLocalError: os` | 预演用 `shutil.rmtree` 清旧目录；函数内残留 `import os` 让整个函数的 `os` 变成未绑定局部变量 | 改时间戳独立目录不删旧（预演产物本身就是证据）；删除重复局部导入 | E03 PASS，48–60 秒 |
| D-25 | P2 | A06 断言字段错位、C11 把昨天的历史 Traceback 算作本轮崩溃、C10/C01 探针被判重形成"假 PASS"、A05 判据过严 | 测试台按想象写断言 / 扫了全量日志 / 用重复副本做并发探针 | A06 改按 `source/degraded` 判断且 GitHub 作成功路径；C11 只扫本轮时间窗；C01/C10 用 PyMuPDF 生成不同论文前 2 页探针并走「两份都留」强制入库 | A06/C11/C01/C10/A05 全部 PASS |
| D-26 | P1 | A02 两种模式的 `writer.route.template` 都是 `compare`，路由未分化 | 用例用含"对比"的问题同时测 qa/compare，而产品按**意图优先**设计，属用例构造与产品意图冲突 | 产品侧把决策依据（`default`/`explicit_mode`/`intent_hint`/`evidence_docs`）写进 trace；用例改用不含对比词的 qa 问题 | A02 PASS：`qa→qa(default)` / `compare→compare(explicit_mode)` |
| D-27 | **P1** | 交付轮 C06 报 `RSS 7→7 MB`，而同样环境前两轮是 820 MB / 1031 MB——数字明显失真却判了 PASS | 用例按 `cmdline` 关键字遍历取**第一个**匹配的 uvicorn 进程，抓到了 C03/E03 刚拉起、还没加载语料的子进程；且没有合理性断言，任何读数都算通过 | 改为按**监听 8013 端口**的 PID 定位主服务，兜底时取 RSS 最大者；并加 `before < 150 MB → FAIL` 断言（主服务加载 Chroma + 19,766 块，不可能只有几十 MB） | C06 复测 **PASS：RSS 629→630 MB（Δ+0.6 MB）**、GPU 1020 MiB；`latest.json` 只允许全量运行写入，避免定向回归覆盖 |
| D-28 | P2 | C07 流式答案的 `bad_numbers=['12']`（引用校验 12/13），但核对论文 p2 原文确有 `twelve hours`、p7 写作 `12 hours` | 数字溯源按**字面**比对阿拉伯数字，英文数词 `twelve` 不在证据池里；答案引 p2 写「12 小时」语义正确却被误标未核实。属安全侧误报，会降低评委对校验器可信度的判断 | 新增 `_WORD_NUM` 英文数词表，只把证据文本里的数词**加进证据池**，不放宽答案侧 | 单元级正反验证：正例 `12` 放行（rate 1.0）、编造 `99/77` 仍被拦（bad=['99','77']）；交付轮 C07 回归 |
| D-29 | **P1** | `Dockerfile` 的 `COPY docs ./docs` 会把 `docs/qa/evidence/` 下 582 MB 的浏览器 profile 与截图缓存打进镜像；容器预演递归复制后 `data/_qa_container_*` 膨胀到单个 584 MB | 缺 `.dockerignore`；预演脚本复制 `docs` 时也没排除证据目录 | 新增 `.dockerignore`（排除 `data/`、`tests/`、`docs/qa/evidence/`、`.env` 等）；预演复制与忽略规则保持一致；`.gitignore` 补浏览器 profile 与 `_qa_*` 临时目录 | 交付轮 E03（预演 91s→提速）、F01；证据本体仅 1.3 MB 入库 |

## 性能与优化前后对比（均为实测）

| 指标 | 优化前 | 优化后 | 依据 |
| --- | --- | --- | --- |
| 引用溯源准确率 | 4/10（首轮）→ 1/10（第二轮判据收紧后暴露 overlap） | **10/10** | `round1_console.log` / `round2_console.log` / `run_20261111_142756.json` |
| 单次问答（语料缓存前） | 36s | 3s | 上一阶段 `CorpusRow` 轻量缓存，见 `memory/2026-11-11.md` |
| 一页中英翻译 | 20s（qwen-plus 串行） | 3–6s（qwen-flash + 3 路并发） | 上一阶段实测日志 |
| 打开一页自动译完整篇 | 90 段 / 45 秒（无限链预取） | 只译当前页 5 段 | 上一阶段 `bi3.png` + `translations` 表计数 |
| 复杂对比任务 | 85.1s，答案无效（元话语） | 100.8s，答案 3477 字且编排节点齐全 | 第一轮 A01 / 第三轮 A01（耗时仍偏高，已列扣分项） |
| SimHash 近似文本（加 2 句） | 汉明距离 38（漏判） | 0–1（命中，阈值 6） | D-19 实测输出 |
| 长 PDF 页图 | — | 81ms（首屏）/ 页元数据 39ms / 章节树 6ms | C04 |
| 图谱首帧（37 节点） | — | 265ms，canvas 998×678 | C05 |
| 服务 RSS | — | 1031→1030 MB（Δ-0.3 MB）；GPU 915 MiB 为系统占用 | C06 |
| 流式输出 | 无 | 39–53 个 token 片段无损拼接，思考阶段 4 个，前端逐 token 渲染 | C07 / C07B |
| 检索效率 | — | 6,341,691 字 / 19,766 块中 17.5s 定位 11 条证据，过滤 99.909% 语料 | F02 |
