# SciAgent 最终回归报告

- 生成时间：`2026-11-11T19:22:28`
- 证据来源：`docs/qa/evidence/run_20261111_191313.json`
- 服务地址：`http://127.0.0.1:8013`

## 汇总

| 指标 | 数值 |
| --- | --- |
| 用例总数 | 40 |
| PASS | 39 |
| FAIL | 0 |
| BLOCKED（等价【待实测】） | 1 |
| 通过率（PASS / 可测项） | 39/39 = 100.0% |

## 零崩溃 Bug 证据

- 状态：**PASS**；本轮窗口零 Traceback（历史 2 处已被兜底），11 个接口全部 200
- 日志扫描行数：2360；Traceback 数：0
- 接口状态码：{"/health": 200, "/api/system/info": 200, "/api/documents": 200, "/api/tasks": 200, "/api/graph": 200, "/api/tools/health": 200, "/api/tools/catalog": 200, "/api/documents/trash": 200, "/api/audit": 200, "/api/research/future-work": 200, "/api/research/reproducibility?online=false": 200}

## 失败项

- 无。全部可测用例通过。

## 无法在本机验证项（【待实测】）

- **E03D** c-3 真实 docker build（【待实测】）：本机无 Docker，无法执行 docker build；已由 E03 完成等价布局预演，真实构建需在装 Docker 的机器执行：docker compose up -d --build

## 多轮循环收敛（每轮都是真实运行，非推断）

| 轮次 | 证据文件 | 用例 | PASS | FAIL | BLOCKED |
| --- | --- | --- | --- | --- | --- |
| 第一轮 | `round1_console.log` / `run_20261111_113738.json` | 29 | 15 | 8 | 6 |
| 第二轮 | `round2_console.log` / `run_20261111_121934.json` | 35 | 26 | 8 | 2 |
| 第三轮 | `round3_console.log` / `run_20261111_141641.json` | 36 | 35 | 0 | 1 |
| 交付轮 | `delivery_console.log` / `run_20261111_144615.json` | 36 | 35 | 0 | 1 |

第三轮到交付轮之间又做了 3 处修复（C06 内存采样失真、C07 英文数词误报、Dockerfile 缺 `.dockerignore`），并分别做了定向回归 + 一次完整全量复跑，详见 `02_defects.md` D-27 ~ D-29。

## 硬性测试用例逐条对应

| 硬性用例 | 覆盖编号 | 实测结论 |
| --- | --- | --- |
| 1 双栏/单栏/扫描件 OCR/百页长文档，核对标题树·公式·表格·图表·参考文献 | A05、C04 | 37 篇含 15 页扫描件、100 页与 541 页长文档；四类分块与标题树/参考文献齐全 |
| 2 Dense/Sparse 混合检索 + 10 条引用回原文核对 | A03、A04 | **10/10 命中**，双通道均有贡献，锚点各自独立 |
| 3 多文档并发导入不崩溃/不串档/无泄漏 | C01、C02、C06 | 3 篇真实并发入库、4 路并发问答无串答，RSS Δ+0.1 MB |
| 4 20+ 论文图谱流畅度；CSV→TikZ/Mermaid；后台保活 | C05、C09、C12、C13、C14、B01 | 37 节点首帧 250ms；四种图表 × 五种格式数值逐格保留；架构图四种绘图代码同构 |
| 5 版本对比 / 引用跳转 / 中英同步滚动 / 语义去重 | B04、B07、B06、B03 | v1↔v7 章节级差异、p3 锚点跳转、双栏顶部段落 7/7 对齐、三层去重全命中 |
| 6 Function Calling 成功 / 超时 / 失败重试 / 降级 | A06、A06B、A06C | GitHub 在线成功；超时重试 3 次、失败重试 2 次，均降级并标注来源 |
| 7 流式完整性 / 思考可视化 / 错误边界 | C07、C07B、C08 | 36 个 token 无损拼接、引用校验 8/8；前端时间线渲染；异常 400/404/422 无 5xx |
| 8 全新环境按 Docker 文档从零复现 | C03、E03、**E03D** | 干净环境 3.9s 启动 + 镜像等价布局零密钥启动 5 接口全 200；**真实 docker build 仍【待实测】** |
| 9 性能基线 | C04、C05、C06、F02、02_defects 性能表 | 页图 90ms / 图谱 250ms / RSS 796 MB / 过滤 99.916% 语料；优化前后对比见 02 |

## 复现命令

```bash
cd sciagent
./.venv/Scripts/python.exe tests/qa_suite.py --group all
./.venv/Scripts/python.exe tests/qa_report.py
```

> 说明：BLOCKED 项等价于【待实测】，**不计入通过**。本机未安装 Docker 属环境限制，不是产品缺陷；已用 E03 布局预演与 C03 干净环境启动做等价验证，但绝不冒充真实镜像构建。
