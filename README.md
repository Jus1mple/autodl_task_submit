# autodl-task-submit

AutoDL GPU 云的 **Python 库 + 命令行工具**：开机/复用实例、传代码/数据、跑任务（前台流式 / 后台脱机 / 批量并行）、抓指标拿结果、按需关机/释放。

用法对标 `huggingface-hub`：`pip install` 装完即有 `autodl` 命令，跑任务一条命令、`--json` 拿结构化数据；同一套能力也可以当库 `import autodl` 直接调。

> 可视化大盘（FastAPI + 网页）**不在核心包里**，在 [`web-dashboard`](../../tree/web-dashboard) 分支单独维护——核心包保持零 web 依赖（只有 requests / paramiko / pyyaml / dotenv）。

## 安装与配置

```bash
pip install git+https://github.com/Jus1mple/autodl_task_submit.git   # 或克隆后 uv sync
echo 'AUTODL_TOKEN=你的Token' > .env  # Token 只放环境变量/.env，不进 yaml（已被 .gitignore）
cp autodl.example.yaml autodl.yaml    # 可选：改实例规格/区域/预算/SSH/git 仓库
```

Token 在 AutoDL 控制台 → 账号 → 设置 → 开发者 Token 获取。**加载优先级**（前者覆盖后者）——原则是**项目本地 `.env` 覆盖一切**，在哪个项目目录里跑就用哪个项目的 token：

1. 当前目录及其父目录里的 `.env`（项目本地，最高——会覆盖已导出的全局环境变量）
2. `autodl.yaml` 所在目录的 `.env`（`--config` 指到别处时跟着配置走）
3. 已导出的环境变量：`export AUTODL_TOKEN=...`
4. `~/.autodl/.env`（全局兜底：配一次，任意目录都能用，只补缺）

## 命令行快速上手

实例里代码/环境已就绪，直接跑上面的脚本、跑完关机：

```bash
autodl use pro-xxxxxxxx                                        # 一次性登记你的实例
autodl run --remote-script ~/proj/submit_exec.sh --down        # 前台实时回显，结束自动关机
```

长任务后台跑，之后随时查进度/退出码/指标：

```bash
autodl run --remote-script ~/proj/train.sh --background        # 立即返回 run_id
autodl logs --run-id <run_id>                                  # tail 日志 + 状态 + 指标
```

## 拿回数据（--json）

所有命令支持 `--json`：**stdout 只输出 JSON，进度日志走 stderr**，可以直接 `| jq` 或被脚本消费。

```bash
autodl run --remote-script ~/proj/run.sh --down --json | jq .metrics
# {"ASR": 0.75, "Accuracy": 0.9}

autodl logs --run-id <id> --json     # {run_id, status, exit_code, log, metrics}
autodl runs --json                   # 台账里全部运行记录（含指标，只查本地不触网）
autodl status --json                 # 余额 + 实例列表 + 计费分类
```

**指标怎么来**：任务把结果写到实例的 `数据盘/metrics.json`（顶层标量即最终指标），或直接打印 `ASR: 0.75` 这类日志——运行结束时自动按 json + 日志正则抓取入库（`.autodl/registry.db`）。

## Python API

CLI 的全部能力都有对应的库接口：

```python
from autodl import connect, submit

ctx = connect()                            # 读 autodl.yaml + .env 的 AUTODL_TOKEN
res = submit(ctx,
             mode="remote_script",         # remote_script | remote | script | script_text
             value="~/proj/submit_exec.sh",
             teardown="power_off",         # keep | power_off | release（无论成败都执行，止损优先）
             stream=lambda s: print(s, end=""))   # 可选：实时回显
print(res["exit_code"], ctx.reg.get_metrics(res["run_id"]))
```

后台提交 + 稍后对账：

```python
meta = submit(ctx, mode="remote", value="cd ~/proj && bash train.sh", background=True)
# ... 之后任意时刻：
from autodl import tasks
run = ctx.reg.get_run(meta["run_id"])
info = tasks.refresh_run(ctx, run)         # 探活/tail/完成登记/抓指标
```

底层件也全部可用：`ctx.api`（HTTP 客户端）、`ctx.ssh`（SSH 层）、`ctx.reg`（SQLite 台账）、`ctx.ensure_instance()` / `ctx.finish_instance()`（实例编排）。

## 命令一览

| 命令 | 作用 |
|---|---|
| `balance` | 查余额 |
| `stock [--region R]` | 查 GPU 库存（默认遍历偏好区域）|
| `status` / `ls` | 列出所有实例，标注"带卡计费 / 仅磁盘计费" |
| `stop-all [--release]` | 一键止损：关停所有 running 实例（`--release` 则彻底释放）|
| `up [--select-region]` | 拉起/复用实例，注入公钥免密，写 `~/.ssh/config`，打印 SSH/VSCode/Jupyter 直连信息 |
| `use <instance_uuid>` | 登记一台**已有**实例为当前活动实例（不创建）|
| `run ...` | 在实例上执行任务（见下；`--sync` 跑前先更新实例仓库）|
| `clone [--repo URL]` | 在实例上克隆项目仓库（幂等；已有则 fetch）|
| `sync [--mode M]` | 实例仓库从 remote 更新（本地 push 之后；见 git 工作流）|
| `setup [--force] [--background]` | 按仓库依赖声明准备环境（hash 幂等，未变秒跳）|
| `patch` | 把本地 patch 目录(git.patches)应用到实例（clone/sync 已自动带）|
| `logs [--run-id R]` | 查看后台任务日志 + 退出码 + 指标 |
| `runs [--limit N]` | 列出台账运行记录（含指标，只查本地）|
| `push <本地目录> [子目录]` | rsync 同步本地到实例数据盘 |
| `pull <远端子路径> [本地目录]` | rsync 从实例拉回产物 |
| `down [--release]` | 关机（或释放）当前活动实例 |
| `snapshot-env --name N [--instance U]` | 把实例环境存为私有镜像（持续占存储费，无删除 API，慎用）|
| `idle-guard [--once] [--idle-minutes N]` | 空闲自动关机看门狗（GPU 利用率+显存双判，keepalive 文件可豁免）|
| `balance-watch --warn Y --stop Y` | 余额预警/急停守护（低于急停线自动关停）|
| `batch --file jobs.yaml [--max-parallel N]` | 批量并行调度：多实例跑多任务，记 run 台账、支持 resume |

通用参数放在命令**后面**：`--config`、`--dry-run`、`--yes/-y`、`--json`。

`run` 的执行方式（互斥）与收尾：
- `--remote-script <实例上的路径>`：跑实例已有脚本（`~` 正确展开为 `$HOME`）
- `--remote "<命令>"`：直接执行任意远端命令
- `--script <本地路径>`：上传本地 bash 脚本再执行
- `--down` 跑完关机（保留磁盘环境，下次自动开机复用）；`--release` 彻底释放；`--background` 后台脱机（不能与 `--down/--release` 同用，跑完自行 `autodl down`）
- 前台/后台运行都记入台账、自动抓指标；`run_id` 自动生成唯一 id（`--name` 可指定）
- `--pull '<glob>' --pull-to <目录>`：**任务完成自动把结果文件拉回本地**（与自动抓指标同一时机）。glob 逗号分隔、相对数据盘、保留相对结构，如 `--pull 'output/checkpoint-*/adapter*.safetensors,metrics.json' --pull-to ./results`。后台任务在 `autodl logs` 检测到完成时拉回。只改本地、不动实例。

## 远程 git 工作流：改代码 → push → 实例更新 → 重跑

日常迭代循环（本地是唯一改代码的地方，实例上的仓库只是**部署副本**）：

```yaml
# autodl.yaml 一次性配置
git:
  repo: https://github.com/you/proj.git   # 私有库：本地 export AUTODL_GIT_TOKEN=...
  branch: main
```

```bash
autodl clone                             # ① 实例数据盘克隆项目（幂等，断电不丢）
autodl setup                             # ② 按仓库依赖装环境（探测 setup.sh/environment.yml/
                                         #    requirements.txt/pyproject；hash 幂等，未变秒跳）
autodl run --remote-script '~/autodl-tmp/repo/train.sh' --background
autodl logs --json | jq '{status, exit_code, metrics}'   # ③ 回传结果，本地解析

# ④ 根据结果本地改代码/改依赖 → git commit && git push，然后一条命令：
autodl run --sync --setup --remote-script '~/autodl-tmp/repo/train.sh' --background
#          ^^^^^^^^^^^^^^ 先更新代码（缺仓库自动 clone），依赖 hash 变了才重装环境，然后跑
autodl sync --json                       # 也可以单独更新不跑任务
```

`setup` 说明：自定义安装命令配 `env.setup`（最高优先）；pip 自动走国内镜像源（`env.pip_index`）且 `--no-cache-dir` 防系统盘爆；`env.academic_turbo: true` 先开学术加速；hash 记在实例系统盘（换实例/换镜像自动失效重装）；安装很久可 `--background` 后 `logs` 查进度；装好后建议 `snapshot-env` 固化成镜像，新实例秒级就绪。

**实例上的数据变了怎么更新**（`--mode` / `--sync-mode` 三种策略）：

| 策略 | 行为 | 适用 |
|---|---|---|
| `ff`（默认） | 跟踪文件被改过就**拒绝更新**，回传改动清单，绝不丢数据 | 日常；配合"产物写仓库外"的约定永远干净快进 |
| `stash` | 改动（含未跟踪文件）收进 `git stash` 再快进，实例上可恢复 | 在实例上临时改过代码调试，想保留 |
| `reset` | `reset --hard` 到远端；丢弃跟踪文件改动，**未跟踪文件（产物/数据）保留** | 远端为准，实例上的手改不要了 |

**约定**：训练产物/数据集写到仓库外（如 `~/autodl-tmp/outputs/`、指标写 `~/autodl-tmp/metrics.json`），仓库目录保持只读部署——这样 `sync` 永远是干净快进，产物也不会被任何策略碰到。产物用 `autodl pull outputs ./results` 拉回本地。

私有仓库（https）：本地设 `AUTODL_GIT_TOKEN`，`clone` 时会把凭据写进实例的 `~/.git-credentials`（0600），token 不进命令行和日志。

> 完整实战样例见 [`examples/vlm-r1-refcoco/`](examples/vlm-r1-refcoco/)：用 autodl 把官方 VLM-R1 的 REC 训练在单卡 5090 上端到端跑通（clone → 打 5090 适配补丁 → 装环境 → 备真实 RefCOCOg 数据 → GRPO-LoRA 训练 → `--pull` 拉回 adapter → 评估效果）。Qwen2.5-VL-3B 训 200 步：训练 IoU 0.11→0.80、format 0→1.0；held-out 测试定位准确度 base 0.52 → trained 0.71，IoU>0.5 命中率 47%→73%。

**改别人的仓库不想 push？用本地 patch 目录。** 当你 clone 的是别人的项目、需要打适配补丁（改依赖 import、桩掉用不到的模块、补字段——就像给 5090 适配一个官方栈跑不通的项目），把补丁存**你本地**，实例上只改工作区、**绝不 commit/push**：

```yaml
git:
  repo: https://github.com/someone/their-project.git
  patches: patches/     # 本地目录，放 *.patch（git diff 生成），用 NN-name.patch 控顺序
```

```bash
# 生成 patch：在本地改好后 git diff > patches/01-fix.patch
autodl clone            # clone 后自动 git apply 本地 patch（幂等）
autodl patch            # 也可单独重放（调试 patch 时用）
autodl sync             # 更新时：先 reset 回干净上游、拉最新、再重放 patch —— 永不累积
autodl run --sync ...   # 一条龙：更新代码+重放patch → 跑
```

- patch 只改实例工作区、不进 git 历史 → 物理上不可能被推回别人的仓库。
- `sync` 配了 patch 会自动用 reset 语义（丢弃上轮 patch、保留产物、重放新 patch）。
- patch 冲突（上游改了补丁邻近行）会明确报出，让你更新 patch，而非 fuzzy 强应用到错位置。patch 目标行留足上下文（别贴文件边界）更稳。

国内实例拉 GitHub 大仓库易因传输中断（GnuTLS/EOF）失败，`clone` 默认**浅克隆**（`git.depth: 1`，部署副本无需全历史）+ 走 **AutoDL 学术加速**（`git.turbo: true`，`source /etc/network_turbo`）+ 放宽慢速超时。需要全历史（如 `git describe`）改 `git.depth: 0`。后续 `sync` 的增量 fetch 不受深度限制，`ff` 快进照常可用。

## 批量并行（超参搜索等）

任务清单 `jobs.yaml`（每个任务三选一：`remote_script` / `remote` / `script`）：

```yaml
- id: lr_1e-3
  remote_script: ~/proj/run.sh        # 跑实例上已有脚本（可在脚本里读环境变量区分配置）
  pull: 'metrics.json,output/*.pt'    # 该任务完成后拉回的结果文件（拉到 <--pull-to>/lr_1e-3/）
- id: lr_1e-4
  remote: "cd ~/proj && LR=1e-4 bash run.sh"
- id: baseline
  script: ./local_task.sh             # 上传本地脚本再跑
```

```bash
autodl batch --file jobs.yaml --max-parallel 3 --on-finish release --pull-to ./results
```
- 至多 `--max-parallel` 台实例并行，一台跑完自动取下一个任务（实例复用）。
- **每个任务完成后自动拉回各自的结果文件**到 `<--pull-to>/<job_id>/`（job 里写 `pull:`，或用命令行 `--pull` 给所有 job 统一 glob）——多任务的产物互不覆盖。
- 结果记入 `.autodl/registry.db`；中断后重跑会 **resume**（跳过已成功的任务）。
- `--on-finish release|power_off|keep` 控制收尾；`--retries N` 单任务失败重试；`--select-region` 按库存选区。

## 省钱守护（无人值守）

```bash
# 空闲自动关机：连续 15 分钟 GPU 利用率<5% 且显存<500MiB 就关机
autodl idle-guard --instance pro-xxxx --idle-minutes 15
#   交互调试时想豁免：在实例上 touch /root/.autodl_keepalive 即不会被关
#   --once 只探一次（适合挂 cron 巡检）

# 余额守护：低于 50 元通知，低于 10 元自动关停所有 running 实例
autodl balance-watch --warn 50 --stop 10 --interval 300
```
两者都靠客户端轮询实现（AutoDL 无空闲关机开关、无 webhook）。SSH 连不上时看门狗会回退查实例状态，**绝不把"网络不通"误判成"空闲"而误关**。

## 环境固化

```bash
autodl snapshot-env --name myenv-v1 --instance pro-xxxx
```
把配好的实例存成私有镜像并轮询到 `finished`，之后在 `autodl.yaml` 设 `image_uuid` 即可秒级复现环境。⚠️ AutoDL **没有删除镜像的 API**，镜像会持续占存储费，只能去控制台删——确认需要再存。

## 设计要点

- **统一任务管线（`autodl/tasks.py`）**：CLI `run`、`batch` 调度器、`submit.py` 全部走同一套"构造命令 → 执行 → 记台账 → 抓指标 → 收尾"。每次运行前会先清掉实例上的旧 `metrics.json`，避免上一轮指标被算到本轮头上。
- **统一收尾（`Context.finish_instance`）**：`keep / power_off / release` 一处实现；release 前**轮询等实例真正 shutdown**，release 返回业务失败码也算失败并重试，最终失败会显著告警（绝不静默泄漏计费）。
- **HTTP 稳健层**：超时 + 对只读接口指数退避重试；`create`/`power_*`/`release` 等写接口**绝不自动重试**，避免重复开机/重复扣费。
- **SSH 连接层**：失败按根因分类（实例已关机 / 跳板不通 / 端口未就绪 / 认证失败），不会把"网络不通"误判成"实例已关机"。
- **凭据安全**：首连后注入本地 SSH 公钥走免密，`root_password` 只在内存用一次，不进命令行/日志。
- **本地台账**：`.autodl/registry.db`（带文件锁的 SQLite）记录实例与任务（前台/后台/批量都进同一张 runs 表）。
- **GET 接口走 query string**：AutoDL 网关会丢弃 GET 的 body（这是早期踩过的坑）。

> 旧入口 `submit.py` 仍可用（`uv run python submit.py`），是包之上的薄封装，等价于 `autodl run --script <脚本> --down`。可视化大盘见 `web-dashboard` 分支。
