"""autodl CLI —— 把各能力收口成防手滑的子命令。

命令：
  balance              查余额
  stock [--region R]   查 GPU 库存（默认遍历偏好区域）
  status / ls          列出所有实例 + 计费风险分类
  stop-all [--release] 一键止损：关停所有 running 实例（--release 则彻底释放）
  up [--select-region] 拉起/复用实例，注入公钥免密，写 ssh config，打印直连信息
  down [--release]     关机当前活动实例（--release 则释放）
  run --script F [--background]  上传脚本并执行（默认前台，--background 后台脱机）
  clone / sync         在实例上克隆项目仓库 / 从 remote 更新（配合本地改码后 git push）
  setup [--force]      按仓库依赖声明准备环境（hash 幂等，未变秒跳）
  logs [--run-id R]    查看后台任务日志 + 退出码 + 指标
  kill [--run-id R]    终止后台任务（kill 进程组）
  runs [--limit N]     列出台账里的运行记录（含指标）
  cost [--since 7d]    我的花费报表（共享账号下只算我开机的时段）
  budget-guard         个人预算/会话时长守护（只关我的实例）
  push LOCAL [SUB]     rsync 同步本地目录到实例数据盘
  pull SUB [LOCAL]     rsync 从实例拉回产物

全局：--config 指定配置文件；--dry-run 仅预览破坏性动作；--yes 跳过确认；--json 机器可读。
--json 时 stdout 只输出 JSON（进度日志走 stderr），可以直接 `| jq` / 被脚本消费。
退出码：0 成功；2 用法错误；3 余额不足；4 无库存；5 SSH 不可达；6 任务非零退出；7 个人预算触顶；1 其它。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

import time

from . import __version__, cost, envsetup, gitsync, monitor, scheduler, tasks
from .config import load_config, require_token
from .core import Context, billing_class
from .errors import (APIError, AutoDLError, BudgetExceeded, ConfigError, InsufficientBalance,
                     SSHUnavailable)

EXIT_OK, EXIT_USAGE, EXIT_BALANCE, EXIT_STOCK, EXIT_SSH, EXIT_TASK, EXIT_ERR = 0, 2, 3, 4, 5, 6, 1
EXIT_BUDGET = 7


def _confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _print_chunk(chunk):
    """前台任务的实时回显（stdout/stderr 交错原样输出）。"""
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _eprint(msg):
    """进度日志走 stderr：--json 模式下 stdout 只留给 JSON 数据。"""
    print(msg, file=sys.stderr)


# ---------------- 命令实现 ----------------
def cmd_balance(ctx, args):
    bal = ctx.api.balance_yuan()
    if args.json:
        print(json.dumps({"balance_yuan": round(bal, 2)}))
    else:
        print(f"账户余额: ¥{bal:.2f}")
    return EXIT_OK


def cmd_stock(ctx, args):
    regions = [args.region] if args.region else ctx.cfg.regions
    out = {}
    for region in regions:
        try:
            data = ctx.api.gpu_stock(region, cuda_v_from=ctx.cfg.cuda_v_from) or []
        except APIError as e:
            print(f"[{region}] 查询失败: {e}", file=sys.stderr)
            continue
        items = {name: st for entry in data for name, st in entry.items()}
        out[region] = items
        if not args.json:
            line = ", ".join(f"{n}:{s.get('idle_gpu_num')}/{s.get('total_gpu_num')}"
                             for n, s in items.items()) or "无数据"
            print(f"[{region}] {line}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return EXIT_OK


def _usage_line(u, b):
    """一行"我的"用量摘要（status / budget-guard 共用）。"""
    def cap(v, c):
        return f"¥{v:.2f}" + (f"/{c:.0f}" if c else "")
    return (f"我的花费: 今日 {cap(u['today'], b.daily_yuan)}  本周 {cap(u['week'], b.weekly_yuan)}  "
            f"本月 {cap(u['month'], b.monthly_yuan)}（+磁盘≈¥{u['disk_month']:.2f}）  "
            f"running {u['running']} 台  ¥{u['burn_rate']:.2f}/h")


def cmd_status(ctx, args):
    instances = ctx.api.list_instances()
    try:
        cost.reconcile(ctx, items=instances)   # 顺手对账：别人开/关了我的机也能追平
    except AutoDLError:
        pass
    active = ctx.reg.get_active()
    rows = []
    for it in instances:
        uid = it.get("instance_uuid") or it.get("uuid") or "?"
        st = it.get("status")
        rows.append({
            "instance_uuid": uid,
            "name": it.get("name") or "",
            "status": st,
            "billing": billing_class(st),
            "region": it.get("region_sign"),
            "active": uid == active,
            "mine": cost.is_mine(ctx, it),
        })
    u = cost.usage(ctx)
    if args.json:
        print(json.dumps({"balance_yuan": round(ctx.api.balance_yuan(), 2), "instances": rows,
                          "usage": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in u.items()}},
                         ensure_ascii=False))
        return EXIT_OK
    mine = [r for r in rows if r["mine"]]
    print(f"账户余额: ¥{ctx.api.balance_yuan():.2f}（共享）    实例数: {len(rows)}，我的: {len(mine)}")
    print(_usage_line(u, ctx.cfg.budget))
    show = rows if getattr(args, "all", False) else mine
    for r in show:
        mark = " *" if r["active"] else "  "
        who = "我" if r["mine"] else "他"
        print(f"{mark} {r['instance_uuid']}  {r['status']:<14} [{r['billing']}] {who}  {r['region'] or ''}  {r['name']}")
    if not show:
        print("  (无实例)" if not rows else f"  (我的实例为空；--all 查看全部 {len(rows)} 台)")
    elif not getattr(args, "all", False) and len(rows) > len(mine):
        print(f"  … 另有 {len(rows) - len(mine)} 台别人的实例（--all 查看）")
    return EXIT_OK


def cmd_stop_all(ctx, args):
    scope = "all" if getattr(args, "all", False) else "mine"
    items, others = ctx.running_instances(scope)
    running = [it.get("uuid") or it.get("instance_uuid") for it in items]
    if not running:
        print("没有我的 running 实例。" + (f"（另有 {len(others)} 台别人的，--all 才会碰）" if others else ""))
        return EXIT_OK
    who = "账号里全部" if scope == "all" else "我的"
    print(f"将关停{who} {len(running)} 台 running 实例: {', '.join(running)}"
          + ("（并释放）" if args.release else ""))
    if others:
        print(f"  跳过 {len(others)} 台别人的实例（--all 才会碰）")
    if args.dry_run:
        print("[dry-run] 未执行。")
        return EXIT_OK
    if scope == "all" and not args.yes:
        print("⚠️ --all 会关停共享账号里所有人的实例！")
    if not _confirm("确认止损？", args.yes):
        print("已取消。")
        return EXIT_OK
    ctx.stop_all_running(release=args.release, scope=scope)
    return EXIT_OK


def cmd_up(ctx, args):
    tasks.check_balance(ctx)
    uuid, snap = ctx.ensure_instance(select_region=args.select_region)
    key = ctx.ssh.ensure_key_access(snap, uuid)
    alias = ctx.ssh.write_ssh_config(snap, identity_file=key or None)
    host, port = snap["proxy_host"], snap["ssh_port"]
    print("\n实例就绪：")
    print(f"  uuid     : {uuid}")
    print(f"  SSH      : ssh {alias}    (或 ssh -p {port} root@{host})")
    print(f"  VSCode   : Remote-SSH 连接到主机 \"{alias}\"")
    print(f"  Jupyter  : ssh -L 8888:localhost:8888 {alias}   然后浏览器开 localhost:8888")
    print(f"  代码同步 : autodl push <本地目录>    产物回收: autodl pull <远端子路径> <本地目录>")
    if not key:
        print("  (未能注入 SSH 公钥，rsync/免密不可用；ssh 仍可用密码登录)")
    return EXIT_OK


def cmd_down(ctx, args):
    uuid = getattr(args, "instance", None) or ctx.reg.get_active()
    if not uuid:
        print("没有记录的活动实例（可加 --instance <uuid> 指定）。")
        return EXIT_OK
    print(f"将{'释放' if args.release else '关机'}实例 {uuid}")
    if args.dry_run:
        print("[dry-run] 未执行。")
        return EXIT_OK
    if not _confirm("确认？", args.yes):
        print("已取消。")
        return EXIT_OK
    ok = ctx.finish_instance(uuid, "release" if args.release else "power_off")
    return EXIT_OK if ok else EXIT_ERR


def cmd_use(ctx, args):
    """登记一台已有实例为当前活动实例（不创建）。"""
    st = ctx.use_instance(args.instance_uuid)
    if st is None:
        print(f"警告：实例 {args.instance_uuid} 查不到状态（可能不存在/已释放），但已登记。")
        return EXIT_USAGE
    return EXIT_OK


def _resolve_ready(ctx, instance, log=print):
    """目标实例（--instance 或活动实例），确保 running（关机则开机）。返回 (uuid, snap)。
    log 默认 print；--json 模式须传 _eprint，避免开机进度污染 stdout 的纯 JSON。"""
    uuid = instance or ctx.reg.get_active()
    if not uuid:
        raise AutoDLError("没有目标实例：加 --instance，或先 autodl use / up")
    return uuid, ctx.ensure_specific(uuid, log=log)


def _print_patches(p, file=None):
    """展示 patch 应用结果。"""
    file = file or sys.stdout
    if not p or not p.get("total"):
        return
    parts = []
    if p["applied"]:
        parts.append(f"应用 {len(p['applied'])}")
    if p["skipped"]:
        parts.append(f"已在 {len(p['skipped'])}")
    if p["failed"]:
        parts.append(f"冲突 {len(p['failed'])}")
    print(f"  本地 patch（只改工作区，不 commit/push）：{', '.join(parts)}", file=file)
    for n in p["failed"]:
        print(f"    ⚠️ 冲突: {n}（上游可能已改同处，需更新此 patch）", file=file)


def cmd_clone(ctx, args):
    uuid, snap = _resolve_ready(ctx, args.instance, log=_eprint if args.json else print)
    res = gitsync.clone(ctx, snap, uuid, repo=args.repo, branch=args.branch, dir=args.dir)
    logf = _eprint if args.json else print
    patches = gitsync.apply_patches(ctx, snap, uuid, dir=res["dir"],
                                    log=None if args.json else logf) \
        if gitsync.local_patches(ctx.cfg) else None
    if patches:
        res["patches"] = patches
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return EXIT_OK
    print(("已克隆" if res["created"] else "已存在（已 fetch 最新）") + f": {res['dir']}  [{res['branch']}]")
    print(f"  HEAD: {res['head']}")
    if res["credentials"]:
        print("  已写入私有仓库凭据（来自本地 AUTODL_GIT_TOKEN）")
    _print_patches(patches)
    return EXIT_OK


def _print_sync(r, file=None):
    file = file or sys.stdout
    if r["blocked"]:
        print(f"✗ 已拒绝更新（mode=ff）：{r['dir']} 有未提交改动：", file=file)
        for line in r["dirty"]:
            print(f"    {line}", file=file)
        print("  处理：--mode stash（改动收进 stash）或 --mode reset（丢弃改动，保留未跟踪文件）",
              file=file)
    elif r["error"]:
        print(f"✗ 更新失败：{r['error']}", file=file)
    elif r["updated"]:
        print(f"已更新 {r['old']} -> {r['new']}  [{r['branch']}]  {r['message']}", file=file)
    else:
        print(f"已是最新（{r['new']}）  [{r['branch']}]  {r['message']}", file=file)
    if r["dirty"] and not r["blocked"]:
        print(f"  （工作区有 {len(r['dirty'])} 处改动/未跟踪文件，已按 mode={r['mode']} 处理）", file=file)


def cmd_sync(ctx, args):
    uuid, snap = _resolve_ready(ctx, args.instance, log=_eprint if args.json else print)
    logf = _eprint if args.json else print
    has_patches = bool(gitsync.local_patches(ctx.cfg))
    # 配了 patch 时用 reset：旧 patch 是 tracked 改动，reset 丢弃它们、保留产物，再重放 patch
    mode = "reset" if (has_patches and args.mode == "ff") else args.mode
    res = gitsync.sync(ctx, snap, uuid, mode=mode, dir=args.dir, branch=args.branch)
    if has_patches and not res["blocked"] and not res["error"]:
        res["patches"] = gitsync.apply_patches(ctx, snap, uuid, dir=res["dir"],
                                               log=None if args.json else logf)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        _print_sync(res)
        _print_patches(res.get("patches"))
    return EXIT_OK if not (res["blocked"] or res["error"]) else EXIT_ERR


def cmd_patch(ctx, args):
    """单独把本地 patch 目录应用到实例（clone/sync 已自动带；此命令用于手动重放/调试）。"""
    if not gitsync.local_patches(ctx.cfg):
        print("未配置本地 patch：在 autodl.yaml 设 git.patches: <目录>（放 *.patch）", file=sys.stderr)
        return EXIT_USAGE
    uuid, snap = _resolve_ready(ctx, args.instance, log=_eprint if args.json else print)
    res = gitsync.apply_patches(ctx, snap, uuid, dir=args.dir,
                                log=None if args.json else print)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        _print_patches(res)
    return EXIT_OK if not res["failed"] else EXIT_ERR


def cmd_setup(ctx, args):
    uuid, snap = _resolve_ready(ctx, args.instance, log=_eprint if args.json else print)
    res = envsetup.run_setup(ctx, snap, uuid, dir=args.dir, force=args.force,
                             background=args.background,
                             stream=None if args.json else _print_chunk,
                             log=_eprint if args.json else print)
    if args.json:
        out = dict(res)
        for k in ("stdout", "stderr"):  # 安装日志可能巨大，JSON 里只留尾部
            if isinstance(out.get(k), str) and len(out[k]) > 2000:
                out[k] = out[k][-2000:]
        print(json.dumps(out, ensure_ascii=False))
    elif res.get("skipped"):
        print(f"环境未变（{res['source']}），跳过安装。--force 可强制重装。")
    elif res.get("background"):
        print(f"环境安装已后台启动: run_id={res['run_id']}")
        print(f"  进度: autodl logs --run-id {res['run_id']}")
    elif res["exit_code"] == 0:
        print("环境安装完成。长期复用建议固化镜像：autodl snapshot-env --name <名字>")
    else:
        print(f"环境安装失败 exit={res['exit_code']}", file=sys.stderr)
    if res.get("skipped") or res.get("background"):
        return EXIT_OK
    return EXIT_OK if res["exit_code"] == 0 else EXIT_TASK


def cmd_run(ctx, args):
    chosen = [(m, v) for m, v in (("script", args.script),
                                  ("remote_script", args.remote_script),
                                  ("remote", args.remote)) if v]
    if len(chosen) != 1:
        print("请且仅指定一种执行方式：--script（上传本地脚本）/ "
              "--remote-script（实例上已有的脚本）/ --remote（任意远端命令）", file=sys.stderr)
        return EXIT_USAGE
    mode, value = chosen[0]
    if args.background and (args.down or args.release):
        print("--background 与 --down/--release 不能同时用（后台任务结束时机未知）；"
              "请任务完成后自行 `autodl down`。", file=sys.stderr)
        return EXIT_USAGE
    if args.dry_run:
        # 预览而不执行：run 会开机（花钱）+ 跑任务，dry-run 必须在任何 API 调用之前拦下
        target = args.instance or ctx.reg.get_active() or "(无活动实例→将新建，产生新计费)"
        preview = tasks.build_command(mode, value) or f"(上传并执行 {value})"
        teardown = "release" if args.release else ("power_off" if args.down else "keep")
        print(f"[dry-run] 实例: {target}（关机则自动开机）")
        if args.sync:
            print(f"[dry-run] 先 sync 仓库（mode={args.sync_mode}）" + ("，再 setup 环境" if args.setup else ""))
        elif args.setup:
            print("[dry-run] 先 setup 环境（hash 幂等）")
        print(f"[dry-run] 执行: {preview}" + ("  [后台]" if args.background else ""))
        mh = tasks.effective_max_hours(ctx, getattr(args, "max_hours", None))
        if mh:
            print(f"[dry-run] 时限: {mh:g}h（实例侧 timeout，超时 exit=124）")
        if args.pull:
            print(f"[dry-run] 完成后拉回 {args.pull} -> {args.pull_to}")
        print(f"[dry-run] 收尾: {teardown}。未执行任何操作。")
        return EXIT_OK
    if args.instance:
        ctx.use_instance(args.instance, log=None)  # 顺手登记为活动实例，之后 logs/down 默认用它
    logf = _eprint if args.json else print

    instance = args.instance
    if args.sync or args.setup:
        # 跑之前先把实例上的代码/环境弄到位；任何一步失败都阻断任务（不浪费卡时）
        tasks.check_balance(ctx)
        if instance:
            uuid0, snap0 = instance, ctx.ensure_specific(instance, log=logf)
        else:
            uuid0, snap0 = ctx.ensure_instance(select_region=args.select_region, log=logf)
        if args.sync:
            res_sync = gitsync.update(ctx, snap0, uuid0, mode=args.sync_mode, log=logf)
            if res_sync["blocked"] or res_sync["error"]:
                _print_sync(res_sync, file=sys.stderr)
                print("sync 未完成，已中止运行（实例保持原样）。", file=sys.stderr)
                return EXIT_ERR
            logf(f"sync: {res_sync['old'] or '-'} -> {res_sync['new']}  {res_sync['message']}")
            p = res_sync.get("patches")
            if p and p.get("failed"):
                print(f"patch 有 {len(p['failed'])} 个冲突，已中止运行（避免用未适配的代码跑）：{p['failed']}",
                      file=sys.stderr)
                return EXIT_ERR
        if args.setup:
            res_setup = envsetup.run_setup(ctx, snap0, uuid0,
                                           stream=None if args.json else _print_chunk, log=logf)
            if res_setup.get("skipped"):
                logf(f"环境未变（{res_setup['source']}），跳过安装")
            elif res_setup["exit_code"] != 0:
                print(f"环境安装失败(exit={res_setup['exit_code']})，已中止运行。"
                      f"日志: autodl logs --run-id {res_setup['run_id']}", file=sys.stderr)
                return EXIT_TASK
            else:
                logf("环境安装完成")
        instance = uuid0

    artifacts = {"patterns": args.pull, "local_dir": args.pull_to} if args.pull else None
    teardown = "release" if args.release else ("power_off" if args.down else "keep")
    res = tasks.submit(
        ctx, mode=mode, value=value, instance=instance,
        select_region=args.select_region, background=args.background,
        teardown=teardown, run_id=args.name, artifacts=artifacts,
        max_hours=getattr(args, "max_hours", None),
        stream=None if (args.json or args.background) else _print_chunk,
        log=logf, check_balance_first=not (args.sync or args.setup),
    )
    if args.background:
        if args.json:
            print(json.dumps({k: res[k] for k in ("run_id", "pid", "log", "exit_file", "instance")},
                             ensure_ascii=False))
        else:
            print(f"后台任务已启动: run_id={res['run_id']} pid={res['pid']}")
            print(f"  日志: {res['log']}")
            print(f"  查看进度: autodl logs --run-id {res['run_id']}")
            if artifacts:
                print(f"  完成后自动拉回 {args.pull} -> {args.pull_to}（logs 时触发）")
        # 后台模式下不在这里关机（任务还在跑）；完成后由用户决定 down
        return EXIT_OK
    if args.json:
        payload = {k: res[k] for k in ("run_id", "exit_code", "stdout", "stderr", "instance")}
        payload["metrics"] = ctx.reg.get_metrics(res["run_id"])
        payload["artifacts"] = res.get("artifacts")
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if res.get("artifacts"):
            a = res["artifacts"]
            print(f"产物已拉回 {len(a['files'])} 项 -> {a['local_dir']}")
        if res["exit_code"] != 0:
            print(f"[远程退出码] {res['exit_code']}")
    return EXIT_OK if res["exit_code"] == 0 else EXIT_TASK


def _follow_logs(ctx, run):
    """实时跟随后台任务日志到完成（tail -f 式），结束后抓指标并拉产物。Ctrl-C 停跟随不停任务。"""
    uuid = run.get("instance_uuid")
    lp, ef = run.get("log_path"), run.get("exit_file")
    if not lp or lp == "(sync)" or not ef:
        print("该 run 不是后台任务，无日志文件可跟随（前台任务本就实时回显）。", file=sys.stderr)
        return EXIT_USAGE
    if ctx.api.status_or_none(uuid) != "running":
        print("实例非 running，无法实时跟随；改为显示最后快照：", file=sys.stderr)
        info = tasks.refresh_run(ctx, run, lines=100)
        if info["log"]:
            print(info["log"])
        return EXIT_OK
    print(f"--- 实时跟随 {run['run_id']}（Ctrl-C 停止跟随，不影响后台任务）---", file=sys.stderr)
    try:
        ctx.ssh.follow(ctx.api.snapshot(uuid), lp, ef, run.get("pid"),
                       lines=100, stream=_print_chunk, instance_uuid=uuid)
    except KeyboardInterrupt:
        print("\n（已停止跟随，任务仍在后台运行；autodl logs 可再看）", file=sys.stderr)
        return EXIT_OK
    except (SSHUnavailable, AutoDLError) as e:
        print(f"\n跟随中断：{e}", file=sys.stderr)
    # 任务结束：刷新一次抓指标 + 拉产物
    info = tasks.refresh_run(ctx, run, lines=0)
    cur = ctx.reg.get_run(run["run_id"]) or run
    m = ctx.reg.get_metrics(run["run_id"])
    line = f"\n--- 状态: {cur['status']}"
    if cur.get("exit_code") is not None:
        line += f" 退出码={cur['exit_code']}"
    print(line + " ---", file=sys.stderr)
    if m:
        print("指标: " + json.dumps(m, ensure_ascii=False), file=sys.stderr)
    if info.get("artifacts"):
        a = info["artifacts"]
        print(f"产物已拉回 {len(a['files'])} 项 -> {a['local_dir']}", file=sys.stderr)
    return EXIT_OK if cur.get("exit_code") in (0, None) else EXIT_TASK


def cmd_logs(ctx, args):
    if args.run_id:
        run = ctx.reg.get_run(args.run_id)
        if not run:
            print(f"未找到 run: {args.run_id}", file=sys.stderr)
            return EXIT_USAGE
    else:
        runs = ctx.reg.list_runs()
        if not runs:
            print("没有记录的后台任务。", file=sys.stderr if args.json else sys.stdout)
            return EXIT_OK
        run = runs[-1]
        if not args.json:
            print(f"(最近任务 run_id={run['run_id']})")
    if getattr(args, "follow", False):
        return _follow_logs(ctx, run)
    info = tasks.refresh_run(ctx, run, lines=args.lines)
    cur = ctx.reg.get_run(run["run_id"]) or run
    m = ctx.reg.get_metrics(run["run_id"])
    if args.json:
        print(json.dumps({"run_id": run["run_id"], "status": cur["status"],
                          "exit_code": cur.get("exit_code"), "instance": cur.get("instance_uuid"),
                          "log": info["log"], "note": info["note"], "metrics": m,
                          "artifacts": info.get("artifacts")},
                         ensure_ascii=False))
        return EXIT_OK
    if info["log"]:
        print(info["log"])
    if info["note"]:
        print(f"({info['note']})")
    if info.get("artifacts"):
        a = info["artifacts"]
        print(f"产物已拉回 {len(a['files'])} 项 -> {a['local_dir']}")
    line = f"--- 状态: {cur['status']}"
    if cur.get("exit_code") is not None:
        line += f" 退出码={cur['exit_code']}"
    print(line + " ---")
    if m:
        print("指标: " + json.dumps(m, ensure_ascii=False))
    return EXIT_OK


def cmd_kill(ctx, args):
    """终止后台任务（默认最近一个 running 的）。"""
    if args.run_id:
        run = ctx.reg.get_run(args.run_id)
        if not run:
            print(f"未找到 run: {args.run_id}", file=sys.stderr)
            return EXIT_USAGE
    else:
        runs = [r for r in ctx.reg.list_runs() if r.get("status") == "running" and r.get("exit_file")]
        if not runs:
            print("没有 running 的后台任务。", file=sys.stderr)
            return EXIT_OK
        run = runs[-1]
    print(f"将终止 {run['run_id']}（实例 {run.get('instance_uuid')}）", file=sys.stderr if args.json else sys.stdout)
    if args.dry_run:
        print("[dry-run] 未执行。")
        return EXIT_OK
    if not args.json and not _confirm("确认终止？", args.yes):
        print("已取消。")
        return EXIT_OK
    res = tasks.kill_run(ctx, run, log=None if args.json else print)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(res["note"])
    return EXIT_OK if res["killed"] or res["state"] != "running" else EXIT_ERR


def cmd_cost(ctx, args):
    """我的花费报表：只统计我开机的时段 × 该实例小时价（+ 磁盘费估算）。"""
    since = cost.parse_since(args.since)
    if not getattr(args, "offline", False):
        try:
            cost.reconcile(ctx, log=_eprint if args.json else print)
        except AutoDLError as e:
            _eprint(f"对账失败（{e}），按本地账本统计")
    by = args.by
    if by == "run":
        rows = [r for r in cost.run_costs(ctx, ctx.reg.list_runs()) if (r.get("started_at") or 0) >= since]
        rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        rep = {"rows": [{"run_id": r["run_id"], "status": r["status"], "instance": r.get("instance_uuid"),
                         "hours": r["hours"], "price": r["price"], "cost": r["cost_yuan"],
                         "started_at": r.get("started_at")} for r in rows],
               "total": sum(r["cost_yuan"] or 0 for r in rows), "disk": 0.0, "since": since,
               "until": time.time()}
    else:
        rep = cost.report(ctx, since, by=by)
    u = cost.usage(ctx)
    if args.json:
        rep["usage"] = u
        print(json.dumps(rep, ensure_ascii=False, default=float))
        return EXIT_OK
    label = {"day": "日期", "instance": "实例", "session": "会话", "run": "run"}[by]
    print(f"自 {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} 起，按{label}：")
    for r in rep["rows"]:
        if by == "day":
            print(f"  {r['day']}  {r['hours']:6.2f}h  ¥{r['cost']:8.2f}")
        elif by == "instance":
            print(f"  {r['instance_uuid']:<18} {r['hours']:6.2f}h  ¥{r['cost']:8.2f}  {r['sessions']} 段  {r.get('gpu_spec') or ''}")
        elif by == "session":
            st = time.strftime("%m-%d %H:%M", time.localtime(r["started_at"]))
            en = time.strftime("%m-%d %H:%M", time.localtime(r["ended_at"])) if r["ended_at"] else "running"
            print(f"  {r['instance_uuid']:<18} {st} → {en:<12} {r['hours']:6.2f}h × ¥{r['price']:.2f} = ¥{r['cost']:.2f}  [{r['source']}] {r.get('project') or ''}")
        else:
            c = f"¥{r['cost']:.2f}" if r["cost"] is not None else "?"
            h = f"{r['hours']:.2f}h" if r["hours"] is not None else "-"
            print(f"  {r['run_id']:<40} {r['status']:<10} {h:>8}  {c:>9}")
    if not rep["rows"]:
        print("  (无记录)")
    print(f"合计 ¥{rep['total']:.2f}" + (f"，磁盘费估算 ¥{rep['disk']:.2f}" if rep.get("disk") else ""))
    print(_usage_line(u, ctx.cfg.budget))
    return EXIT_OK


def cmd_budget_guard(ctx, args):
    res = monitor.budget_guard(ctx, interval=args.interval, once=args.once)
    print(f"budget-guard 结束: {res}")
    return EXIT_OK


def cmd_runs(ctx, args):
    """列出台账里的运行记录（最近的在前，含指标）——只查本地 SQLite，不触网。"""
    allm = ctx.reg.all_metrics()
    rows = []
    for r in ctx.reg.list_runs():
        rows.append({"run_id": r["run_id"], "status": r["status"], "exit_code": r["exit_code"],
                     "instance": r["instance_uuid"], "experiment_id": r.get("experiment_id"),
                     "tag": r.get("tag"), "started_at": r["started_at"],
                     "metrics": allm.get(r["run_id"], {})})
    rows.sort(key=lambda x: x["started_at"] or 0, reverse=True)
    if args.limit:
        rows = rows[: args.limit]
    if args.json:
        print(json.dumps({"runs": rows}, ensure_ascii=False))
        return EXIT_OK
    if not rows:
        print("台账里没有运行记录。")
        return EXIT_OK
    for r in rows:
        code = "-" if r["exit_code"] is None else str(r["exit_code"])
        tag = f"[{r['tag']}] " if r["tag"] else ""
        m = " ".join(f"{k}={v}" for k, v in r["metrics"].items())
        print(f"{r['run_id']:<40} {r['status']:<10} exit={code:<4} {tag}{m}".rstrip())
    return EXIT_OK


def cmd_push(ctx, args):
    uuid = ctx.reg.get_active()
    if not uuid:
        print("没有活动实例，先 autodl up。")
        return EXIT_USAGE
    snap = ctx.api.snapshot(uuid)
    key = ctx.ssh.ensure_key_access(snap, uuid)
    sub = args.subdir or "code"
    out = ctx.ssh.push(snap, args.local, sub, key, instance_uuid=uuid)
    print(out)
    print(f"已同步到 {ctx.cfg.ssh.remote_workdir}/{sub}")
    return EXIT_OK


def cmd_pull(ctx, args):
    uuid = ctx.reg.get_active()
    if not uuid:
        print("没有活动实例，先 autodl up。")
        return EXIT_USAGE
    snap = ctx.api.snapshot(uuid)
    key = ctx.ssh.ensure_key_access(snap, uuid)
    out = ctx.ssh.pull(snap, args.subpath, args.local or ".", key, instance_uuid=uuid)
    print(out)
    print(f"已拉回到 {args.local or '.'}")
    return EXIT_OK


def cmd_snapshot_env(ctx, args):
    uuid = args.instance or ctx.reg.get_active()
    if not uuid:
        print("没有实例。先 autodl up 或加 --instance。", file=sys.stderr)
        return EXIT_USAGE
    print("注意：私有镜像会持续占存储费，且 AutoDL 没有删除镜像 API（只能去控制台删）。")
    if not _confirm("确认保存镜像？", args.yes):
        print("已取消。")
        return EXIT_OK
    image_uuid = ctx.snapshot_env(uuid, args.name, wait=not args.no_wait)
    print(f"镜像 image_uuid: {image_uuid}")
    print(f"以后可在 autodl.yaml 设 image_uuid: {image_uuid} 复用该环境。")
    return EXIT_OK


def cmd_idle_guard(ctx, args):
    res = monitor.idle_guard(
        ctx, instance_uuid=args.instance, idle_minutes=args.idle_minutes,
        interval=args.interval, util_threshold=args.util, mem_threshold_mib=args.mem_mib,
        keepalive_path=args.keepalive, once=args.once,
    )
    print(f"idle-guard 结束: {res}")
    return EXIT_OK


def cmd_balance_watch(ctx, args):
    res = monitor.balance_watch(
        ctx, warn_yuan=args.warn, stop_yuan=args.stop, interval=args.interval,
        stop_mode=args.stop_mode, once=args.once,
    )
    print(f"balance-watch 结束: {res}")
    return EXIT_OK


def cmd_batch(ctx, args):
    tasks.check_balance(ctx)
    with open(args.file, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if isinstance(spec, dict) and "jobs" in spec:
        spec = spec["jobs"]
    jobs = []
    for j in spec:
        jid = str(j["id"])
        # 每个 job 可声明 pull（结果文件 glob）；逗号分隔或列表。拉回 <--pull-to>/<job_id>/
        pull = j.get("pull") or args.pull
        if isinstance(pull, list):
            pull = ",".join(str(x) for x in pull)
        base = {"id": jid, "pull": pull, "pull_to": args.pull_to}
        if j.get("remote_script"):
            jobs.append({**base, "mode": "remote_script", "value": j["remote_script"]})
        elif j.get("remote"):
            jobs.append({**base, "mode": "remote", "value": j["remote"]})
        elif j.get("script"):
            try:
                text = Path(j["script"]).expanduser().read_text(encoding="utf-8")
            except OSError as e:
                print(f"job {jid} 脚本读取失败: {e}", file=sys.stderr)
                return EXIT_USAGE
            jobs.append({**base, "mode": "script_text", "value": text, "display": j["script"]})
        else:
            print(f"job {jid} 缺少 remote_script/remote/script 之一", file=sys.stderr)
            return EXIT_USAGE
    batch_id = args.batch_id or Path(args.file).stem
    if args.dry_run:
        print(f"[dry-run] 批次 {batch_id}: {len(jobs)} 任务，并发 {args.max_parallel}，on_finish={args.on_finish}")
        for j in jobs:
            preview = j.get("display") or (tasks.build_command(j["mode"], j["value"]) or "<上传脚本>")
            print(f"  - {j['id']}: {preview}")
        return EXIT_OK
    results = scheduler.run_batch(
        ctx, jobs, max_parallel=args.max_parallel, on_finish=args.on_finish,
        select_region=args.select_region, resume=not args.no_resume,
        batch_id=batch_id, job_retries=args.retries,
    )
    print("\n===== 批次结果 =====")
    for r in sorted(results, key=lambda x: str(x["id"])):
        print(f"  {r['id']:<16} {r['status']:<10} code={r['exit_code']}  {r['instance'] or ''}")
    ok = sum(1 for r in results if r["status"] in ("succeeded", "skipped"))
    print(f"成功/跳过 {ok}/{len(results)}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    bad = [r for r in results if r["status"] in ("failed", "not_run")]
    return EXIT_TASK if bad else EXIT_OK


# ---------------- 解析器 ----------------
def build_parser():
    p = argparse.ArgumentParser(prog="autodl", description="AutoDL API 客户端工具")
    p.add_argument("--version", action="version", version=f"autodl-task-submit {__version__}")
    # 通用参数放在父解析器里，挂到每个子命令，这样 `autodl status --json` 可用（放命令后面）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径（默认就近查找 autodl.yaml）")
    common.add_argument("--dry-run", action="store_true", help="仅预览破坏性动作")
    common.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    common.add_argument("--json", action="store_true", help="机器可读输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    add("balance", help="查余额")
    sp = add("stock", help="查 GPU 库存")
    sp.add_argument("--region", help="只查指定区域")
    for nm in ("status", "ls"):
        sp = add(nm, help="列出我的实例 + 计费分类 + 我的花费（--all 看全账号）")
        sp.add_argument("--all", action="store_true", help="显示共享账号里所有人的实例")
    sp = add("stop-all", help="一键止损：关停我的 running 实例（--all 才动别人的）")
    sp.add_argument("--release", action="store_true", help="同时释放（彻底停止磁盘计费）")
    sp.add_argument("--all", action="store_true", help="关停共享账号里所有人的实例（慎！）")
    sp = add("up", help="拉起/复用实例并打印直连信息")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp = add("down", help="关机当前活动实例（或 --instance 指定的一台）")
    sp.add_argument("--release", action="store_true", help="释放而非仅关机")
    sp.add_argument("--instance", help="要关的实例 uuid（默认活动实例；不会改动活动实例登记）")
    sp = add("use", help="登记一台已有实例为当前活动实例（不创建）")
    sp.add_argument("instance_uuid", help="实例 uuid，如 pro-xxxxxxxx")
    sp = add("run", help="在实例上执行任务（本地脚本/实例已有脚本/任意命令）")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--script", help="上传并执行的本地 bash 脚本路径")
    g.add_argument("--remote-script", help="实例上已有的脚本路径（如 ~/proj/submit_exec.sh），不上传")
    g.add_argument("--remote", help="在实例上直接执行的任意命令")
    sp.add_argument("--instance", help="指定要用的已有实例 uuid（默认用活动实例或新建）")
    sp.add_argument("--background", action="store_true", help="后台脱机执行（长任务用）")
    sp.add_argument("--name", help="run_id（默认自动生成唯一 id；前后台任务都记入台账）")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp.add_argument("--down", action="store_true", help="前台任务结束后关机（保留以便复用）")
    sp.add_argument("--release", action="store_true", help="前台任务结束后释放（彻底停止计费）")
    sp.add_argument("--sync", action="store_true",
                    help="运行前先把实例仓库更新到 remote 最新（缺仓库则按 git.repo 自动 clone）")
    sp.add_argument("--sync-mode", choices=list(gitsync.SYNC_MODES), default="ff",
                    help="--sync 的脏工作区策略（默认 ff：脏则拒绝）")
    sp.add_argument("--setup", action="store_true",
                    help="运行前先按仓库依赖准备环境（hash 幂等，依赖没变则秒跳）")
    sp.add_argument("--pull", help="结果文件 glob（逗号分隔，相对数据盘），任务完成自动拉回本地。"
                                   "如 'output/checkpoint-*/adapter*.safetensors,metrics.json'")
    sp.add_argument("--pull-to", default="./results", help="产物拉回到的本地目录（默认 ./results）")
    sp.add_argument("--max-hours", type=float, help="任务时限（小时）：实例侧 timeout，到点终止(exit=124)；"
                                                     "默认 budget.max_run_hours")
    sp = add("clone", help="在实例上克隆项目仓库（幂等；已有则 fetch）")
    sp.add_argument("--repo", help="仓库 URL（默认 autodl.yaml 的 git.repo）")
    sp.add_argument("--branch", help="分支（默认 git.branch）")
    sp.add_argument("--dir", help="实例上的目录（默认 <数据盘>/repo）")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp = add("sync", help="实例仓库从 remote 更新（本地改码 git push 之后跑这个）")
    sp.add_argument("--mode", choices=list(gitsync.SYNC_MODES), default="ff",
                    help="脏工作区策略：ff=脏则拒绝 / stash=改动收进 stash / reset=丢弃改动(保留未跟踪文件)")
    sp.add_argument("--branch", help="分支（默认 git.branch）")
    sp.add_argument("--dir", help="实例上的目录（默认 <数据盘>/repo）")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp = add("patch", help="把本地 patch 目录(git.patches)应用到实例(clone/sync 已自动带)")
    sp.add_argument("--dir", help="仓库目录（默认 <数据盘>/repo）")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp = add("setup", help="按仓库依赖声明准备实例环境（hash 幂等，未变秒跳）")
    sp.add_argument("--force", action="store_true", help="忽略 hash 强制重装")
    sp.add_argument("--background", action="store_true", help="后台安装（之后 autodl logs 查进度）")
    sp.add_argument("--dir", help="仓库目录（默认 <数据盘>/repo）")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp = add("logs", help="查看后台任务日志 + 退出码 + 指标（--follow 实时跟随）")
    sp.add_argument("--run-id", help="指定 run_id（默认最近一个）")
    sp.add_argument("--lines", type=int, default=50, help="tail 行数")
    sp.add_argument("--follow", "-f", action="store_true",
                    help="实时跟随日志到任务完成（tail -f 式，Ctrl-C 停跟随不停任务）")
    sp = add("kill", help="终止后台任务（kill 实例上的进程组，登记 exit=137）")
    sp.add_argument("--run-id", help="指定 run_id（默认最近一个 running 的后台任务）")
    sp = add("runs", help="列出台账里的运行记录（含指标，只查本地不触网）")
    sp.add_argument("--limit", type=int, default=20, help="最多显示条数（0=全部）")
    sp = add("cost", help="我的花费报表（共享账号下只算我开机的时段）")
    sp.add_argument("--since", default="month", help="起点：7d / 24h / 2w / today / week / month(默认) / YYYY-MM-DD")
    sp.add_argument("--by", choices=["day", "instance", "session", "run"], default="day", help="汇总维度")
    sp.add_argument("--offline", action="store_true", help="不触网对账，只看本地账本")
    sp = add("budget-guard", help="个人预算/会话时长守护：触顶只关我的实例")
    sp.add_argument("--interval", type=int, default=300, help="轮询间隔(秒)")
    sp.add_argument("--once", action="store_true", help="只判一次不动作")
    sp = add("snapshot-env", help="把实例环境存为私有镜像（会持续占存储费）")
    sp.add_argument("--name", required=True, help="镜像名")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp.add_argument("--no-wait", action="store_true", help="不等待保存完成")
    sp = add("idle-guard", help="空闲自动关机看门狗")
    sp.add_argument("--instance", help="目标实例（默认活动实例）")
    sp.add_argument("--idle-minutes", type=int, default=15, help="连续空闲多少分钟则关机")
    sp.add_argument("--interval", type=int, default=60, help="探测间隔(秒)")
    sp.add_argument("--util", type=int, default=5, help="GPU 利用率低于该百分比算空闲")
    sp.add_argument("--mem-mib", type=int, default=500, help="显存占用低于该 MiB 才算空闲")
    sp.add_argument("--keepalive", default=monitor.DEFAULT_KEEPALIVE, help="豁免文件路径")
    sp.add_argument("--once", action="store_true", help="只探一次（巡检/测试）")
    sp = add("balance-watch", help="余额预警/急停守护")
    sp.add_argument("--warn", type=float, required=True, help="预警线(元)")
    sp.add_argument("--stop", type=float, required=True, help="急停线(元)")
    sp.add_argument("--interval", type=int, default=300, help="轮询间隔(秒)")
    sp.add_argument("--stop-mode", choices=["mine", "active", "stop_all"], default="mine",
                    help="急停范围：mine=只关我的(默认) / active=活动实例 / stop_all=全账号(慎)")
    sp.add_argument("--once", action="store_true", help="只查一次")
    sp = add("batch", help="批量并行调度（多实例跑多任务）")
    sp.add_argument("--file", required=True, help="任务清单 YAML（list of {id, remote_script|remote|script}）")
    sp.add_argument("--max-parallel", type=int, default=2, help="并发实例数上限")
    sp.add_argument("--on-finish", choices=["release", "power_off", "keep"], default="release", help="收尾方式")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp.add_argument("--no-resume", action="store_true", help="不跳过已成功的任务")
    sp.add_argument("--retries", type=int, default=0, help="单任务失败重试次数")
    sp.add_argument("--batch-id", help="批次 id（默认用清单文件名，用于 resume）")
    sp.add_argument("--pull", help="结果文件 glob（jobs.yaml 里每个 job 也可各自写 pull:）；"
                                   "每个任务完成拉回 <--pull-to>/<job_id>/")
    sp.add_argument("--pull-to", default="./results", help="产物拉回的本地根目录（默认 ./results）")
    sp = add("push", help="rsync 同步本地到实例")
    sp.add_argument("local", help="本地目录/文件")
    sp.add_argument("subdir", nargs="?", help="远端子目录（默认 code）")
    sp = add("pull", help="rsync 从实例拉回")
    sp.add_argument("subpath", help="远端子路径（相对数据盘）")
    sp.add_argument("local", nargs="?", help="本地目录（默认当前目录）")
    return p


_DISPATCH = {
    "balance": cmd_balance, "stock": cmd_stock, "status": cmd_status, "ls": cmd_status,
    "stop-all": cmd_stop_all, "up": cmd_up, "down": cmd_down, "use": cmd_use, "run": cmd_run,
    "logs": cmd_logs, "runs": cmd_runs, "clone": cmd_clone, "sync": cmd_sync,
    "patch": cmd_patch, "setup": cmd_setup, "push": cmd_push, "pull": cmd_pull,
    "snapshot-env": cmd_snapshot_env, "idle-guard": cmd_idle_guard,
    "balance-watch": cmd_balance_watch, "batch": cmd_batch,
    "kill": cmd_kill, "cost": cmd_cost, "budget-guard": cmd_budget_guard,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        require_token(cfg)
        ctx = Context(cfg)
        return _DISPATCH[args.cmd](ctx, args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except InsufficientBalance as e:
        print(str(e), file=sys.stderr)
        return EXIT_BALANCE
    except BudgetExceeded as e:
        print(f"个人预算: {e}", file=sys.stderr)
        return EXIT_BUDGET
    except SSHUnavailable as e:
        print(f"SSH 不可达: {e}", file=sys.stderr)
        return EXIT_SSH
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return EXIT_USAGE
    except AutoDLError as e:
        print(f"错误: {e}", file=sys.stderr)
        return EXIT_ERR
    except OSError as e:
        print(f"IO 错误: {e}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
