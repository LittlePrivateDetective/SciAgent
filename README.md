# SciAgent —— 科研助手 Agent

面向 AI 大模型创新应用挑战赛（第九届传智杯）的科研全流程助手：文献解析 → 抗幻觉 RAG 问答（原文锚点溯源）→ 多篇对比 → 引用图谱 → 写作辅助。

## 当前进度

| 模块 | 状态 |
| --- | --- |
| 双栏感知 PDF 解析（页码 + bbox 锚点） | 可用 |
| 检索单元合并（citation-ready chunk） | 可用 |
| FastAPI 服务骨架 | 可用 |
| 混合检索（BM25 + 向量） | 开发中 |
| 问答溯源（bbox 回跳） | 开发中 |
| 引用图谱（PageRank 核心节点） | 未开始 |
| 前端界面 | 未开始 |

## 技术架构

```
PDF ──► parse_pdf（PyMuPDF，栏归属 + 阅读顺序 + bbox）
          │
          ▼
     merge_blocks（600 字窗口，保留合并包围盒）
          │
          ▼
     retriever（BM25 + 向量，混合打分）
          │
          ▼
     answer（LLM + 强制引用，输出 [p{page}@{x},{y}] 锚点）
          │
          ▼
     前端高亮回跳原页
```

## 安装运行

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env            # 填入模型 API Key
uvicorn app.main:app --reload
```

解析单个 PDF：

```bash
python -m app.parse_pdf data/sample.pdf
```

## 设计取舍

- **锚点而不是摘要**：每个 chunk 携带页码与包围盒，回答必须给出可点击的原文位置，用可验证性对抗幻觉。
- **栏感知排序**：双栏论文按「先左栏、后右栏」还原阅读顺序，跨栏标题自动断段，避免把左右栏句子串成一句。
