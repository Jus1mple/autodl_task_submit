# autodl 使用手册

`autodl` 是 AutoDL GPU 云的 **Python 库 + 命令行工具**：开机/复用实例、传代码传数据、跑任务（前台流式 / 后台脱机 / 批量并行）、实时跟随进度、抓指标、拉回结果文件、按需关机/释放。设计对标 `huggingface-hub`：装完即用，命令行返回可选的机器可读数据（`--json`），也可以当库 `import`。

本手册是完整参考；快速上手看 [README](README.md)，端到端实战样例看 [examples/vlm-r1-refcoco/](examples/vlm-r1-refcoco/)。

---

## 1. 安装（uv）

推荐用 [uv](https://docs.astral.sh/uv/)。四种方式按场景选：

```bash
# A. 全局命令行工具（日常最方便，装完任意目录可用 autodl）
uv tool install git+ssh://git@github.com/Jus1mple/autodl_task_submit.git
autodl --version

# B. 装进某个项目的 uv 虚拟环境（当库用 / 项目内脚本调）
uv add git+ssh://git@github.com/Jus1mple/autodl_task_submit.git
# 或手动: uv venv && uv pip install git+ssh://git@github.com/Jus1mple/autodl_task_submit.git

# C. 本地开发（克隆本仓库）
git clone git@github.com:Jus1mple/autodl_task_submit.git && cd autodl_task_submit
uv sync            # 创建 .venv 并安装（含 pytest）
uv run autodl --version
uv run pytest      # 跑测试（4 套桩测试，不触网不花钱，约 8 秒）

# D. 不用 uv（普通 pip）
pip install git+ssh://git@github.com/Jus1mple/autodl_task_submit.git
```

> 仓库当前为私有，git 安装要用 `git+ssh://` 形式（需已配 GitHub SSH key）。发布到 PyPI 后可直接 `uv tool install autodl-task-submit`。
> 依赖极简：requests / paramiko / pyyaml / python-dotenv。可视化大盘在 `web-dashboard` 分支单独维护，核心包零 web 依赖。

## 2. 首次配置

### Token

AutoDL 控制台 → 账号 → 设置 → **开发者 Token**。Token 只放环境变量或 `.env`，**绝不写进 yaml**（防误提交）。

加载优先级（前者覆盖后者）——原则是**项目本地 `.env` 覆盖一切**，在哪个项目目录跑就用哪个项目的 token：

1. 当前目录及其父目录里的 `.env`（最高，覆盖已导出的环境变量）
2. `autodl.yaml` 所在目录的 `.env`
3. 已导出的环境变量 `export AUTODL_TOKEN=...`
4. `~/.autodl/.env`（全局兜底：配一次到处能用，只补缺）

```bash
echo 'AUTODL_TOKEN=你的Token' > .env                 # 项目本地
mkdir -p ~/.autodl && echo 'AUTODL_TOKEN=...' > ~/.autodl/.env   # 或全局兜底
```

其它可用环境变量：`AUTODL_GIT_TOKEN`（私有 https 仓库凭据，见 §6.4）。

### autodl.yaml（可选）

`autodl` 从当前目录向上查找 `autodl.yaml`（或用 `--config` 指定）。没有也能跑（全部用默认值）。完整字段：

```yaml
base_url: https://api.autodl.com
image_uuid: base-image-12be412037   # 创建实例用的镜像（私有镜像填 image-xxxx）
gpu_spec_uuid: v-48g                # GPU 规格（见 autodl.example.yaml 注释里的对照表）
cuda_v_from: 111                    # 最低 CUDA 版本（111 = 11.1）
instance_name: task-runner
expand_disk_gb: 10                  # 系统盘扩容
req_gpu_amount: 1                   # 单机卡数 1–4
data_center_list: []                # 空 = AutoDL 自动调度
regions: [westDC2, westDC3, beijingDC1]   # stock / --select-region 遍历的偏好区域
gpu_stock_name: vGPU-48GB           # 库存匹配的型号名（对应 gpu_spec_uuid）
min_balance_yuan: 10.0              # 余额护栏：低于此值拒绝开机/提交（共享账号下只是兜底）
registry_path: .autodl/registry.db  # 本地台账（SQLite，项目级）
owner: ""                           # 我的标识（共享账号）：新建实例名带 "<owner>/" 前缀，止损/记账只认我的，见 §11
budget:                             # 个人预算与配额（0=不限），见 §11
  daily_yuan: 0
  weekly_yuan: 0
  monthly_yuan: 0
  max_session_hours: 0              # 单次开机保险丝（实例内 shutdown 定时器）
  max_run_hours: 0                  # 任务默认时限（run --max-hours 覆盖）
  max_concurrent: 0                 # 我同时 running 的台数上限
  max_price_per_hour: 0             # 单台小时价上限（元）
  ledger_path: ~/.autodl/ledger.db  # 个人账本（人级，跨项目）

http:                               # HTTP 稳健层
  timeout_connect: 10.0
  timeout_read: 30.0
  retries: 3                        # 只对幂等读接口重试；写接口绝不自动重试
  backoff: 1.5

ssh:
  user: root
  remote_workdir: /root/autodl-tmp  # 数据盘：代码/数据/日志/metrics.json 的约定根
  connect_timeout: 20.0
  connect_retries: 5
  retry_interval: 5.0
  identity_file: ""                 # 留空自动探测 ~/.ssh/id_ed25519 等，没有则生成
  config_alias: autodl              # 写入 ~/.ssh/config 的 Host 别名

git:                                # 实例上的项目仓库（clone / sync / run --sync）
  repo: ""                          # https 或 ssh URL；私有 https 配合 AUTODL_GIT_TOKEN
  branch: main
  dir: ""                           # 实例上的目录；留空 = <remote_workdir>/repo
  depth: 1                          # 浅克隆（规避国内拉大仓库中断）；0 = 全克隆
  turbo: true                       # clone/fetch 前开 AutoDL 学术加速
  patches: ""                       # 本地 patch 目录（见 §6.4），clone/sync 后自动 git apply

env:                                # 实例环境准备（setup / run --setup）
  setup: ""                         # 自定义安装命令（最高优先），在仓库目录下执行
  auto: true                        # 未配 setup 时自动探测 setup.sh > environment.yml > requirements.txt > pyproject.toml
  pip_index: https://pypi.tuna.tsinghua.edu.cn/simple
  academic_turbo: false             # 安装前 source /etc/network_turbo
```

## 3. 核心概念

- **活动实例**：`use` / `up` 登记的当前默认实例。`run` / `logs` / `down` / `clone` 等不指定 `--instance` 时都作用于它。
- **本地台账**（`.autodl/registry.db`，带文件锁的 SQLite）：记录实例、每次运行（run_id / 退出码 / 日志位置 / 起止时间）、指标（标量 + 趋势曲线）。**这是"真相源"：指标在任务完成时即入库，实例关机、甚至释放后数据都还在。**
- **数据盘约定**（`/root/autodl-tmp`）：代码放 `repo/`（git 部署副本），产物写 `outputs/` 等**仓库外**目录，指标写 `metrics.json`。系统盘（`/root`、conda）关机保留、**释放即丢**；数据盘同理。长期保环境用 `snapshot-env` 固化成私有镜像。
- **计费与收尾**：running = 带卡计费；关机 = 仅磁盘计费；释放 = 彻底停止计费但环境丢失。收尾三选一贯穿所有命令：`keep`（不动）/ `power_off`（关机保留复用，下次自动开机）/ `release`（彻底释放）。**任务失败也会执行收尾（止损优先）。**
- **run_id**：每次运行的唯一标识（自动生成 `名字-时间戳-随机后缀`，`--name` 可指定）。前台、后台、批量、setup 都进同一张 runs 表。

## 4. 命令参考

通用参数放在**子命令后面**：`--config 路径`、`--dry-run`（预览破坏性动作）、`--yes/-y`（跳过确认）、`--json`（机器可读，见 §7）。

### 查询（只读，不花钱）

| 命令 | 说明 |
|---|---|
| `autodl balance` | 账户余额 |
| `autodl stock [--region R]` | GPU 库存（区域 × 型号的空闲/总数；默认遍历 `regions`）|
| `autodl status` / `ls [--all]` | **我的**实例 + 计费分类（带卡 / 仅磁盘）+ 我的今日/本周/本月花费与烧钱速率，`*` 标活动实例；`--all` 看共享账号里所有人的 |
| `autodl runs [--limit N]` | 本地台账的运行记录（含指标；只查本地不触网）|
| `autodl cost [--since 7d] [--by day\|instance\|session\|run] [--offline]` | 我的花费报表（§11）|

### 实例生命周期

| 命令 | 说明 |
|---|---|
| `autodl up [--select-region]` | 复用活动实例（关机自动开机）或新建；注入 SSH 公钥免密、写 `~/.ssh/config` 别名、打印 SSH/VSCode/Jupyter 直连信息 |
| `autodl use <uuid>` | 登记一台**已有**实例为活动实例（不创建）|
| `autodl down [--release]` | 关机（`--release` 则释放）活动实例；release 前轮询等真正 shutdown，失败会重试并显著告警 |
| `autodl stop-all [--release] [--all]` | **一键止损**：关停**我的** running 实例（共享账号里别人的一律跳过；`--all` 才动全账号）；单台失败不影响其余 |
| `autodl snapshot-env --name N [--instance U] [--no-wait]` | 实例环境存为私有镜像。⚠️ AutoDL 无删除镜像 API，持续占存储费 |

### 跑任务（核心）

```bash
autodl run (--remote-script 实例上的脚本 | --remote "任意命令" | --script 本地脚本) \
    [--instance U] [--background] [--name RUN_ID] [--select-region] \
    [--down | --release] [--sync [--sync-mode ff|stash|reset]] [--setup] \
    [--pull '<glob,glob>' [--pull-to 目录]] [--max-hours H]
```

- 三种执行方式互斥：`--remote-script`（跑实例上已有脚本，`~` 正确展开）/ `--remote`（任意命令）/ `--script`（上传本地脚本再跑）。
- **前台**（默认）：实时流式回显 stdout/stderr，等结束拿退出码；巨量输出只在内存保留末尾 ~2M（防 OOM），完整日志请改后台。
- **后台**（`--background`）：`setsid` 脱机执行立即返回 run_id，输出落实例日志文件。不能与 `--down/--release` 同用（结束时机未知），跑完自行 `down`。
- `--sync`：跑之前把实例仓库更新到 remote 最新（缺仓库自动 clone、有 patch 自动重放），失败即中止不浪费卡时。
- `--setup`：跑之前按依赖声明准备环境（hash 幂等，没变秒跳）。
- `--pull 'glob1,glob2' --pull-to DIR`：**任务完成自动把结果文件拉回本地**（保留相对结构）。前台内联拉；后台在 `logs` 检测到完成时拉。
- `--max-hours H`：任务时限。实例侧 `timeout` 包住命令，本地进程退出/断网依然生效；到点先 TERM、60 秒后 KILL，退出码 124，不重试。不给则用 `budget.max_run_hours`。
- 每次运行前会清掉实例上的旧 `metrics.json`，防止指标串台。

### 看进度 / 拿结果

| 命令 | 说明 |
|---|---|
| `autodl logs [--run-id R] [--lines N]` | tail 后台任务日志 + 状态 + 退出码 + 指标；完成时自动登记、抓指标、拉产物 |
| `autodl logs -f` / `--follow` | **实时跟随**（tail -f 语义）：tqdm 进度条原样刷新，任务结束自动收尾退出；Ctrl-C 只停跟随不停任务 |
| `autodl kill [--run-id R]` | **终止后台任务**：kill 实例上的整个进程组（setsid 会话），登记 exit=137；默认最近一个 running 的后台任务 |
| `autodl push <本地目录> [子目录]` | rsync 同步本地 → 实例数据盘（默认 `code/`）|
| `autodl pull <远端子路径> [本地目录]` | rsync 实例 → 本地（手动拉任意文件；声明式拉回用 `run --pull`）|

指标为空时 `logs` 会**幂等补抓**：只要实例还能连就重读 `metrics.json`；实例已关则提示开机后可补抓。

### 代码与环境（git 工作流）

| 命令 | 说明 |
|---|---|
| `autodl clone [--repo URL] [--branch B] [--dir D]` | 在实例上克隆项目（幂等，已有则 fetch；浅克隆 + 学术加速；自动应用本地 patch）|
| `autodl sync [--mode ff\|stash\|reset]` | 实例仓库从 remote 更新（本地 `git push` 之后跑）。配了 patch 自动用 reset 语义 + 重放 patch |
| `autodl patch` | 手动重放本地 patch 目录（clone/sync 已自动带；调试 patch 用）|
| `autodl setup [--force] [--background]` | 按仓库依赖声明装环境；内容 hash 记在实例系统盘，未变秒跳、换实例自动失效 |

`sync` 的脏工作区三策略：**ff**（默认，跟踪文件被改就拒绝并回传改动清单）/ **stash**（改动收进 stash 可恢复）/ **reset**（丢弃跟踪文件改动，**未跟踪的产物保留**）。

### 批量与守护

```bash
autodl batch --file jobs.yaml [--max-parallel N] [--on-finish release|power_off|keep] \
             [--retries N] [--select-region] [--no-resume] [--pull GLOB] [--pull-to DIR]
```

`jobs.yaml`：每个任务 `id` + 三选一（`remote_script`/`remote`/`script`）+ 可选 `pull`（各自的结果文件 glob，拉到 `<pull-to>/<job_id>/`，并行互不覆盖）。至多 N 台实例并行、实例复用；中断重跑自动 **resume**（跳过已成功）。

| 命令 | 说明 |
|---|---|
| `autodl idle-guard [--idle-minutes 15] [--once]` | 空闲自动关机看门狗（GPU 利用率 + 显存双判；实例上 `touch /root/.autodl_keepalive` 豁免；SSH 不通绝不误判为空闲）|
| `autodl balance-watch --warn 50 --stop 10 [--stop-mode mine\|active\|stop_all]` | 账户余额预警/急停；默认 `mine` 只关我的实例 |
| `autodl budget-guard [--interval 300] [--once]` | **个人预算守护**：今日/本周/本月花费触顶 → 关停我的全部 running；某台连续开机超 `max_session_hours` → 只关那台。绝不碰别人的（§11）|

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 其它错误（API/IO）|
| 2 | 用法/配置错误 |
| 3 | 余额低于 `min_balance_yuan` |
| 5 | SSH 不可达 |
| 7 | 个人预算/配额触顶（`budget.*`）|
| 6 | 远端任务非零退出 |
| 130 | Ctrl-C |

## 5. 典型工作流

### 5.1 最短路径：跑一个任务

```bash
autodl use pro-xxxxxxxx                                   # 一次性登记已有实例
autodl run --remote-script '~/proj/run.sh' --down         # 开机→实时回显→关机
```

### 5.2 长训练：后台 + 跟随 + 自动拉产物

```bash
autodl run --script train.sh --background --name exp1 \
    --pull 'output/checkpoint-*/,metrics.json' --pull-to ./results
autodl logs -f                        # 实时盯进度条；结束自动抓指标+拉 checkpoint
autodl runs --json | jq '.runs[0].metrics'
autodl down --yes
```

### 5.3 迭代循环：本地改码 → push → 实例更新 → 重跑

```yaml
# autodl.yaml
git: { repo: git@github.com:you/proj.git, branch: main }
```
```bash
autodl clone && autodl setup                              # 一次性
autodl run --sync --setup --remote-script '~/autodl-tmp/repo/train.sh' --background
#          ^^^^^^^^^^^^^^ 每次改完 git push 后：更新代码+按需重装环境+跑，一条命令
```

### 5.4 跑别人的仓库（要适配、但绝不 push 上游）

把适配补丁存**你本地**（`git diff` 生成，`NN-name.patch` 控顺序）：

```yaml
git:
  repo: https://github.com/someone/their-project.git
  patches: patches/          # clone/sync 后自动 git apply
```

patch 只改实例工作区、不进 git 历史 → **物理上不可能被推回上游**。`sync` 时先 reset 回干净上游、拉最新、再重放 patch——永不累积。patch 冲突（上游改了邻近行）会明确报出而非强应用。私有 https 仓库：本地 `export AUTODL_GIT_TOKEN=...`，凭据写实例 `~/.git-credentials`（0600），不进命令行。

完整实战（含 5090 硬件适配、真实数据、训练出 IoU 0.835 的模型）见 [examples/vlm-r1-refcoco/](examples/vlm-r1-refcoco/)。

### 5.5 批量超参搜索

```yaml
# jobs.yaml
- id: lr_1e-3
  remote: "cd ~/autodl-tmp/repo && LR=1e-3 bash train.sh"
  pull: 'metrics.json,output/*.pt'
- id: lr_1e-4
  remote: "cd ~/autodl-tmp/repo && LR=1e-4 bash train.sh"
  pull: 'metrics.json,output/*.pt'
```
```bash
autodl batch --file jobs.yaml --max-parallel 3 --on-finish release --pull-to ./results
# 每个任务的产物落 ./results/<job_id>/；中断重跑 resume
```

### 5.6 无人值守省钱

```bash
autodl idle-guard --idle-minutes 15 &          # 连续空闲 15 分钟自动关机
autodl balance-watch --warn 50 --stop 10 &     # 余额告警/急停
```

## 6. `--json` 数据契约

所有命令支持 `--json`：**stdout 只输出一行 JSON，进度日志走 stderr**——可以直接 `| jq`、被脚本/agent 消费。主要返回结构：

| 命令 | 返回字段 |
|---|---|
| `run`（前台） | `run_id, exit_code, stdout, stderr, instance, metrics, artifacts` |
| `run --background` | `run_id, pid, log, exit_file, instance` |
| `logs` | `run_id, status, exit_code, instance, log, note, metrics, artifacts` |
| `runs` | `runs: [{run_id, status, exit_code, instance, experiment_id, tag, started_at, metrics}]` |
| `status` | `balance_yuan, instances: [{instance_uuid, name, status, billing, region, active, mine}], usage: {today, week, month, disk_month, burn_rate, running, sessions_open}` |
| `cost` | `rows: [...], total, disk, since, until, usage`（rows 字段随 `--by` 变化）|
| `kill` | `run_id, killed, state, note` |
| `clone` | `dir, branch, head, created, credentials, patches` |
| `sync` | `dir, branch, mode, old, new, updated, dirty, blocked, error, message, patches` |
| `setup` | `skipped, source, command, exit_code`（或后台句柄）|
| `patch` | `applied, skipped, failed, total` |

## 7. 指标（metrics）规范

任务把结果写到 `<数据盘>/metrics.json`，运行结束自动抓取入库：

```json
{
  "final_acc": 0.99,                    // 顶层标量 = 最终指标
  "loss": 0.012,
  "series": [                           // 可选：趋势曲线（步级）
    {"step": 1, "loss": 0.5, "reward": 0.1},
    {"step": 2, "loss": 0.4, "reward": 0.3}
  ]
}
```

也可以什么都不写、直接在日志里打印 `ASR: 0.75` 这类行——通过 `metrics_spec`（Python API / 实验定义）声明正则抓取；前台任务的正则直接作用于 stdout。查询：`autodl logs/runs --json`，或 Python `ctx.reg.get_metrics(run_id)` / `get_metric_series(run_id)`。

## 8. Python API

CLI 的全部能力都有库接口：

```python
from autodl import connect, submit

ctx = connect()                        # 读 autodl.yaml + .env；等价 load_config+require_token+Context

# 一站式提交（护栏→起实例→执行→抓指标→按需收尾）
res = submit(ctx,
             mode="remote_script",     # remote_script | remote | script | script_text
             value="~/proj/run.sh",
             teardown="power_off",     # keep | power_off | release（成败都执行）
             artifacts={"patterns": "output/*,metrics.json", "local_dir": "./results"},
             stream=lambda s: print(s, end=""))
print(res["exit_code"], ctx.reg.get_metrics(res["run_id"]))

# 后台 + 稍后对账
meta = submit(ctx, mode="remote", value="bash train.sh", background=True)
from autodl import tasks
info = tasks.refresh_run(ctx, ctx.reg.get_run(meta["run_id"]))   # 探活/tail/完成登记/抓指标/拉产物
```

底层件：`ctx.api`（HTTP 客户端）、`ctx.ssh`（SSH 层）、`ctx.reg`（台账）、`ctx.ensure_instance()` / `ensure_specific()` / `finish_instance()`（实例编排）、`autodl.gitsync` / `autodl.envsetup`（git 与环境）。

## 9. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `未找到 AUTODL_TOKEN` | 按 §2 配置；注意 `.env` 在**当前目录或其父目录**才会被找到 |
| `SSH 不可达 [instance_not_running]` | 实例没开机：`autodl up` 或 `run --instance` 会自动开机 |
| `SSH 不可达 [port_not_ready]` | 刚开机 sshd 未就绪，稍等重试（连接层已带重试）|
| `clone/fetch 失败: GnuTLS recv error` | 国内拉 GitHub 大仓库中断。默认浅克隆+学术加速已缓解；仍失败可重试或 `git.depth: 1` 确认开启 |
| `sync 已拒绝更新（mode=ff）` | 实例上跟踪文件被改。看清单后选 `--mode stash`（保留）或 `--mode reset`（丢弃，产物不受影响）|
| `patch 冲突` | 上游改了补丁邻近行：本地重新 `git diff` 生成该 patch。patch 目标行留足上下文更稳 |
| `CUDA out of memory` | 真实图片/长序列显存大：调小 batch、`num_generations`，或用 `--max_pixels` 限制视觉 token |
| 指标为空 | `logs` 会自动补抓（实例在线时）；实例已关则开机后再 `logs --run-id`。metrics.json 必须是合法 JSON |
| `/root/autodl-pub` 不存在 | 公共数据集是尽力挂载，偶尔缺失：换实例重开机，或脚本里做好回退 |
| 台账里 run 永远 running | 进程被 pkill/实例硬重启且未对账：下次实例在线时任意 `logs` 会登记为 failed |

## 10. 成本与安全设计（内建，无需配置）

- **写接口绝不自动重试**（create/power_on/power_off/release），避免重复开机重复扣费；只读接口指数退避重试。
- **余额护栏**：低于 `min_balance_yuan` 拒绝开机/提交（退出码 3）。共享账号下这只是兜底，按人限额见 §11。
- **止损只关我的**：`stop-all` / `balance-watch` / `budget-guard` 默认只作用于我的实例（owner 前缀或本地登记过）；碰别人的必须显式 `--all` / `--stop-mode stop_all`。
- **收尾必达**：任务失败也执行 `--down/--release`；release 前轮询等真正 shutdown、业务失败码算失败并重试、最终失败**显著告警**绝不静默泄漏计费。
- **凭据安全**：`root_password` 只在内存用一次不进命令行/日志；首连注入 SSH 公钥走免密；git token 走凭据文件（0600）。
- **误关保护**：idle-guard 用利用率+显存双判、支持 keepalive 豁免、SSH 不通绝不当作空闲。
- **危险操作确认**：`down`/`stop-all`/`snapshot-env` 需确认（`--yes` 跳过，`--dry-run` 预览）。

## 11. 共享账号：只算我的账、只限我自己

账号被很多人共用时，余额是大家的，余额差分算不出"我花了多少"；AutoDL 也没有账单 API。`autodl` 改用**归属式记账**：

- **口径**：一条 session = 我让某台实例处于 running 的一段时间，费用 = 该实例小时价（snapshot 的 `payg_price`，折后价）× 秒数 ÷ 3600。这与官方"按秒计费、时长 = 关机时间 − 开机时间"一致。关机不释放的数据盘另按 ¥0.01/GB/天 估算。
- **记账时机**：本工具每次开机/创建/关机/释放自动开关 session；每次 `status` / `cost` / 预检前都用平台的 `started_at` / `stopped_at` **对账**——别人从控制台开关了我的机也能追平；实例从账号里消失则结清并标记释放。
- **"我的"边界**：`owner: kedong` 后新建实例名为 `kedong/task-runner`。对账按前缀认领，换电脑、账本丢了也能重建；`use` 登记过的实例同样算我的。
- **账本**是人级的（`~/.autodl/ledger.db`，跨项目），run/指标台账仍是项目级的。`cost --by run` 会把两者按实例和时间对上。

四层限制，各管一件事：

| 层 | 配置 / 参数 | 机制 |
|---|---|---|
| 单次任务 | `run --max-hours H` / `budget.max_run_hours` | 实例侧 `timeout` 包住命令，本地进程死了也生效；超时 exit=124 |
| 单次开机 | `budget.max_session_hours` | 开机时在实例内挂 `sleep N; shutdown` 保险丝（AutoDL：实例内 `shutdown` 即关机），笔记本合盖/断网也自关 |
| 额度 | `budget.daily/weekly/monthly_yuan` | 开机/创建/提交/batch 前预检，触顶退出码 7；`budget-guard` 守护触顶关停我的实例 |
| 资源 | `budget.max_concurrent` / `max_price_per_hour` | 创建/开机前拦；新建实例价格只有就绪后才知道，超价**立即释放**并报错 |

```bash
autodl cost                          # 本月按天
autodl cost --since 7d --by instance # 最近 7 天按实例
autodl cost --by session             # 每段开机：谁开的(source)、哪个项目、多少钱
autodl cost --by run                 # 每个 run 的费用（run 之间的空档 = 闲置开销，在 session 里看）
autodl budget-guard --interval 300 & # 守护
```

精度边界（诚实说明）：这是**归属估算**，不是发票。单价按开机时快照，平台改价不追；代金券/折扣变动看不到；别人用我的实例也会算到我头上（所以要贴 owner 前缀）；`payg_price` 单位文档未明示，按同族接口的"元×1000"处理，首次开机时请对照控制台核一次。月底用控制台账单按我的实例 uuid 筛一遍对账即可。
