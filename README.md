# autodl-task-submit

在 AutoDL 开发者 API 之上的可靠命令行工具：开机/复用实例、传代码/数据、跑任务（本地脚本或实例上已有的脚本）、拿结果、按需关机/释放。

## 安装与配置

```bash
uv sync                       # 安装依赖
cp autodl.example.yaml autodl.yaml   # 可选：按需改实例规格/区域/预算/SSH
echo 'AUTODL_TOKEN=你的Token' > .env # Token 只放 .env，不进 yaml（已被 .gitignore）
```

Token 在 AutoDL 控制台 → 账号 → 设置 → 开发者 Token 获取。运行入口：`uv run python -m autodl <命令>`。

## 命令一览

| 命令 | 作用 |
|---|---|
| `balance` | 查余额 |
| `stock [--region R]` | 查 GPU 库存（默认遍历偏好区域）|
| `status` / `ls` | 列出所有实例，标注"带卡计费 / 仅磁盘计费" |
| `stop-all [--release]` | 一键止损：关停所有 running 实例（`--release` 则彻底释放）|
| `up [--select-region]` | 拉起/复用实例，注入公钥免密，写 `~/.ssh/config`，打印 SSH/VSCode/Jupyter 直连信息 |
| `use <instance_uuid>` | 登记一台**已有**实例为当前活动实例（不创建）|
| `run ...` | 在实例上执行任务（见下）|
| `logs [--run-id R]` | 查看后台任务日志（tail + 退出码）|
| `push <本地目录> [子目录]` | rsync 同步本地到实例数据盘 |
| `pull <远端子路径> [本地目录]` | rsync 从实例拉回产物 |
| `down [--release]` | 关机（或释放）当前活动实例 |
| `snapshot-env --name N [--instance U]` | 把实例环境存为私有镜像（持续占存储费，无删除 API，慎用）|
| `idle-guard [--once] [--idle-minutes N]` | 空闲自动关机看门狗（GPU 利用率+显存双判，keepalive 文件可豁免）|
| `balance-watch --warn Y --stop Y` | 余额预警/急停守护（低于急停线自动关停）|
| `batch --file jobs.yaml [--max-parallel N]` | 批量并行调度：多实例跑多任务，记 run 台账、支持 resume |

通用参数放在命令**后面**：`--config`、`--dry-run`、`--yes/-y`、`--json`。

## 场景：实例已配置好，直接跑里面的脚本

实例里项目代码与环境都已就绪，有入口 `submission_main.py` 和执行脚本 `submit_exec.sh`（负责激活环境等）：

```bash
# 一次性登记你的实例（之后命令默认用它）
uv run python -m autodl use pro-xxxxxxxx

# 跑实例上已有的脚本，前台等结果，跑完关机（保留实例，下次再用）
uv run python -m autodl run --instance pro-xxxxxxxx \
    --remote-script ~/proj/submit_exec.sh --down
```

- `--remote-script` 跑的是**实例上已存在**的脚本，不上传；stdout/stderr/退出码会原样返回。
- `--down` 跑完只关机（停 GPU 计费、保留磁盘与环境）；**下次再 `run --instance` 会自动开机复用**。
- 想彻底停止计费用 `--release`（连磁盘费一起停，但环境会随实例释放而丢失，必要时先 `image/save`）。
- 长任务（数小时）加 `--background`，立即返回 `run_id`，之后用 `autodl logs --run-id <id>` 看进度与退出码。

`run` 的三种执行方式（互斥）：
- `--remote-script <实例上的路径>`：跑实例已有脚本（你的主要用法）
- `--remote "<命令>"`：直接执行任意远端命令
- `--script <本地路径>`：上传本地 bash 脚本再执行

## 省钱守护（无人值守）

```bash
# 空闲自动关机：连续 15 分钟 GPU 利用率<5% 且显存<500MiB 就关机
uv run python -m autodl idle-guard --instance pro-xxxx --idle-minutes 15
#   交互调试时想豁免：在实例上 touch /root/.autodl_keepalive 即不会被关
#   --once 只探一次（适合挂 cron 巡检）

# 余额守护：低于 50 元通知，低于 10 元自动关停所有 running 实例
uv run python -m autodl balance-watch --warn 50 --stop 10 --interval 300
```
两者都靠客户端轮询实现（AutoDL 无空闲关机开关、无 webhook）。SSH 连不上时看门狗会回退查实例状态，**绝不把"网络不通"误判成"空闲"而误关**。

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
uv run python -m autodl batch --file jobs.yaml --max-parallel 3 --on-finish release
```
- 至多 `--max-parallel` 台实例并行，一台跑完自动取下一个任务（实例复用）。
- 结果记入 `.autodl/registry.db`；中断后重跑会 **resume**（跳过已成功的任务）。
- `--on-finish release|power_off|keep` 控制收尾；`--retries N` 单任务失败重试；`--select-region` 按库存选区。

## 环境固化

```bash
uv run python -m autodl snapshot-env --name myenv-v1 --instance pro-xxxx
```
把配好的实例存成私有镜像并轮询到 `finished`，之后在 `autodl.yaml` 设 `image_uuid` 即可秒级复现环境。⚠️ AutoDL **没有删除镜像的 API**，镜像会持续占存储费，只能去控制台删——确认需要再存。

## 可视化实验大盘（Web）

```bash
pip install 'autodl-task-submit[web]'    # 或本地开发：uv sync --extra web
uv run autodl web                         # 默认 http://127.0.0.1:8848
```

四个页面:
- **概览**:余额、实例/实验数量、任务与实例的状态分布图。
- **实例**:列表 + 开机/关机/释放;「新建/开机实例」后台进行、前端轮询(浏览器不卡)。
- **提交 & 实验**:选实例 → 跑实例上已有脚本(`remote_script`)/任意命令/上传脚本,**后台运行**;每个实验记录状态、退出码、用时、配置与指标,点开看实时日志。
- **大盘**:跨实验的「指标 × 实验」对比表 + 按某指标排序的柱状图。

**指标怎么记**:实验在数据盘写一个 `metrics.json`(如 `{"acc":0.97,"loss":0.12}`),任务完成时大盘**自动抓取入库**;也可在实验详情里手动填 JSON,或直接 `POST /api/runs/<run_id>/metrics`。后端是 FastAPI,所有能力都复用 CLI 那套实例/SSH/台账逻辑;前端是零构建的单页(原生 JS + Chart.js)。

## 设计要点

- **HTTP 稳健层**：超时 + 对只读接口指数退避重试；`create`/`power_*`/`release` 等写接口**绝不自动重试**，避免重复开机/重复扣费。
- **SSH 连接层**：失败按根因分类（实例已关机 / 跳板不通 / 端口未就绪 / 认证失败），不会把"网络不通"误判成"实例已关机"。
- **凭据安全**：首连后注入本地 SSH 公钥走免密，`root_password` 只在内存用一次，不进命令行/日志。
- **本地台账**：`.autodl/registry.db`（带文件锁的 SQLite）记录实例与任务，替代旧的单值 `.instance_uuid`。
- **GET 接口走 query string**：AutoDL 网关会丢弃 GET 的 body（这是早期踩过的坑）。

> 旧的一体脚本 `submit.py` 仍可用（`uv run python submit.py`），新功能请用 `autodl` 包。
