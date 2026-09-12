---
name: autodl
description: 用 autodl CLI 在 AutoDL GPU 云上跑任务：开机/复用实例、克隆仓库+打本地补丁、装环境、跑训练（前台/后台/批量）、实时看进度、自动抓指标、拉回结果文件、用完关机止损。只要用户提到 AutoDL、云 GPU/GPU 云、租卡跑训练/实验/微调、开关 GPU 机器、把某个 repo 放到远程 GPU 上跑、收集远程训练的指标或 checkpoint——即使没说"autodl"三个字——都应使用本 skill。涉及花钱的云资源操作，跟着本 skill 的成本安全规则走。
---

# autodl：在 AutoDL GPU 云上跑任务

`autodl` CLI 把 AutoDL API + SSH 封装成任务提交管线。本文只讲**做对判断**所需的最小集合；命令细节 `--help` 和仓库 `USAGE.md` 都很全，通常不需要额外阅读。踩坑时才读 `references/troubleshooting.md`（网络/显存/依赖债/训练有效性的实战坑库），要速查全参数/配置/Python API 才读 `references/commands.md`。

## 成本安全（先读，这是 skill 存在的首要原因）

1. **每次开工先 `autodl status --json`**：余额、我的哪些实例 running（带卡计费=烧钱）、哪台是活动实例、我今日/本月花了多少（`usage`）。只读免费。账号是**共享**的：`instances[].mine` 标出哪些是用户的，`--all` 才列别人的。
2. **谁开的机谁关**：你启动的任务收尾后必须回到关机态（前台加 `--down`；后台完成后 `autodl down --yes`）。会话结束前 `status` 复核。
3. **别人的 running 实例绝不擅动**：账户里其它 running 是别人的工作负载。`stop-all` / `balance-watch` / `budget-guard` 默认只关**用户自己的**（owner 前缀或本地登记过的），`--all` / `--stop-mode stop_all` 才动全账号——除非用户明确说"全部止损"，否则绝不加。要关用户自己的某一台用 `autodl down --instance <uuid> --yes`。
4. **release 是销毁**（环境随实例消失）：只对自己刚创建的临时实例用；用户已有实例一律只关机。
5. **新建实例=新计费**：优先复用（`autodl use <uuid>` 登记后，关机实例会在 run 时自动开机）。`batch` 会开多台并按 `--on-finish` 收尾，先确认用户接受。
6. 拿不准就 `--dry-run` 预览（run/down/stop-all/kill/batch 都支持，不执行任何操作）。
7. **限额是用户自己的**（`autodl.yaml` 的 `budget.*`）：退出码 7 = 个人预算触顶，**不要绕**（不要建议改配置抬高上限），报给用户。长任务给 `--max-hours`；跑飞的后台任务用 `autodl kill --run-id`。花费用 `autodl cost` 查，别用余额差分推算。

## 高频工作流

```bash
# 快速跑一条命令（自动开机→执行→关机）
autodl use pro-xxxx && autodl run --remote 'nvidia-smi' --down --json

# 长训练（黄金路径）：后台 + 声明产物回传
autodl run --script train.sh --background --name exp1 \
    --pull 'output/checkpoint-*/,metrics.json' --pull-to ./results
autodl logs -f                          # 交互实时盯；Ctrl-C 只停跟随不停任务
until autodl logs --run-id exp1 --json | jq -e '.status != "running"' >/dev/null; do sleep 60; done   # agent 轮询
autodl logs --run-id exp1 --json | jq '{status, exit_code, metrics}'
autodl down --yes

# 跑一个 git 仓库（autodl.yaml 配 git:/env: 后）
autodl clone && autodl setup --background     # 幂等；浅克隆+学术加速；hash 没变秒跳
autodl run --sync --setup --remote-script '~/autodl-tmp/repo/train.sh' --background ...
#          ^^^^^^^^^^^^^^ 本地改码 git push 后：更新代码+按需重装环境+跑，一条命令

# 查历史结果：本地台账离线可答，不要为此开机
autodl runs --json          # run_id/状态/指标；曲线用 Python: ctx.reg.get_metric_series(run_id)
```

## 关键语义（易错点）

- **`--json` 契约**：stdout 只有一行纯 JSON（进度在 stderr），直接 `| jq`。退出码：0 成功 / 3 余额不足 / 5 SSH 不可达 / 6 远端任务失败 / 7 个人预算触顶。
- `--background` 与 `--down/--release` **互斥**（后台结束时机未知），跑完自行 down。
- **指标约定**：任务把结果写实例的 `~/autodl-tmp/metrics.json`（顶层标量+可选 `series` 曲线），完成自动入本地台账；关机后台账数据仍在。指标为空先 `logs` 一次（会自动补抓），别急着断定丢了。
- 跑**别人的仓库**要适配时：补丁放本地 `patches/*.patch`（git diff 生成）配 `git.patches:`，clone/sync 自动应用、绝不 push 上游。范例：仓库 `examples/vlm-r1-refcoco/`。
- `run --instance X` 会**顺手把 X 登记为活动实例**——探查完不属于你管的机器后，记得 `autodl use <原实例>` 切回，否则之后的裸 `down` 会关错机器。
- 实例系统盘只有 30G（HF 缓存大户）：大任务前 `run --remote 'df -h /'` 看一眼。
- **训练类任务先测 baseline 再训**：base 完全不会→RL 冷启动训不动；训练 reward 刷满≠有效，必须 held-out 对比 base。细节见 troubleshooting。
