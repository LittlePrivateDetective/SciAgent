# 本地模型实测脚本（归档副本）

> 这是 `<bench-dir>` 的归档副本，方便随仓库一起交付。
> 实际跑建议复制到仓库外的独立目录（脚本会把 `results/` 写到自身旁边），
> 结论见 [`docs/本地模型实测.md`](../本地模型实测.md)。

# 本地模型 vs 云端模型：实测目录

这是 `<repo>` 的**旁路测试目录**，只做一件事：把项目里的模型换成
本地 3B 模型，用同一批题目跟云端模型比，看清楚差在哪、哪些功能还能用。

主仓库不动，`data/` 是空的独立目录——测试不会写脏演示库。

## 目录

```
prompts.py        40 组固定测试题（问答 / 英译中 / 中译英 / 结构化 JSON / 长文本）
bench.py          主基准：流式调用，测首字延迟、吞吐、质量故障率
concurrency.py    并发对比：4 路并发 vs 串行，看本地单卡能不能扛
charts.py         把结果画成 5 张图（中文标注）
results/          每轮的原始 json（含逐题答案，可直接翻）
charts/           生成的图
```

## 怎么跑

前置：本机起了 Ollama，并拉过模型。主仓库的 `.env` 里要有云端 key（脚本只读它，不复制）。

```bash
ollama serve                   # 另开一个窗口
ollama pull qwen2.5:3b         # 约 1.9GB，Q4_K_M

cd <bench-dir>
C:\...\sciagent\.venv\Scripts\python.exe bench.py --models local,cloud-fast,cloud-main
python concurrency.py --models local,cloud-fast --workers 4 --n 8
python charts.py
```

`--limit 5` 先冒烟；`--cat json` 只跑某一类；`--repeat 2` 看稳定性（会跑两遍，用于算方差）。

## 三个容易测错的地方（都已处理）

1. **KV cache 会把长文本测成假的**。Ollama 按最长公共前缀复用上下文，六条长 prompt
   如果共用同一个开头，后五条的 prefill 会被整段跳过，TTFT 从 ~950ms 掉到 ~160ms。
   所以每条材料的开头都是唯一的。
2. **CoT 模型需要更多输出预算**。deepseek-v4.1-flash 的推理 token 也算在 `max_tokens` 里，
   预算给少了会返回空串——这不是模型不行，是配置问题。脚本给它额外 +600。
3. **预算是任务属性，不是模型属性**。第一轮 JSON 题三路全挂，原因是"抽架构节点"要
   8–10 个节点、320 token 装不下。给到 800 之后三路基本都能过。报告里区分了
   "预算不足"和"能力不足"。

## 看结果

`results/bench_*.json` 里每行是一道题在一个模型上的记录：

```
ttft_ms / total_ms   首字延迟 / 总耗时
in_tokens / out_tokens
tok_s                生成速度（CoT 模型这个数偏高，见报告说明）
empty / json_ok / lang_ok / repeat_ratio   质量故障标记
cost_cny             按公开价折算的成本
answer               前 600 字原文，可直接翻
```

结论写在 `report.md`，同步一份到主仓库 `docs/本地模型实测.md`。
