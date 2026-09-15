# autodl 命令与配置参考（浓缩版）

完整手册在仓库 `USAGE.md`；此处为高频速查。

## 命令总表

通用参数放子命令后：`--config` `--dry-run` `--yes/-y` `--json`

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `balance` | 余额 | |
| `stock` | GPU 库存（区域×型号 空闲/总数）| `--region` |
| `status` / `ls` | 我的实例+计费分类+我的花费(usage)，`*`=活动实例 | `--all` 看全账号 |
| `cost` | 我的花费报表（只算我开机的时段）| `--since 7d` `--by day\|instance\|session\|run` `--offline` |
| `runs` | 本地台账运行记录（含指标，不触网）| `--limit N` |
| `up` | 复用/新建实例+SSH免密+打印直连信息 | `--select-region` |
| `use <uuid>` | 登记已有实例为活动实例（不创建）| |
| `down` | 关机活动实例（或指定的一台）| `--instance U` `--release`(彻底释放,慎) |
| `stop-all` | 止损：关停**我的** running | `--release` `--all`(全账号,慎) |
| `run` | 执行任务 | 见下 |
| `logs` | tail 日志+状态+指标；完成时自动对账 | `--run-id` `--lines` `-f/--follow` |
| `kill` | 终止后台任务（kill 进程组，exit=137）| `--run-id` |
| `clone` | 实例上克隆仓库（幂等+patch）| `--repo` `--branch` `--dir` |
| `sync` | 仓库更新到 remote（+重放 patch）| `--mode ff\|stash\|reset` |
| `patch` | 手动重放本地补丁 | `--dir` |
| `setup` | 按依赖声明装环境（hash 幂等）| `--force` `--background` |
| `push/pull` | rsync 本地↔实例数据盘 | |
| `batch` | 多实例并行批量 | `--file` `--max-parallel` `--on-finish` `--retries` `--pull` |
| `snapshot-env` | 实例→私有镜像（占存储费,无删除API,慎）| `--name` |
| `idle-guard` | 空闲自动关机看门狗 | `--idle-minutes` `--once` |
| `balance-watch` | 账户余额预警/急停（默认只关我的）| `--warn` `--stop` `--stop-mode mine\|active\|stop_all` |
| `budget-guard` | 个人预算/开机时长守护（只关我的）| `--interval` `--once` |

## run 详解

```
autodl run (--remote-script 实例上脚本 | --remote "命令" | --script 本地脚本) \
  [--instance U] [--background] [--name RUN_ID] [--down|--release] \
  [--sync [--sync-mode ff|stash|reset]] [--setup] \
  [--pull 'glob1,glob2' --pull-to DIR] [--max-hours H] [--select-region] [--json]
```

- 三种执行方式互斥；`--remote-script` 的 `~` 会正确展开。
- 前台：流式回显，`--down/--release` 结束后收尾（失败也收尾）。
- 后台：立即返回 run_id；与 `--down/--release` 互斥。
- `--sync`：先更新仓库（缺则 clone）；`--setup`：先按 hash 装环境；失败即中止不浪费卡时。
- `--pull`：完成时把 glob 匹配的文件拉回本地（后台任务在 logs 检测到完成时触发）。
- `--max-hours`：实例侧 `timeout` 时限，本地断网也生效；超时 exit=124 不重试。默认 `budget.max_run_hours`。
- 每次运行前自动清实例上旧 `metrics.json`，防指标串台。

## autodl.yaml 字段

```yaml
image_uuid: base-image-12be412037   # 或私有镜像 image-xxxx
gpu_spec_uuid: v-48g                # v-48g=vGPU-48GB(4090) / 4090D / h800 / 5090-p ...
cuda_v_from: 111
instance_name: task-runner
expand_disk_gb: 10
req_gpu_amount: 1                   # 1-4
regions: [westDC2, westDC3, beijingDC1]
gpu_stock_name: vGPU-48GB
min_balance_yuan: 10.0              # 账户余额护栏（共享账号下只是兜底）
registry_path: .autodl/registry.db
owner: ""                           # 我的标识：实例名前缀 "<owner>/"，止损/记账只认我的
budget:                             # 个人限额，0=不限；触顶退出码 7
  daily_yuan: 0                     # 今日/本周/本月我的花费上限（weekly_yuan / monthly_yuan 同理）
  max_session_hours: 0              # 单次开机保险丝（实例内 shutdown 定时器）
  max_run_hours: 0                  # 任务默认时限
  max_concurrent: 0                 # 我同时 running 台数
  max_price_per_hour: 0             # 单台小时价上限（元）
  ledger_path: ~/.autodl/ledger.db  # 个人账本（人级）
ssh:
  remote_workdir: /root/autodl-tmp  # 数据盘约定根
git:
  repo: ""                          # https/ssh；私有https配 AUTODL_GIT_TOKEN
  branch: main
  dir: ""                           # 默认 <workdir>/repo
  depth: 1                          # 浅克隆
  turbo: true                       # clone/fetch 走学术加速
  patches: ""                       # 本地补丁目录, clone/sync 自动 git apply
env:
  setup: ""                         # 自定义安装命令(最高优先)
  auto: true                        # 探测 setup.sh>environment.yml>requirements.txt>pyproject
  pip_index: https://pypi.tuna.tsinghua.edu.cn/simple
  academic_turbo: false
```

Token 优先级：项目目录(及父目录) `.env` > autodl.yaml 旁 `.env` > 环境变量 > `~/.autodl/.env`。

## metrics.json 规范

任务写到 `<数据盘>/metrics.json`，完成自动入库：

```json
{"final_acc": 0.99,
 "series": [{"step": 1, "loss": 0.5}, {"step": 2, "loss": 0.4}]}
```

顶层标量=最终指标；`series`=趋势曲线。日志正则抓取（`ASR: 0.75` 这类行）通过 Python API 的 `metrics_spec` 声明。

## Python API

```python
from autodl import connect, submit
ctx = connect()                       # 读 autodl.yaml + .env
res = submit(ctx, mode="remote_script",  # remote_script|remote|script|script_text
             value="~/proj/run.sh", teardown="power_off",   # keep|power_off|release
             background=False,
             artifacts={"patterns": "output/*", "local_dir": "./results"},
             max_hours=6,                          # 任务时限（实例侧 timeout）
             stream=lambda s: print(s, end=""))
ctx.reg.get_metrics(res["run_id"]); ctx.reg.get_metric_series(res["run_id"])

from autodl import tasks
tasks.refresh_run(ctx, ctx.reg.get_run(rid))   # 探活/对账/抓指标/拉产物
```

底层：`ctx.api`(HTTP) `ctx.ssh`(SSH) `ctx.reg`(台账) `ctx.ledger`(个人账本) `ctx.ensure_instance/ensure_specific/finish_instance`，`autodl.gitsync`、`autodl.envsetup`、`autodl.cost`（`usage/report/check_budget/reconcile`）。
