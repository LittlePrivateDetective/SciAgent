# SciAgent · 科研智能体

> 第九届传智杯「AI 大模型创新应用挑战赛」参赛作品（B 组 · 个人赛）
> 作者：章璞（广东海洋大学 · 自动化 · 2027 届）

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
| 引用图谱 | ✅ | 三级引用匹配（arXiv/DOI/标题）+ PageRank（幂迭代自实现）+ 介数中心性 + 社区发现 + ECharts 前端 |
| 领域综述 | ✅ | 基于图谱影响力排序，自动生成带 `[n]` 引用的四段式迷你综述 |
| 阅读器 | ⏳ 基础版 | 页图 + bbox 高亮 + 章节树；PDF.js 画布渲染待接 |
| 翻译/写作/推荐 | ⏳ 未开始 | P1/P2 排期见 `docs/方案设计-v1.md` |

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
└── api/       routes 全部 HTTP 接口
web/           Vue3 + Element Plus 单页（CDN，无构建链）
data/          pdf 测试集 / uploads 上传 / pages 页图 / chroma 向量 / sciagent.db
docs/          方案设计、官方需求文档、执行备忘录
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/documents/upload` | 上传 PDF（命中重复会返回三选一决策 + 版本差异报告） |
| POST | `/api/documents/resolve` | keep_new / keep_old / both |
| GET | `/api/documents/{id}/diff` | 与库内旧版本的章节级差异 |
| GET | `/api/documents/{id}/page/{n}.png` | 渲染单页，供阅读器与高亮 |
| GET | `/api/documents/{id}/sections` | 章节树 |
| POST | `/api/chat` | 多 Agent 问答（返回答案+证据+校验+trace） |
| GET | `/api/graph` | 引用图谱 + PageRank/介数/社区（`?refresh=true` 跳缓存） |
| POST | `/api/graph/survey` | 基于图谱影响力排序生成带引用的领域综述 |
| POST | `/api/library/rescan` | 批量导入官方测试集 |
| GET | `/api/system/info` | 模型路由/向量后端/OCR 能力自检 |

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
- 541 页书籍首次解析约 40–60s，走后台任务不阻塞 UI；
- 前端 PDF.js 画布渲染与中英对照阅读在 P1 排期。

## 许可证

代码 Apache-2.0；`data/pdf/` 下论文版权归原作者，仅用于赛事测试。
