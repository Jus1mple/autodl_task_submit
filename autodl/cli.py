"""autodl CLI —— 把各能力收口成防手滑的子命令。

命令：
  balance              查余额
  stock [--region R]   查 GPU 库存（默认遍历偏好区域）
  status / ls          列出所有实例 + 计费风险分类
  stop-all [--release] 一键止损：关停所有 running 实例（--release 则彻底释放）
  up [--select-region] 拉起/复用实例，注入公钥免密，写 ssh config，打印直连信息
  down [--release]     关机当前活动实例（--release 则释放）
  run --script F [--background]  上传脚本并执行（默认前台，--background 后台脱机）
  logs [--run-id R]    查看后台任务日志（tail）
  push LOCAL [SUB]     rsync 同步本地目录到实例数据盘
  pull SUB [LOCAL]     rsync 从实例拉回产物

全局：--config 指定配置文件；--dry-run 仅预览破坏性动作；--yes 跳过确认；--json 机器可读。
退出码：0 成功；2 用法错误；3 余额不足；4 无库存；5 SSH 不可达；6 任务非零退出；1 其它。
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

import yaml

from . import monitor, scheduler
from .config import load_config, require_token
from .core import Context
from .errors import APIError, AutoDLError, ConfigError, SSHUnavailable

EXIT_OK, EXIT_USAGE, EXIT_BALANCE, EXIT_STOCK, EXIT_SSH, EXIT_TASK, EXIT_ERR = 0, 2, 3, 4, 5, 6, 1


def _confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _running_billing_class(status):
    if status == "running":
        return "带卡计费"
    if status in ("shutdown", "power_off", "shutting_down"):
        return "仅磁盘计费"
    return status or "?"


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
            print(f"[{region}] 查询失败: {e}")
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


def cmd_status(ctx, args):
    instances = ctx.api.list_instances()
    active = ctx.reg.get_active()
    rows = []
    for it in instances:
        uid = it.get("instance_uuid") or it.get("uuid") or "?"
        st = it.get("status")
        rows.append({
            "instance_uuid": uid,
            "status": st,
            "billing": _running_billing_class(st),
            "region": it.get("region_sign"),
            "active": uid == active,
        })
    if args.json:
        print(json.dumps({"balance_yuan": round(ctx.api.balance_yuan(), 2), "instances": rows},
                         ensure_ascii=False))
        return EXIT_OK
    print(f"账户余额: ¥{ctx.api.balance_yuan():.2f}    实例数: {len(rows)}")
    for r in rows:
        mark = " *" if r["active"] else "  "
        print(f"{mark} {r['instance_uuid']}  {r['status']:<14} [{r['billing']}]  {r['region'] or ''}")
    if not rows:
        print("  (无实例)")
    return EXIT_OK


def cmd_stop_all(ctx, args):
    running = [(it.get("instance_uuid") or it.get("uuid"))
               for it in ctx.api.list_instances() if it.get("status") == "running"]
    running = [u for u in running if u]
    if not running:
        print("没有 running 实例。")
        return EXIT_OK
    print(f"将关停 {len(running)} 台 running 实例: {', '.join(running)}"
          + ("（并释放）" if args.release else ""))
    if args.dry_run:
        print("[dry-run] 未执行。")
        return EXIT_OK
    if not _confirm("确认止损？", args.yes):
        print("已取消。")
        return EXIT_OK
    ctx.stop_all_running(release=args.release)
    return EXIT_OK


def cmd_up(ctx, args):
    _balance_guard(ctx)
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
    uuid = ctx.reg.get_active()
    if not uuid:
        print("没有记录的活动实例。")
        return EXIT_OK
    print(f"将{'释放' if args.release else '关机'}实例 {uuid}")
    if args.dry_run:
        print("[dry-run] 未执行。")
        return EXIT_OK
    if not _confirm("确认？", args.yes):
        print("已取消。")
        return EXIT_OK
    ctx.power_off_with_cost(uuid)
    if args.release:
        time.sleep(15)
        r = ctx.api.release(uuid)
        print(f"  释放: {r.get('code')} {r.get('msg')}")
        ctx.reg.remove_instance(uuid)
        ctx.reg.set_active(None)
    return EXIT_OK


def cmd_use(ctx, args):
    """登记一台已有实例为当前活动实例（不创建）。"""
    st = ctx.use_instance(args.instance_uuid)
    if st is None:
        print(f"警告：实例 {args.instance_uuid} 查不到状态（可能不存在/已释放），但已登记。")
        return EXIT_USAGE
    return EXIT_OK


def cmd_run(ctx, args):
    modes = [bool(args.script), bool(args.remote_script), bool(args.remote)]
    if sum(modes) != 1:
        print("请且仅指定一种执行方式：--script（上传本地脚本）/ "
              "--remote-script（实例上已有的脚本）/ --remote（任意远端命令）", file=sys.stderr)
        return EXIT_USAGE
    _balance_guard(ctx)

    # 解析目标实例：--instance 指定已有实例；否则复用活动实例 / 新建
    if args.instance:
        ctx.use_instance(args.instance, log=None)
        uuid = args.instance
        snap = ctx.ensure_specific(uuid)
    else:
        uuid, snap = ctx.ensure_instance(select_region=args.select_region)

    # 构造要执行的命令（remote 模式不上传，直接跑实例上已有的脚本/命令）
    if args.remote_script:
        rs = args.remote_script
        command = f"cd {shlex.quote(str(Path(rs).parent) or '.')} && bash {shlex.quote(Path(rs).name)}"
        descr = {"remote_script": rs}
    elif args.remote:
        command = args.remote
        descr = {"remote": args.remote}
    else:
        command = None
        descr = {"script": args.script}

    rc = EXIT_OK
    try:
        if args.background:
            run_id = args.name or f"run-{int(time.time())}"
            try:
                if command is None:  # 上传本地脚本后台跑
                    meta = ctx.ssh.run_background(snap, open(args.script, encoding="utf-8").read(), run_id, uuid)
                else:                # 跑实例上已有的脚本/命令
                    meta = ctx.ssh.run_background_command(snap, command, run_id, uuid)
            except (SSHUnavailable, OSError) as e:
                # 实例已就绪（可能是本次刚创建的），但任务没起来——绝不能静默放任计费
                print(f"⚠️ 后台任务启动失败: {e}", file=sys.stderr)
                print(f"⚠️ 实例 {uuid} 已就绪但任务未启动、仍在计费！"
                      f"请尽快 `autodl down`（或 `autodl down --release`）。", file=sys.stderr)
                return EXIT_SSH
            ctx.reg.record_run(run_id, uuid, descr, meta["log"], meta["exit_file"], meta["pid"])
            print(f"后台任务已启动: run_id={run_id} pid={meta['pid']}")
            print(f"  日志: {meta['log']}")
            print(f"  查看进度: autodl logs --run-id {run_id}")
            # 后台模式下不在这里关机（任务还在跑）；由 logs 完成后用户决定 down
            return EXIT_OK

        # 前台同步：等任务结束、拿到结果
        if command is None:
            out, err, code = _run_inline(ctx, snap, open(args.script, encoding="utf-8").read(), uuid)
        else:
            print(f"执行: {command}")
            out, err, code = ctx.ssh.run(snap, command, uuid)
        print("\n======== 任务输出 ========")
        print(out)
        print("==========================")
        if err.strip():
            print(f"[stderr]\n{err}")
        if code != 0:
            print(f"[远程退出码] {code}")
            rc = EXIT_TASK
    finally:
        # 执行完按需关机/释放（前台模式才在此收尾）
        if not args.background:
            if args.release:
                ctx.power_off_with_cost(uuid)
                time.sleep(15)
                r = ctx.api.release(uuid)
                print(f"  释放: {r.get('code')} {r.get('msg')}")
                ctx.reg.remove_instance(uuid)
                ctx.reg.set_active(None)
            elif args.down:
                ctx.power_off_with_cost(uuid)
                print("  已关机（保留实例，下次 autodl up / run --instance 即可再开机复用）")
    return rc


def _run_inline(ctx, snap, script_text, uuid):
    # 同步执行：上传脚本到数据盘再跑
    return ctx.ssh.run_script(snap, script_text, "inline", uuid)


def cmd_logs(ctx, args):
    if args.run_id:
        run = ctx.reg.get_run(args.run_id)
        if not run:
            print(f"未找到 run: {args.run_id}")
            return EXIT_USAGE
        uuid, log_file, exit_file = run["instance_uuid"], run["log_path"], run["exit_file"]
    else:
        runs = ctx.reg.list_runs()
        if not runs:
            print("没有记录的后台任务。")
            return EXIT_OK
        run = runs[-1]
        uuid, log_file, exit_file = run["instance_uuid"], run["log_path"], run["exit_file"]
        print(f"(最近任务 run_id={run['run_id']})")
    snap = ctx.api.snapshot(uuid)
    # 探活 + 退出码
    state, code = ctx.ssh.poll(snap, {"pid": run["pid"], "exit_file": exit_file}, uuid)
    print(ctx.ssh.tail(snap, log_file, lines=args.lines, instance_uuid=uuid))
    print(f"--- 状态: {state}" + (f" 退出码={code}" if code is not None else "") + " ---")
    if state == "done" and code is not None:
        ctx.reg.finish_run(run["run_id"], code)
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
    _balance_guard(ctx)
    with open(args.file, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if isinstance(spec, dict) and "jobs" in spec:
        spec = spec["jobs"]
    jobs = []
    for j in spec:
        jid = str(j["id"])
        if j.get("remote_script"):
            rs = j["remote_script"]
            cmd = f"cd {shlex.quote(str(Path(rs).parent) or '.')} && bash {shlex.quote(Path(rs).name)}"
            jobs.append({"id": jid, "command": cmd})
        elif j.get("remote"):
            jobs.append({"id": jid, "command": j["remote"]})
        elif j.get("script"):
            jobs.append({"id": jid, "script_text": open(j["script"], encoding="utf-8").read()})
        else:
            print(f"job {jid} 缺少 remote_script/remote/script 之一", file=sys.stderr)
            return EXIT_USAGE
    batch_id = args.batch_id or Path(args.file).stem
    if args.dry_run:
        print(f"[dry-run] 批次 {batch_id}: {len(jobs)} 任务，并发 {args.max_parallel}，on_finish={args.on_finish}")
        for j in jobs:
            print(f"  - {j['id']}: {j.get('command', '<上传脚本>')}")
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


def _balance_guard(ctx):
    bal = ctx.api.balance_yuan()
    if bal < ctx.cfg.min_balance_yuan:
        raise _Exit(EXIT_BALANCE, f"余额 ¥{bal:.2f} 低于阈值 ¥{ctx.cfg.min_balance_yuan:.2f}，已中止。")


class _Exit(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------- 解析器 ----------------
def build_parser():
    p = argparse.ArgumentParser(prog="autodl", description="AutoDL API 客户端工具")
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
    add("status", help="列出实例 + 计费分类")
    add("ls", help="status 的别名")
    sp = add("stop-all", help="一键止损：关停所有 running 实例")
    sp.add_argument("--release", action="store_true", help="同时释放（彻底停止磁盘计费）")
    sp = add("up", help="拉起/复用实例并打印直连信息")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp = add("down", help="关机当前活动实例")
    sp.add_argument("--release", action="store_true", help="释放而非仅关机")
    sp = add("use", help="登记一台已有实例为当前活动实例（不创建）")
    sp.add_argument("instance_uuid", help="实例 uuid，如 pro-xxxxxxxx")
    sp = add("run", help="在实例上执行任务（本地脚本/实例已有脚本/任意命令）")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--script", help="上传并执行的本地 bash 脚本路径")
    g.add_argument("--remote-script", help="实例上已有的脚本路径（如 ~/proj/submit_exec.sh），不上传")
    g.add_argument("--remote", help="在实例上直接执行的任意命令")
    sp.add_argument("--instance", help="指定要用的已有实例 uuid（默认用活动实例或新建）")
    sp.add_argument("--background", action="store_true", help="后台脱机执行（长任务用）")
    sp.add_argument("--name", help="后台任务 run_id")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp.add_argument("--down", action="store_true", help="前台任务结束后关机（保留以便复用）")
    sp.add_argument("--release", action="store_true", help="前台任务结束后释放（彻底停止计费）")
    sp = add("logs", help="查看后台任务日志")
    sp.add_argument("--run-id", help="指定 run_id（默认最近一个）")
    sp.add_argument("--lines", type=int, default=50, help="tail 行数")
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
    sp.add_argument("--stop-mode", choices=["stop_all", "active"], default="stop_all", help="急停范围")
    sp.add_argument("--once", action="store_true", help="只查一次")
    sp = add("batch", help="批量并行调度（多实例跑多任务）")
    sp.add_argument("--file", required=True, help="任务清单 YAML（list of {id, remote_script|remote|script}）")
    sp.add_argument("--max-parallel", type=int, default=2, help="并发实例数上限")
    sp.add_argument("--on-finish", choices=["release", "power_off", "keep"], default="release", help="收尾方式")
    sp.add_argument("--select-region", action="store_true", help="按库存自动选区创建")
    sp.add_argument("--no-resume", action="store_true", help="不跳过已成功的任务")
    sp.add_argument("--retries", type=int, default=0, help="单任务失败重试次数")
    sp.add_argument("--batch-id", help="批次 id（默认用清单文件名，用于 resume）")
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
    "logs": cmd_logs, "push": cmd_push, "pull": cmd_pull,
    "snapshot-env": cmd_snapshot_env, "idle-guard": cmd_idle_guard,
    "balance-watch": cmd_balance_watch, "batch": cmd_batch,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        require_token(cfg)
        ctx = Context(cfg)
        return _DISPATCH[args.cmd](ctx, args)
    except _Exit as e:
        print(str(e), file=sys.stderr)
        return e.code
    except SSHUnavailable as e:
        print(f"SSH 不可达: {e}", file=sys.stderr)
        return EXIT_SSH
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return EXIT_USAGE
    except AutoDLError as e:
        print(f"错误: {e}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
