# autodl 傻瓜手册

照着抄命令就能用。只讲最常用的做法；想知道全部参数看 [USAGE.md](USAGE.md)。

**先记住一句话：机器开着就在花钱。** 跑完一定关机，花了多少用 `autodl cost` 看。

---

## 第一次使用（只做一次）

### 1. 装工具

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+ssh://git@github.com/Jus1mple/autodl_task_submit.git
autodl --version
```

仓库是私有的，第二条命令要求你的 GitHub 账号已经配好 SSH 密钥。以后升级把第二条命令加 `--force` 再跑一次。

### 2. 放 Token

AutoDL 控制台 → 账号 → 设置 → 开发者 Token，复制下来：

```bash
mkdir -p ~/.autodl
echo 'AUTODL_TOKEN=粘贴你的Token' > ~/.autodl/.env
```

Token 只放这里，不要写进代码、不要发到聊天里。

### 3. 在你的项目目录里建 `autodl.yaml`

```yaml
owner: 你的名字拼音          # 必填。账号是大家共用的，靠它区分哪些机器是你的
gpu_spec_uuid: v-48g-350w    # 要租的卡，见下表
budget:
  daily_yuan: 50             # 每天最多花多少钱，超了自动拒绝开机
  max_run_hours: 6           # 单个任务最多跑几小时，到点自动停
  max_session_hours: 12      # 机器开着最多几小时，到点自动关机（断网也有效）
```

常用卡：

| 填什么 | 是什么卡 |
|---|---|
| `v-48g-350w` | 3090（实测 ¥1.78/小时） |
| `4090D` | 4090D |
| `v-48g` | 4090 48G 显存 |
| `5090-p` | 5090 32G |
| `h800` | H800 80G |

检查一下：

```bash
autodl status
```

能看到余额和"我的花费"一行，就配好了。

---

## 每次用：五条命令

按顺序来，这就是一次完整的使用。

**① 看一眼现在的状态**（免费）

```bash
autodl status
```

`running` 表示正在花钱，`shutdown` 表示已关机只收一点磁盘费。只会列出你自己的机器。

**② 跑任务**

```bash
autodl run --script train.sh --background --name exp1 --pull 'output/,metrics.json'
```

- `--script train.sh`：把你电脑上的脚本传上去跑。
- `--background`：后台跑，命令马上返回，关掉终端也不影响。
- `--name exp1`：给这次任务起个名字，后面查进度用。
- `--pull ...`：跑完自动把这些文件下载到本地 `./results`。

没有机器会自动新开一台，有关机的机器会自动开机。

**③ 看进度**

```bash
autodl logs -f
```

实时滚动日志，跑完自动下载结果。按 `Ctrl-C` 只是不看了，任务照样在跑。

**④ 关机**

```bash
autodl down
```

关机后环境和文件都保留，下次 `run` 自动开机接着用。

**⑤ 看花了多少钱**

```bash
autodl cost
```

---

## 常见场景直接抄

**试一下能不能用**（开机 → 跑 → 自动关机）

```bash
autodl run --remote 'nvidia-smi' --down
```

**短任务，跑完就关**

```bash
autodl run --script eval.sh --down
```

**不知道一条命令会干什么，先预览**（什么都不会执行）

```bash
autodl run --script train.sh --background --dry-run
```

**任务跑飞了，停掉它**

```bash
autodl kill --run-id exp1
```

**查某个任务的结果和指标**

```bash
autodl logs --run-id exp1
autodl runs
```

**手动上传 / 下载文件**（机器上的工作目录是 `/root/autodl-tmp`）

```bash
autodl push ./data data
autodl pull output ./results
```

**用 git 仓库里的代码跑**：在 `autodl.yaml` 里加

```yaml
git:
  repo: https://github.com/你/你的项目.git
```

然后：

```bash
autodl clone
autodl setup
autodl run --remote-script '~/autodl-tmp/repo/train.sh' --background --name exp2
```

本地改完代码 `git push` 之后，一条命令更新代码、按需重装依赖、再跑：

```bash
autodl run --sync --setup --remote-script '~/autodl-tmp/repo/train.sh' --background --name exp3
```

**看这周钱花在哪几台机器上**

```bash
autodl cost --since 7d --by instance
autodl cost --by run
```

**让它自己盯着预算**（超预算自动关掉你的机器）

```bash
nohup autodl budget-guard > budget-guard.log 2>&1 &
```

---

## 省钱红线

- **后台任务不会自动关机。** 跑完要么 `autodl down`，要么靠 `max_session_hours` 到点自动关。
- **不要加 `--all`。** `autodl stop-all` 默认只关你的机器；加了 `--all` 会把别人正在跑的训练一起关掉。
- **`--release` 是删除。** `autodl down --release` 会把机器连同上面装好的环境和文件一起删掉，只对确定不要的机器用。平时用 `autodl down`。
- **不确定就加 `--dry-run`。** `run`、`down`、`stop-all`、`kill`、`batch` 都支持，只预览不执行。
- **看到退出码 7 不要去调高预算。** 那是你给自己设的上限，先看 `autodl cost` 钱花哪了。

---

## 出错了看这里

命令结束时的退出码：

| 退出码 | 意思 | 怎么办 |
|---|---|---|
| 0 | 成功 | |
| 2 | 命令写错了 | 看提示，或加 `--help` |
| 3 | 账户余额太低 | 联系管理账号的人充值 |
| 5 | 连不上机器 | 刚开机时等一分钟重试 |
| 6 | 你的脚本自己报错了 | `autodl logs --run-id 名字` 看日志 |
| 7 | 超过你设的预算 | `autodl cost` 看钱花哪了 |

任务结果里的 `exit_code`：

| exit_code | 意思 |
|---|---|
| 124 | 超过 `max_run_hours` / `--max-hours` 时限，被自动停了 |
| 137 | 被 `autodl kill` 停了，或者内存爆了 |

常见现象：

| 现象 | 原因和办法 |
|---|---|
| 机器自己关机了 | `max_session_hours` 到点了，正常。文件都在，再 `run` 会自动开机 |
| `未找到 AUTODL_TOKEN` | 第一次使用第 2 步没做，或 Token 粘错了 |
| `CUDA out of memory` | 显存不够：调小 batch size，或换大显存的卡 |
| `No space left on device` | 系统盘满了，多半是模型缓存。大文件放 `/root/autodl-tmp` |
| 拉 GitHub 代码失败 | 国内网络抖动，重试一次通常就好 |
| 指标是空的 | 再跑一次 `autodl logs --run-id 名字`，会自动补抓 |
| `status --all` 里一台都没有，余额却在减少 | 别人在控制台开的机器这里看不到，不是你的问题 |

---

## 速查卡

```text
autodl status                 现在什么情况、花了多少
autodl run --script X.sh --background --name N    后台跑
autodl run --remote '命令' --down                  跑完就关
autodl logs -f                实时看进度
autodl logs --run-id N        看某个任务
autodl kill --run-id N        停掉任务
autodl down                   关机（保留环境）
autodl cost                   花费报表
加 --dry-run                  只预览不执行
加 --json                     输出给脚本用
```
