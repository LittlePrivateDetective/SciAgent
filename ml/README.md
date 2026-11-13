# 本地模型微调（LoRA / QLoRA）

这个目录做一件事：**把库里的论文变成一份能训 3B 模型的指令数据，训出一个"会按
SciAgent 规矩答论文题"的本地模型，并用可判定的指标证明它到底变好了没有。**

它和主站的关系是"旁路"：主站默认走云端大模型（`LLM_ROUTE=cloud`），这里产出的模型
只在 `LLM_ROUTE=local` 时接管。代码一行不改，只换 `OLLAMA_MODEL`。

## 为什么这么设计

1. **数据不是凭空造的**：每条样本都来自库里真实的论文段落，强模型只负责"出题 + 按规范作答"，
   再用规则把不合格的丢掉（引用越界、数字不在证据里、元话语、过短过长）。
2. **格式与线上完全一致**：`system` 用 `app/agents/prompts.py` 的 `SYSTEM_CONTRACT`，
   输入是「【证据】+【问题】」，输出带 `[n]` 引用。翻译样本则对齐本地路由的
   `_SYS_LITE` + JSON 数组进出——**格式错位比内容错位更难查**。
3. **只用硬指标评**：引用率、引用越界率、数字出错率、元话语率、拒答正确率、字/秒。
   不请模型当裁判，避免"自己给自己打分"。

## 流程

```bash
# 0) 依赖（与主站隔离，单独一个 Python 3.12 环境；主站是 3.13，互不影响）
py -3.12 -m venv ml/.venv
ml/.venv/Scripts/pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
ml/.venv/Scripts/pip install transformers peft bitsandbytes accelerate datasets

# 1) 造数据（在**主站 venv** 里跑：要用 app 的解析与提示词）
.venv/Scripts/python.exe ml/prepare_sft.py --n 700 --deny 80     # → data/sft/qa.jsonl
.venv/Scripts/python.exe ml/prepare_trans.py                     # → data/sft/trans.jsonl
.venv/Scripts/python.exe ml/split.py                             # → train.jsonl / dev.jsonl

# 2) 训练（ml/.venv；3B QLoRA，6GB 显存）
ml/.venv/Scripts/python.exe ml/train_lora.py --dry-run           # 先看编码与显存
ml/.venv/Scripts/python.exe ml/train_lora.py                     # 正式

# 3) 合并 LoRA（fp16，在 CPU 上合并；6GB 显存合并不下）
ml/.venv/Scripts/python.exe ml/merge_lora.py \
  --lora data/sft/out/lora-3b --out data/sft/out/merged-3b

# 4) 转 GGUF 并导入 Ollama（Modelfile 的 FROM 指向 sciagent-3b-f16.gguf）
ml/.venv/Scripts/python.exe <llama.cpp>/convert_hf_to_gguf.py data/sft/out/merged-3b \
  --outfile data/sft/out/sciagent-3b-f16.gguf --outtype f16
ollama create sciagent-3b -f data/sft/Modelfile

# 5) 三列评测（base / 微调 / 云端；云端读 .env 的 LLM_*）
ml/.venv/Scripts/python.exe ml/eval_ft.py --lora data/sft/out/lora-3b --cloud
```

## 显存账（RTX 2060 6GB）

| 项 | 占用 |
| --- | --- |
| 3B 权重 4bit（nf4 + 双重量化） | ≈2.0 GB |
| 激活（seq 1024、batch 1、梯度检查点） | ≈1.5 GB |
| LoRA 参数 + 优化器（r=16，约 20M 可训参数，paged adamw 8bit） | ≈0.3 GB |
| CUDA 上下文 + 碎片 | ≈0.5 GB |
| **合计** | **≈4.3 GB** |

所以 `batch=1` + `grad_accum=8`，再大必 OOM。Turing 不支持 bf16，训练用 `fp16`
（硬上 bf16 会得到 NaN loss）。

## 产物

- `data/sft/out/lora-3b/`：LoRA 权重（120MB），**不进仓库**
- `data/sft/out/merged-3b/`：合并后的 fp16 权重（5.75GB），**不进仓库**
- `data/sft/out/sciagent-3b-f16.gguf`：GGUF（6.17GB），**不进仓库**
- Ollama 模型 `sciagent-3b:latest`（6.18GB，F16）存放在 `~/.ollama/models`
- `ml/models/`：底座权重（5.8GB），**不进仓库**
- 训练日志与评测结果：`data/sft/`（不进仓库）；结论见 `docs/微调实验.md`

## 已知边界

- 3B 的天花板低于云端 200B 级模型：**微调能让它"守规矩"（引用格式、数字保真、拒答），
  不能让它在复杂对比题上追上云端**。评测表里会把这两类分开看。
- 训练数据只有几百条，覆盖 37 篇论文；换一批文献要重新造数据。
- 4bit 训练本身有精度损失；如果显存够（≥12GB），`bf16` + `r=32` 会更稳。
