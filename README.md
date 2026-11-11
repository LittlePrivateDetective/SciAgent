# SciAgent · 科研智能体

[![CI](https://github.com/LittlePrivateDetective/SciAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/LittlePrivateDetective/SciAgent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/LittlePrivateDetective/SciAgent)](https://github.com/LittlePrivateDetective/SciAgent/releases)

> 第九届传智杯「AI 大模型创新应用挑战赛」参赛作品（个人赛 · B 组）
> 作者：章璞　参赛编号：**2133148**　广东海洋大学 · 自动化 · 本科（2023 级）
>
> 技术文档：[docs/技术文档.pdf](docs/技术文档.pdf)（26 页）

一个**能自我校验引用**的科研助手：上传论文 → 版面感知解析 → 锚点化检索 → 多 Agent 编排作答 →
每个结论都能点回原文那一块高亮。

## 一句话讲清它和"套壳 RAG"的区别

普通 RAG 让模型写"根据文献 [1]"，你无从核对；
SciAgent 让模型**只能引用检索到的 chunk**，服务端把 chunk 换成 `[p3@120,480,468,512]` 这种物理坐标，
再用校验器回查这个坐标是否真的存在于解析结果里——**编不出坐标的引用会被判为幻觉并重新作答**。

这是本项目的中心契约，也是答辩时最该讲的一点。

## 核心能力（当前完成度）

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 版面感知解析 | ✅ | 双栏/单栏阅读顺序还原、字号层级建章节树、图/表/公式/参考文献分块 |
| 细粒度结构 | ✅ | 标题、作者、单位、摘要、一~三级标题、参考文献条目、arXiv/DOI、年份 |
| 内容级去重 | ✅ | arXiv 同族 + SimHash 指纹 + 标题相似三层判定，附**章节级版本差异报告** |
| 扫描件兜底 | ✅ | 无文本层自动走视觉模型 OCR（`qwen3.5-ocr`），Markdown 标题反推层级 |
| 混合检索 | ✅ | BM25 + 稠密向量 + RRF 融合 + cross-encoder 重排（API 优先，本地规则兜底） |
| 锚点校验 | ✅ | 引用编号 / 坐标 / 数字溯源三重校验，越界比例 >20% 触发重规划 |
| 多 Agent 编排 | ✅ | LangGraph 七节点 + 条件边动态重规划 + Reflector 写长期记忆 |
| 快速 / 深思 | ✅ | 对话框一键切换：快速=非推理模型直答（首字 ~1.3s），深思=主模型先推理（多 ~2.6s）；回答上方标出模式与耗时；单条请求覆盖，不改全局 |
| 引用图谱 | ✅ | 三级引用匹配（arXiv/DOI/标题）+ PageRank（幂迭代自实现）+ 介数中心性 + 社区发现 + ECharts 前端 |
| 领域综述 | ✅ | 基于图谱影响力排序，自动生成带 `[n]` 引用的四段式迷你综述 |
| 阅读器 | ✅ | 页图 + **词级透明文本层**（文字可选、角标可点）+ 一~三级章节树 |
| 悬停翻译 | ✅ | 鼠标在正文上停 0.4 秒自动译**所在整句**（前端按词序断句，节标题/缩写不误切）；跟随光标的轻量浮层、永不拦截鼠标；同一句二次悬停走本地缓存秒出；开关状态跨会话记住（官方 1.3.3.1） |
| 证据句级高亮 | ✅ | 点证据不涂整段：服务端在 chunk 内挑出**真正作答的句子**（问题/答案实词 + 数字加权），前端用词级坐标逐行绘制，句首从行中间起、到句末为止；扫描件无文本层时如实降级为虚线近似框 |
| 文内引用跳转 | ✅ | **双向**：点正文 `[11]` 直达 References 第 11 条并高亮，浮层同步显示条目全文；条目浮层里列出「正文引用 N 处」，点一处跳回原文并按句高亮（官方 1.3.2 要求的「从 References 跳回去」）；`[1, 2]` / `[4-6]` 都能识别，数学区间 `[0, 1]` 不会误判为引用；答案里的 `p7 ▸` 按坐标回查证据所属文献，跨文献也能跳对页 |
| 划词翻译 | ✅ | 选中英文即出现「译」按钮，走独立非推理模型；扫描件无文本层时如实提示（官方 1.3.3.1） |
| 中英对照 | ✅ | 双栏双向联动滚动（按段落锚点对齐）、按页懒翻译 + 落库缓存、`#bi` 深链；**整篇中文译本导出 Markdown**（读缓存、不重调模型，标题/章节/References 齐全，公式与代码原样围栏保留、未译段落保留原文并注明，官方 1.3.3.2） |
| 中译英（中→英） | ✅ | 划词/片段**自动判方向**（中文→英文、英文→中文）；整篇中译英按 ~1200 字分批、3 路并发，产出 Markdown 可预览可下载，标题同步翻译（官方 1.6.4） |
| 论文发现与推荐 | ✅ | 标题/关键词搜 arXiv → 下载 PDF → 走**同一条入库流水线**；参考文献按**正文被引次数**排序补全；新论文推荐带中文理由 + GitHub 标记 + 赞/踩反馈（官方 1.4） |
| 后台参数中心 | ✅ | `/admin` 独立控制台，**口令登录**（`ADMIN_PASSWORD`，会话 12h、错 8 次锁 10 分钟）；**42 项参数**分 5 组，每项标生效方式（立即 / 仅之后入库 / 需重建索引 / 需重启）、来源（后台覆盖 / .env / 默认）与范围校验；密钥只回掩码；整批校验不留中间态；改动写审计；内置模型连通性自测与运行概览；**5 分钟后台保活**（刷新连接 + 让本地模型常驻显存，间隔/开关可调） |
| 本地模型（可选档） | ✅ | Ollama + qwen2.5:3b 真实部署并实测：**40 组题 × 3 模型**。首字延迟 **114ms**（云端 qwen-flash 431ms）、结构化 JSON **8/8** 与云端持平、0 元成本；边界也很清楚——6k token 输入首字要 3.6s、4 路并发加速仅 1.07×（云端 3.25×）。含 5 张对比图与逐题原始数据：[本地模型实测](docs/本地模型实测.md) |
| 外部工具链 | ✅ | Function Calling 选工具 → arXiv / GitHub / Crossref 真实 API；重试 → 降级 → 如实标注 |
| 学术图表 | ✅ | CSV 实验表格**无损**转 4 类图 × 5 种代码：柱状/折线/雷达/三线表 → Mermaid·TikZ(pgfplots)·Matplotlib·LaTeX·Markdown；原始数值逐格照搬（`41.0` 不会变成 `41`）；**自动生成 Figure Caption**（中英双版 + 核心趋势），模型写完把图注里每个数字回查原表，查不到的列出来（官方 1.6.3） |
| 论文架构拓扑图 | ✅ | idea（可带上已生成的大纲）→ 一张分层架构图，**同时输出 Mermaid / Graphviz DOT / TikZ / Matplotlib 四种代码**。模型只产出节点与边，代码由渲染层生成——四种格式必然描述同一张图，且不会出现模型写错的 `\node` 语法；预览用 ECharts 自绘分层图，不依赖外部渲染器（官方 1.6.2） |
| Future Work 对撞 | ✅ | 候选句抽取 → 4-gram 语义去重 → 与库内后续论文关键词对撞 |
| 代码复现性 | ✅ | 正文抽 GitHub 仓库 → star/许可证/最后更新/归档状态加权分级 |
| 流式输出 | ✅ | SSE token 级增量 + 思考过程时间线 + 完成事件带证据与校验 |
| 数据安全 | ✅ | 删除进回收站（可恢复）+ 审计日志 + 前端二次确认 |
| 写作 Copilot | ✅ | idea → Abstract / Introduction / Related Work / Research Plan；**References 由服务端按 GB/T 7714 从库内元数据生成，模型只能写 `[n]`**，编造条目结构上不可能（官方 1.6.1） |
| 多用户 / 协同过滤推荐 | ⏳ 未开始 | 单机单用户定位；`DEMO_ACCESS_TOKEN` 仅留字段 |

## 架构

```
Web(Vue3+Element) ──HTTP──► FastAPI
                              │
                     ┌────────┴────────┐
                     │ LangGraph 编排   │  plan → retrieve → answer → verify
                     │                  │      ↑___________replan______|
                     └────────┬────────┘           review → reflect
                              │
        ┌─────────┬───────────┼───────────┬──────────┐
   解析(PyMuPDF) 检索(BM25+向量) 锚点校验  图谱(NetworkX) 记忆(SQLite)
                              │
                    SQLite(元数据) + Chroma/SQLite(向量) + 本地文件
```

## 快速开始

```bash
# 1. 依赖
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
pip install -r requirements.txt

# 2. 配置（不填也能跑，走 mock 模式）
cp .env.example .env
# 必填：LLM_API_KEY / LLM_MODEL；想要好的检索效果再填 EMB_* 两项

# 3. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 <http://127.0.0.1:8000> 即界面，<http://127.0.0.1:8000/docs> 是自动生成的 API 文档。

首次进来点左侧**「一键导入官方测试集」**，会把 `data/pdf/` 下 39 篇论文（含 Attention v1/v7、
扫描件、541 页书籍）后台入库。

## 三种模型接入方式

| 模式 | 配置 | 用途 |
| --- | --- | --- |
| 云 API | `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` | 默认，接 DeepSeek/通义/智谱等任意 OpenAI 兼容服务 |
| 本地 Ollama | `OLLAMA_ENABLED=1` + `LLM_ROUTE=local` | 3B 量化模型，呼应"本地模型部署加分"，也可作断网兜底 |
| Mock | `LLM_MOCK=1` | 无密钥时整条链路照常跑通，用于评审环境自检 |

Embedding 同理：`EMB_PROVIDER=api|local|hash`。**强烈建议用 api**（bge-m3），
hash 兜底只能做词形匹配，语义召回会明显变差——这一点在文档里如实说明，不粉饰。

## 目录结构

```
app/
├── core/      config 配置 / logger 日志 / llm 模型网关（云+Ollama+mock 三路）
├── db/        models 八张表 / session SQLite+WAL
├── parse/     structure 版面解析 / ocr 扫描件兜底
├── rag/       chunker 切块 / embed 向量 / store 向量库 / hybrid 混合检索
│              anchors 锚点校验 / dedup 内容去重
├── agents/    state 状态 / prompts 提示词 / nodes 七节点 / graph 编排
├── pipeline/  ingest 入库管线（解析→去重→切块→向量化→落库）
├── tools/     external 外部 API 工具链 / chart CSV→五种图表代码+图注 / topology 架构图四格式
├── research/  future_work 提炼对撞 / reproducibility 复现性评估
└── api/       routes 全部 HTTP 接口
web/           Vue3 + Element Plus 单页（CDN，无构建链）
data/          pdf 测试集 / uploads 上传 / pages 页图 / chroma 向量 / sciagent.db
docs/          技术文档（技术文档.pdf）/ 方案设计 / 测评报告 / qa 全套测试交付物
tests/         qa_suite 回归台 / qa_report 报告 / browser_cdp 浏览器驱动 /
               container_rehearsal 容器布局预演 / restore_scanned 扫描件恢复
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/documents/upload` | 上传 PDF（命中重复会返回三选一决策 + 版本差异报告） |
| POST | `/api/documents/resolve` | keep_new / keep_old / both |
| GET | `/api/documents/{id}/diff` | 与库内旧版本的章节级差异 |
| GET | `/api/documents/{id}/page/{n}.png` | 渲染单页，供阅读器与高亮 |
| GET | `/api/documents/{id}/sections` | 章节树 |
| POST | `/api/chat` | 多 Agent 问答（返回答案+证据+校验+trace；`thinking` 可选 true/false 覆盖本次模式） |
| GET | `/api/graph` | 引用图谱 + PageRank/介数/社区（`?refresh=true` 跳缓存） |
| POST | `/api/graph/survey` | 基于图谱影响力排序生成带引用的领域综述 |
| POST | `/api/library/rescan` | 批量导入官方测试集 |
| GET | `/api/system/info` | 模型路由/向量后端/OCR 能力自检 |
| GET | `/api/documents/{id}/page/{n}/text` | 页内词级文本层（划词与角标点击的底座） |
| GET | `/api/documents/{id}/references` | References 逐条索引：编号 → 页码 + bbox |
| POST | `/api/translate` | 划词翻译（即译即返，不落库） |
| POST | `/api/documents/{id}/translate-en` | 启动整篇中译英（后台任务 + 3 路并发） |
| GET | `/api/documents/{id}/translate-en` | 译英进度与英文版预览 |
| GET | `/api/documents/{id}/translate-en.md` | 下载英文版 Markdown |
| GET | `/api/documents/{id}/translate-zh` | 中文译本预览（读 translations 缓存拼 Markdown） |
| GET | `/api/documents/{id}/translate-zh.md` | 下载中文译本 Markdown |
| GET | `/api/documents/{id}/references/{n}/cites` | 第 n 条参考文献被正文哪些地方引用（反向跳转） |
| GET | `/api/discover/search` | arXiv 检索候选论文，标注库里是否已有 |
| POST | `/api/discover/import` | 下载指定 arXiv 编号并走完整入库 |
| GET | `/api/discover/references` | 库内论文的参考文献候选（按正文被引次数排序） |
| POST | `/api/discover/references/import` | 批量补全（后台任务），配 `/api/discover/jobs/{id}` 轮询 |
| GET | `/api/discover/recommend` | 新论文推荐：TF-IDF 兴趣画像 + 中文理由 + GitHub 标记 |
| GET/POST | `/api/discover/feedback` | 赞 / 踩 / 点开反馈（影响下次推荐排序） |
| GET | `/api/admin/params` | 参数定义 + 当前值 + 来源（密钥只给掩码） |
| PUT | `/api/admin/params` | 保存参数（整批校验，返回需重启 / 需重建索引项） |
| POST | `/api/admin/params/reset` | 恢复到启动时的 .env 值（不碰密钥明文） |
| GET | `/api/admin/overview` | 语料规模、存储、模型与向量库状态、运行时、审计 |
| GET | `/api/keepalive` | 保活状态：最近心跳时间、耗时、累计次数、最近错误 |
| POST | `/api/keepalive/beat` | 手动补一次心跳（外部 cron / 计划任务调它，30 秒内幂等） |
| POST | `/api/admin/test-model` | 真实调用一次模型，回报耗时 / token / 空回复诊断 |
| POST | `/api/write/outline` | 写作 Copilot：idea → 四段框架 + 无幻觉 References |
| POST | `/api/write/topology` | idea（或大纲）→ 架构拓扑图，一次返回 Mermaid / DOT / TikZ / Matplotlib |
| GET/POST | `/api/documents/{id}/bilingual[/translate]` | 中英对照段落 / 按需翻译 |
| POST | `/api/chat/stream` | SSE 流式问答（thinking / token / done 事件；`thinking=true` 时推理片段不逐字推，只报"推理完成（N 字）"） |
| GET/POST | `/api/tools/*` | 工具健康检查、schema、显式调用、LLM Function Calling 决策 |
| POST | `/api/tools/csv2chart` | CSV → 柱状/折线/雷达/三线表 × Mermaid / TikZ / Matplotlib / LaTeX / Markdown + Figure Caption |
| GET | `/api/research/future-work` | Future Work 提炼去重与后续论文对撞 |
| GET | `/api/research/reproducibility` | 论文开源代码复现性评估 |
| GET | `/api/documents/trash` · `/api/audit` | 回收站与关键写操作审计 |
| POST | `/api/documents/{id}/restore` | 从回收站恢复（重新解析源文件） |

## 回归测试

```bash
./.venv/Scripts/python.exe tests/qa_suite.py --group all      # 全量 QA（证据落 docs/qa/evidence/）
./.venv/Scripts/python.exe tests/qa_report.py                 # 生成测试总表与回归报告
```

交付物见 `docs/qa/`：测试用例总表、缺陷与修复记录、最终回归报告、逐项自评、可复现交付说明。

后台与部署：`docs/后台控制台.md`（参数中心怎么用）、`docs/部署对比-本地vs云端.md`（两条路的成本/延迟/显存/合规取舍）。

## 批量脚本

```bash
python scripts/bulk_import.py        # 批量导入 data/pdf（断点续传，逐文件计时）
python scripts/rebuild_citations.py  # 解析器升级后刷新引用表（不重算向量）
```

## 设计取舍（为什么这么写）

1. **不用 LangChain 的 AgentExecutor**：黑盒，讲不清"失败了怎么重规划"。LangGraph 的图就是文档。
2. **向量库双后端**：赛题点名 Chroma，但它在部分 Python 版本装不上；SQLite 里也存一份向量，
   Chroma 缺失时自动降级，作品不会因为一个包而整体不可用。
3. **前端不上构建链**：8 天工期里 npm 是纯风险项，Vue3 + Element Plus 走 CDN 即可交付。
4. **OCR 用视觉大模型而非 PaddleOCR**：扫描件只有几十页，按页调用成本极低；
   paddle 的安装成功率是 8 天工期内不可接受的风险。
5. **锚点不放给模型生成**：模型只给 chunk 序号，坐标由服务端渲染。这是"可验证"的前提。

## 部署

```bash
docker compose up -d --build     # 单容器，数据挂 ./data
```

生产建议：Caddy 反代 + HTTPS；源站只放行 CDN 回源段；`DEMO_ACCESS_TOKEN` 给评审专用口令。

## 已知限制

- `EMB_PROVIDER=hash` 时语义检索退化，属于兜底档，不是交付档；
- 公式只抽 bbox 不做 LaTeX 重建（刻意选择：可验证 > 花哨）；
- 541 页书籍首次解析实测 **260 秒**（其中向量化 246 秒），走后台任务不阻塞 UI；
- 本机无 Docker，`docker build` 与真机图谱帧率标【待实测】，详见 `docs/qa/05_delivery.md`；
- arXiv API 与 PDF 下载本机实测可达（检索 ~1.7s、下载+入库 11s/篇）；接口抖动时工具层降级到本地语料并标注 `degraded=true`；
- 阅读器用页图 + 词级透明文本层，不引 PDF.js：排版 100% 还原、无 worker/CMap 依赖，
  但正文文字位置由 PyMuPDF 的词框决定，个别花体字/连字会略有偏移；
- 扫描件没有文本层，页面上不能划词、角标也不能点；单页提供「翻译本页」（按段落调翻译并复用浮层），整篇译文走「对照」视图。真正的词级文本层需要本地 OCR 引擎（tesseract），本机未安装；
- 中译英演示样本在 `docs/samples/cn_paper_sample.pdf`（自制 1 页中文论文），上传后点阅读器「译英」即可；
  中文扫描件要先过 OCR 才有可译文本；
- 新论文推荐是**按需刷新**（按钮 + 时间窗口），没有常驻定时任务：演示环境不一定有外网，
  后台定时抓取只会空转，反而让日志充满失败记录；
- 多用户与协同过滤推荐未做：当前是单机单用户定位。

## 许可证

代码 MIT（见 `LICENSE`）；`data/pdf/` 下论文版权归原作者，仅用于赛事测试。
