# 用 autodl 跑通 VLM-R1 REC 训练（真实数据，端到端）

用 `autodl` 在一台 AutoDL 实例上，把官方 [VLM-R1](https://github.com/om-ai-lab/VLM-R1) 的指代表达定位（REC）训练完整跑通：**克隆代码 → 打适配补丁 → 装环境 → 备真实数据 → GRPO-LoRA 训练 → 结果文件拉回本地**。全程一台单卡（实测 RTX 5090 32G）。

这个例子专门展示 `autodl` 对付「别人的、且官方栈在你硬件上跑不通的」真实项目：适配补丁存你本地、只改实例、绝不 push 回上游；训练指标自动入本地台账；产出的 LoRA adapter 用 `--pull` 声明式拉回。

## 为什么要适配（背景）

- VLM-R1 官方栈是 `torch 2.5.1+cu124 + flash-attn + vllm`，在 RTX 5090（Blackwell / sm_120）上 CUDA 跑不通 → 换支持 5090 的新版 torch，训练用 `--attn_implementation sdpa`，跳过 flash-attn/vllm。
- 项目 main 分支代码有依赖债：`glm_module.py` 要 `transformers>=4.53` 的 `Glm4v`，`qwen2_5vl_monkey_patch.py` 要 `<4.53` 的 `Qwen2_5_VLVisionFlashAttention2` —— 无单一版本满足。用项目原生 pin 的 `4.49`，把这两个我们用不到的模块桩掉（见 `patches/01-5090-adapt.patch`）。
- 这些补丁改的是别人的仓库，存本地 `patches/`，`autodl clone/sync` 自动 `git apply`，**不进实例 git 历史、不可能被 push 回上游**。

## 用法

```bash
# 0) 配置：复制样例配置 + 放 token
cp examples/vlm-r1-refcoco/autodl.example.yaml autodl.yaml
echo 'AUTODL_TOKEN=你的Token' > .env

# 1) 克隆 VLM-R1 到实例 + 自动应用 5090 适配补丁
autodl clone

# 2) 装环境（transformers 4.49 + trl + peft + grpo_jsonl 依赖；hash 幂等，装一次）
autodl setup --background && autodl logs   # 装完再往下

# 3) 备真实数据：RefCOCOg 标注 + COCO train2014 子集（优先用 AutoDL 公共数据集 COCO14）
autodl run --script examples/vlm-r1-refcoco/prepare_data.sh --background --name prep
autodl logs --run-id prep                  # 看 "DONE"

# 4) 训练 + 结果文件自动拉回本地
autodl run --script examples/vlm-r1-refcoco/train.sh --background --name train \
  --pull 'real_run/output/checkpoint-*/,real_run/metrics.json' --pull-to ./results
autodl logs --run-id train                 # 完成时自动拉回 adapter + metrics 到 ./results/

# 5) 看结果（指标已入本地台账，关机也不丢）
autodl runs --json | jq '.runs[0].metrics'

# 6) 评估效果：对比 base vs 训练后模型的定位准确度
autodl run --script examples/vlm-r1-refcoco/eval.sh --name eval
autodl down --yes                          # 关机（保留环境，下次秒开复用）
```

数据量可调：`prepare_data.sh` 的 `N_SAMPLES`（默认 2000）、`train.sh` 的 `STEPS`（默认 200）/ `MODEL`（默认 Qwen2.5-VL-3B）/ `NUM_GEN` / `MAX_PIXELS`。显存不够就调小 `NUM_GEN` 或 `MAX_PIXELS`（真实 COCO 图分辨率高）。

## 实测结果（Qwen2.5-VL-3B，2000 样本，200 步，真实 RefCOCOg）

教科书式的 GRPO 学习曲线——format 前 30 步先学会（升到 1.0），IoU（定位准确度）随后爬升到 0.8：

| 指标 | 起点 → 终点 | 峰值 |
|---|---|---|
| reward（format + IoU） | 0.11 → 1.80 | 1.95 |
| IoU（定位准确度） | 0.11 → 0.80 | 0.95 |
| format（格式遵循） | 0 → 1.0 | 1.0 |

**效果评估**（`eval.sh`，held-out 测试样本，base vs 训练后）：

| | 平均 IoU | IoU>0.5 命中率 |
|---|---|---|
| base Qwen2.5-VL-3B（未训练） | 0.518 | 47–55% |
| trained 200 步（2000 样本） | 0.713 | 73% |
| **trained 500 步（8000 样本，对齐官方发布训练量）** | **0.835** | **85%** |

500 步版即对齐官方发布的 `Qwen2.5VL-3B-VLM-R1-REC-500steps` 的训练量（单卡 2.5 小时）。步数/数据继续加，收益递减但仍有空间。

对照：换成 Qwen2-VL-**2B** 时 reward 始终为 0（模型不遵循 `<think></think><answer>{...[bbox]...}</answer>` 格式）——同样的流程，模型规模不够就训不动。所以这台 32G 单卡上，**Qwen2.5-VL-3B + LoRA** 是甜点。

## 文件

- `autodl.example.yaml` —— git 仓库 + patches + 5090 适配环境
- `patches/01-5090-adapt.patch` —— 桩掉 GLM 模块 + flash-attn monkey-patch（从官方仓库 `git diff` 生成）
- `prepare_data.sh` —— 下 RefCOCOg 标注 + 解压 COCO 子集
- `train.sh` —— Qwen2.5-VL-3B GRPO-LoRA 训练（官方 `grpo_jsonl.py` + 官方参数，单卡适配）
- `eval.sh` —— 对比 base vs 训练后模型的定位准确度（IoU）
