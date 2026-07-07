# 故障速查（全部来自真机实战）

按现象查。每条都是在真实 AutoDL 实例上踩过并验证过解法的。

## 网络与下载

| 现象 | 根因与解法 |
|---|---|
| `clone/fetch 失败: GnuTLS recv error (-54)` / `early EOF` | 国内实例 https 拉 GitHub 大仓库传输中断。默认浅克隆(`git.depth:1`)+学术加速(`git.turbo:true`)已缓解；确认没被关掉，重试通常成功 |
| HF 模型下载慢/失败 | 实例上 `export HF_ENDPOINT=https://hf-mirror.com`；大模型下载放后台任务跑，别占前台 |
| pip 慢 | 走 `env.pip_index`（默认清华源）。注意学术加速(`network_turbo`)对 pypi 反而可能更慢——它只加速 github/HF |
| `/root/autodl-pub` 不存在 | 公共数据集是尽力挂载，某次开机可能缺失。脚本里做回退（HF 下载），或重开机 |

## 磁盘与显存

| 现象 | 根因与解法 |
|---|---|
| `No space left on device` | 系统盘 30G 被 HF 缓存占满。`df -h /` 确认；清 `/root/.cache/huggingface/hub/models--*`（已拉回本地的产物也可清）。pip 装包一律 `--no-cache-dir` |
| CUDA OOM（训练启动前，模型加载时）| 给 TRL/Trainer 传模型名字符串会默认 **fp32** 加载（7B→28G 直接爆）。预先 `AutoModelForCausalLM.from_pretrained(..., torch_dtype=torch.bfloat16)` 再传实例 |
| CUDA OOM（GRPO 训练时）| 依次：`beta=0`（不建参考模型，省一整份权重）→ 降 `num_generations` → 降 `per_device_batch`（用 grad_accum 补）→ 降 `max_completion_length` → 图片任务 `--max_pixels 200704` |
| MoE 显存 | 训练吃**总参数**不是激活参数（所有专家都要在显存）。7B-MoE≈14G bf16 权重 |

## 硬件兼容（Blackwell / RTX 5090）

- torch≤2.5 的 CUDA 内核不支持 sm_120：装**新版 torch**（pypi 默认轮子已带 cu12.8+）。
- flash-attn 预编译轮子不兼容：跳过，训练/推理用 `--attn_implementation sdpa`。
- vllm 旧版与新 torch 冲突：非必需就跳过（GRPO 训练只有 `--use_vllm` 才需要）。
- 完整适配范例：仓库 `examples/vlm-r1-refcoco/`（含现成 patch）。

## 第三方仓库的依赖债

- 版本死结（A 模块要 transformers≥X、B 模块要 <X）：用项目 pin 的版本，把**用不到的模块桩成 no-op**，补丁进本地 `patches/`（git.patches 自动应用，不 push 上游）。
- 上游 Trainer 子类绑定特定 transformers 内部 API（如 `_get_train_sampler` 签名变化）：别升 transformers 迁就单个 import，回到项目 pin 版本再桩掉冲突模块。
- 补丁规则：必须 `git diff` 生成（手写 hunk 过不了严格校验）；目标行别贴文件边界（留上下文）；生成时别 `strip()`（会剥掉尾换行→corrupt patch）。

## 训练有效性

- **RL 冷启动失败**：base 模型完全不会目标格式 → reward 恒 0 → 组内无方差 → 无梯度。表现为 loss/KL 微动但 reward 全 0。解法：换有底子的 base（如 REC 任务 Qwen2.5-VL-3B 而非 2B），或先 SFT 冷启动再 RL。**训练前先花几分钟测 baseline**。
- **reward 刷满但没提升**：训练 reward → 1.0 而 held-out 准确率不涨甚至降 = 过拟合小训练集/reward hacking。判定效果只能靠 held-out 对比 base，不能看训练 reward。
- 输出格式类失败往往不是"看不懂"而是"制式错"（如坐标用 0-1 归一化而解析器要整数像素）——看模型原始输出文本定位，别只看 reward。

## 执行环境细节

- ssh 非交互 shell 不读 .bashrc：conda/pip 可能不在 PATH，脚本开头 `. /root/miniconda3/etc/profile.d/conda.sh && conda activate base`（setup 已内建此逻辑）。
- 训练脚本用 `tee` 落日志会掩盖退出码：`set -e` + 解析步骤放 tee 之后，或检查 `PIPESTATUS`。
- 换模型复跑前清 `output_dir`：旧 checkpoint 会被自动 resume，形状不匹配直接崩。
- 长任务监控：交互用 `autodl logs -f`；脚本/agent 用 `until ... logs --json | jq -e '.status != "running"'; do sleep 60; done`。macOS 本地没有 `timeout` 命令（GNU coreutils），别在本地 shell 里用。
- heredoc 嵌套注意 shell 展开：单引号 heredoc（`<<'EOF'`）内 `$VAR` 不展开——python 代码里需要的路径直接写死或在 python 内定义。
