# autodl-task-submit

AutoDL GPU 云的 **Python 库 + 命令行工具**：开机/复用实例、传代码/数据、跑任务（前台流式 / 后台脱机 / 批量并行）、抓指标拿结果、按需关机/释放。

用法对标 `huggingface-hub`：`pip install` 装完即有 `autodl` 命令，跑任务一条命令、`--json` 拿结构化数据；同一套能力也可以当库 `import autodl` 直接调。

> 可视化大盘（FastAPI + 网页）**不在核心包里**，在 [`web-dashboard`](../../tree/web-dashboard) 分支单独维护——核心包保持零 web 依赖（只有 requests / paramiko / pyyaml / dotenv）。

## 安装与配置

```bash
pip install autodl-task-submit        # 或本地开发：uv sync
echo 'AUTODL_TOKEN=你的Token' > .env  # Token 只放环境变量/.env，不进 yaml（已被 .gitignore）
cp autodl.example.yaml autodl.yaml    # 可选：改实例规格/区域/预算/SSH/git 仓库
```

Token 在 AutoDL 控制台 → 账号 → 设置 → 开发者 Token 获取。**加载优先级**（前者覆盖后者）：

1. 已导出的环境变量：`AUTODL_TOKEN=... autodl run ...`
2. 当前目录及其父目录里的 `.env`（项目本地）
3. `autodl.yaml` 所在目录的 `.env`（`--config` 指到别处时跟着配置走）
4. `~/.autodl/.env`（全局兜底：配一次，任意目录都能用）

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
autodl run --remote-script '~/autodl-tmp/repo/train.sh' --background
autodl logs --json | jq '{status, exit_code, metrics}'   # ② 回传结果，本地解析

# ③ 根据结果本地改代码 → git commit && git push，然后：
autodl run --sync --remote-script '~/autodl-tmp/repo/train.sh' --background
#          ^^^^^^ 跑之前自动把实例仓库更新到 origin/main（缺仓库则自动 clone）
autodl sync --json                       # 也可以单独更新不跑任务
```

**实例上的数据变了怎么更新**（`--mode` / `--sync-mode` 三种策略）：

| 策略 | 行为 | 适用 |
|---|---|---|
| `ff`（默认） | 跟踪文件被改过就**拒绝更新**，回传改动清单，绝不丢数据 | 日常；配合"产物写仓库外"的约定永远干净快进 |
| `stash` | 改动（含未跟踪文件）收进 `git stash` 再快进，实例上可恢复 | 在实例上临时改过代码调试，想保留 |
| `reset` | `reset --hard` 到远端；丢弃跟踪文件改动，**未跟踪文件（产物/数据）保留** | 远端为准，实例上的手改不要了 |

**约定**：训练产物/数据集写到仓库外（如 `~/autodl-tmp/outputs/`、指标写 `~/autodl-tmp/metrics.json`），仓库目录保持只读部署——这样 `sync` 永远是干净快进，产物也不会被任何策略碰到。产物用 `autodl pull outputs ./results` 拉回本地。

私有仓库（https）：本地设 `AUTODL_GIT_TOKEN`，`clone` 时会把凭据写进实例的 `~/.git-credentials`（0600），token 不进命令行和日志。

## 批量并行（超参搜索等）

任务清单 `jobs.yaml`（每个任务三选一：`remote_script` / `remote` / `script`）：

```yaml
- id: lr_1e-3
  remote_script: ~/proj/run.sh        # 跑实例上已有脚本（可在脚本里读环境变量区分配置）
- id: lr_1e-4
  remote: "cd ~/proj && LR=1e-4 bash run.sh"
- id: baseline
  script: ./local_task.sh             # 上传本地脚本再跑
```

```bash
autodl batch --file jobs.yaml --max-parallel 3 --on-finish release
```
- 至多 `--max-parallel` 台实例并行，一台跑完自动取下一个任务（实例复用）。
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
