# SciAgent 可复现交付说明

> 本文件只写**实测确认过**的环境与步骤。任何本机无法验证的环节都明确标注【待实测】，
> 不把"应该能跑"写成"已验证"。

## 1. 实测运行环境

| 项 | 实测值 |
| --- | --- |
| OS | Windows 10 Build 19045（MINGW64_NT-10.0-19045 / Git Bash 3.6.9） |
| Python | 3.13.14（项目 `.venv`） |
| Web | FastAPI 0.162.2 / Uvicorn 0.54.0 / Pydantic 2.14.3 |
| DB | SQLAlchemy 2.1.8 + SQLite（WAL），`data/sciagent.db` |
| 向量库 | ChromaDB 1.5.9（`data/chroma`），可降级 SQLite+NumPy |
| 解析 | PyMuPDF 2.1 / jieba 0.42.1 |
| 图谱 | NetworkX 3.7 / NumPy 2.5.4（PageRank 为纯 NumPy 幂迭代，不依赖 scipy） |
| LLM | `deepseek-v4.1-flash`（DashScope 兼容端点） |
| 翻译/工具路由 | `qwen-flash`（非推理模型） |
| Embedding | `qwen3.7-text-embedding` 1024 维（DashScope） |
| Rerank | `qwen3.7-text-rerank`（DashScope） |
| OCR | `qwen3.5-ocr`（VLM）；本机 `pytesseract` 与 tesseract 二进制**均未安装**，只走 VLM |
| 浏览器验证 | Microsoft Edge（headless=new） |
| GPU 显存 | 本机 nvidia-smi 报告 915–1087 MiB（**系统级占用**）；全部模型走云端 API，进程显存占用为 0 |
| 服务常驻内存 RSS | 629→630 MB（一次问答 Δ+0.6 MB，C06 按监听端口定位实测；早期"7 MB"读数是抓错进程，已作废） |
| 语料规模 | 37 篇 / 19,766 块 / 6,341,691 字 / 含 541 页长书、100 页长文、15 页扫描件 |

`scipy`、`sklearn`、`PIL`、`pytesseract`、`pdfplumber`、`pandas`、`matplotlib`、`arxiv`、
`sentence-transformers` 在本机 **未安装**——这些都不在主链路上：BM25 为纯 Python 自实现、
页图由 PyMuPDF 直接输出 PNG、图谱 PageRank 用纯 NumPy 幂迭代、外部工具用 httpx 直连、
CSV 图表用标准库 `csv`。第三轮静态校验（`tests/container_rehearsal.py`）据此修正了
`requirements.txt`：删掉 5 个从不导入的冗余包，补上**漏声明的编排核心 `langgraph`**
（否则容器里 `build_langgraph()` 会静默降级成手写状态机）与 `PyYAML`；
`Pillow`/`pytesseract` 标为 OCR 可选兜底，`sentence-transformers` 显式不随镜像安装
（会拉 torch >1GB，仅 `EMB_PROVIDER=local` 档需要）。实测 15 个第三方 import 全部有声明。

## 2. 从零启动（本机实测通过）

```bash
cd <repo>
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # 填入 LLM_API_KEY / VLM_API_KEY / EMB_API_KEY / RERANK_API_KEY
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013
```

访问：

| 入口 | 用途 |
| --- | --- |
| `http://127.0.0.1:8013/` | 主界面（阅读 / 对照 / 图谱） |
| `http://127.0.0.1:8013/#bi` | 深链直达中英对照视图 |
| `http://127.0.0.1:8013/docs` | 自动生成的 API 文档 |
| `http://127.0.0.1:8013/health` | 健康检查 |
| `http://127.0.0.1:8013/static/selftest.html` | 中英联动回归自测 |
| `http://127.0.0.1:8013/static/qa_ui.html?kind=stream` | 前端流式渲染自测 |

**零数据启动**已由用例 C03 实测：设置独立 `DATA_DIR` + `VECTOR_BACKEND=sqlite`，
新实例 3.9 秒内启动成功，`/api/documents` 返回 0 篇，接口全部 200。
镜像侧另有 `.dockerignore` 排除 `data/`、`docs/qa/evidence/` 等运行期产物，
避免把浏览器 profile 与 QA 证据打进镜像。

## 3. 灌入官方测试集

```bash
curl -X POST "http://127.0.0.1:8013/api/library/rescan?sync=true"
```

- `data/pdf/` 下 39 个 PDF；实测导入 37 篇 / 19,766 块，2 组同族/同文被正确判重拦截。
- 541 页长书单独耗时 260 秒（其中向量化 246 秒）；15 页扫描件 OCR 约 115–193 秒。
- 断点续传脚本：`scripts/bulk_import.py`；只刷引用表不重算向量：`scripts/rebuild_citations.py`。
- 批量脚本写入的数据在服务进程外完成，**跑完必须重启服务**，否则进程内 BM25/语料/图谱缓存不感知新块。

## 4. Docker

```bash
cp .env.example .env     # 填密钥
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

- `Dockerfile`：`python:3.11-slim` + tesseract/中文字体，装 `requirements.txt`，暴露 8000，带 HEALTHCHECK。
- `docker-compose.yml`：挂 `./data:/app/data`，注入 `.env`，内存上限 1800M。
- 第三轮已补齐 `chromadb`（避免容器静默退化成 SQLite 检索）与漏声明的 `langgraph`。
- **E03 实测通过（Docker 配置一致性 + 布局预演）**：校验 COPY 5 项源文件齐全、compose/CMD 可解析、
  15 个第三方 import 全部声明、`.env.example` 覆盖 `Settings` 全部字段；再按 Dockerfile 的 COPY
  清单搭出等价 `/app` 目录（**不含 .env**），用零密钥 `LLM_MOCK=1` 真实启动 uvicorn，
  `/health`、`/api/system/info`、`/`、`/static/app.js`、`/docs` 五个接口全部 200。
  证据 `evidence/container_rehearsal.json`。
- **【待实测】E03D：本机未安装 Docker（`docker: command not found`），`docker build` / `docker compose up`
  无法实机执行，不计入已通过项。** 请在装有 Docker 的机器上按上述命令补测；
  布局预演只证明"镜像内的文件布局与启动命令自洽"，不能替代真实镜像构建。

## 5. 回归测试复现

```bash
./.venv/Scripts/python.exe tests/qa_suite.py --group all              # 全量
./.venv/Scripts/python.exe tests/qa_suite.py --case A04,B01,C07       # 单项回归
./.venv/Scripts/python.exe tests/qa_report.py                          # 生成 01/03 交付表
```

证据落盘：`docs/qa/evidence/run_<时间戳>.json`（逐条含完整 evidence）、
`docs/qa/evidence/ui_*.txt`（浏览器交互日志）、`docs/qa/evidence/ui_main.png`（界面截图）。

## 6. 已知限制（如实列出）

1. **Docker 未实机验证**：本机无 Docker，见第 4 节。
2. **arXiv 导出 API 在本机网络不可达**：`arxiv_search` 真实请求超时/失败后自动降级到
   本地 37 篇语料，返回体带 `degraded=true` / `source=local_library`，绝不冒充在线结果。
   GitHub REST API 与 Crossref 在本机可达（B05 实测取到真实 star / license / pushed_at）。
3. **OCR 只有 VLM 一条路**：本机无 tesseract，断网且无 VLM key 时扫描件无法入库；
   已有 `LLM_MOCK=1` 保证其余链路仍可演示。
4. **推理模型长表格易空输出**：`deepseek-v4.1-flash` 的思维链会吃掉 JSON 输出预算，
   已实现"非推理模型纯文本兜底 + 审稿节点短路"，但复杂对比任务的端到端耗时仍可达 30–90 秒。
5. **图谱真实帧率【待实测】**：无头浏览器使用虚拟时钟，1 秒内 rAF 回调只有 2 次，
   不能代表真机 FPS。已实测的是 **37 节点**首帧 265ms / canvas 998×678。
6. **长文档整篇翻译有上限**：`TRANS_MAX_PAGES=60`，541 页书籍只支持按页懒翻译（滚到哪译到哪）。
7. **DEMO_ACCESS_TOKEN 未启用**：`config.py` 留了字段但尚无鉴权中间件，公网部署前需补。
8. **Future Work 的"已被后续工作涉及"是关键词对撞结论**，措辞为 `possibly_addressed`，
   不宣称因果已证实；这是为了避免 LLM 改写产生幻觉而做的可解释取舍。
9. 本机 2 组同名 "Attention Is All You Need"（v1 与扫描版）是有意保留的，
   用于验证"两份都留"与 OCR 路径，不是重复数据错误。
10. **数字溯源对英文数词曾偏严**：论文里 `twelve hours` 与答案「12 小时」字面不匹配会被标未核实。
    现已把英文数词加入证据池（不放宽答案侧，编造数字仍拦），见 D-28。
11. **浏览器自动化依赖本机 Edge**：`tests/browser_cdp.py` 用 CDP 真实时间驱动，
    无 Edge 的机器上 B06/B07/C05/C07B 会标 BLOCKED；接口级用例不受影响。
12. **C06 内存读数需要主服务已加载语料**：用例按监听端口定位并加了 150 MB 下限断言，
    在空库新实例上跑会判 FAIL（这是刻意的防失真保护，不是崩溃）。

## 7. 本机无法验证项汇总（等价【待实测】，不计入通过）

| 编号 | 项 | 原因 | 补测命令 / 环境 |
| --- | --- | --- | --- |
| E03D | 真实 `docker build` / `docker compose up` | 本机未安装 Docker Desktop / Engine，也无 podman / nerdctl | 在装 Docker 的机器执行 `docker compose up -d --build && curl :8000/health` |
| — | 真机图谱 FPS | 无头浏览器虚拟时钟下 rAF 仅 2 次/秒，不代表真机 | 真机打开图谱视图用 DevTools Performance 录制 |
| — | 公网部署与评审口令 | 未购买域名、未启用 `DEMO_ACCESS_TOKEN` 鉴权中间件 | 部署后由用户配置 |

> 已由 E03 完成「Docker 配置一致性 + 镜像等价布局零密钥启动」实测，C03 完成「干净环境从零启动」实测，
> 但这两项**不能替代真实镜像构建**，因此 E03D 保持 BLOCKED。
