"""守护：空闲自动关机看门狗 + 余额预警/急停。

AutoDL 没有官方空闲关机开关、没有 webhook，这里靠客户端轮询补上。
关键安全点（来自评审）：
- 空闲判定用「GPU 利用率低 且 显存占用低」双条件，避免 dataloader/评估阶段（util=0 但显存还占着）被误关。
- 支持豁免：实例上存在 keepalive 文件（交互调试时 `touch /root/.autodl_keepalive`）即视为活跃。
- SSH 不可达时**回退查实例状态**：已关机/释放则停止看门狗；running 但连不上记为"未知"，
  绝不当作空闲（不误关）。
"""
from __future__ import annotations

import time

from . import cost
from .errors import APIError, SSHUnavailable

DEFAULT_KEEPALIVE = "/root/.autodl_keepalive"


def _probe_idle(ctx, snapshot, uuid, keepalive_path, util_threshold, mem_threshold_mib):
    """返回 (idle: bool, detail: str)。SSH 不可达时抛 SSHUnavailable。"""
    cmd = (
        f'echo "KEEP=$([ -f {keepalive_path} ] && echo 1 || echo 0)"; '
        f'nvidia-smi --query-gpu=utilization.gpu,memory.used '
        f'--format=csv,noheader,nounits 2>/dev/null || echo NA'
    )
    out, _err, _code = ctx.ssh.run(snapshot, cmd, uuid)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    keep = any(l == "KEEP=1" for l in lines)
    if keep:
        return False, "keepalive 文件存在"
    gpu_lines = [l for l in lines if not l.startswith("KEEP=")]
    if not gpu_lines or gpu_lines == ["NA"]:
        # 拿不到 nvidia-smi（无卡模式/驱动异常）：保守起见不判空闲
        return False, "nvidia-smi 不可用，跳过"
    parsed = 0
    max_util, max_mem = 0.0, 0
    for l in gpu_lines:
        parts = [p.strip() for p in l.split(",")]
        if len(parts) >= 2:
            try:
                u = float(parts[0]); m = int(float(parts[1]))
            except ValueError:
                continue  # [N/A] 等异常字段：跳过该行
            parsed += 1
            max_util = max(max_util, u); max_mem = max(max_mem, m)
    if parsed == 0:
        # 有输出但没一行能解析（驱动异常/格式意外）：保守，不判空闲，避免误关
        return False, "nvidia-smi 输出异常，跳过"
    idle = max_util < util_threshold and max_mem < mem_threshold_mib
    return idle, f"util={max_util:.0f}% mem={max_mem}MiB"


def idle_guard(ctx, instance_uuid=None, idle_minutes=15, interval=60,
               util_threshold=5, mem_threshold_mib=500, keepalive_path=DEFAULT_KEEPALIVE,
               once=False, log=print):
    """看门狗：实例连续空闲 idle_minutes 则 power_off。once=True 只探一次（用于测试/巡检）。"""
    uuid = instance_uuid or ctx.reg.get_active()
    if not uuid:
        raise APIError("没有指定实例，也没有活动实例")
    idle_start = None  # 连续空闲的起始时刻（按真实经过时间计，避免 off-by-one）
    need = idle_minutes * 60
    while True:
        st = ctx.api.status_or_none(uuid)
        if st in (None, "removed"):
            if log:
                log(f"实例 {uuid} 已不存在，看门狗退出。")
            return "gone"
        if st != "running":
            if log:
                log(f"实例 {uuid} 状态 {st}（非 running），看门狗退出。")
            return st
        try:
            snap = ctx.api.snapshot(uuid)
            idle, detail = _probe_idle(ctx, snap, uuid, keepalive_path, util_threshold, mem_threshold_mib)
        except SSHUnavailable as e:
            if e.reason == SSHUnavailable.INSTANCE_NOT_RUNNING:
                if log:
                    log(f"实例已非 running（{e.status}），看门狗退出。")
                return e.status
            # 连不上但实例还在 running：记为未知，不累计空闲（保守，不误关）
            if log:
                log(f"  SSH 暂不可达[{e.reason}]，本轮跳过（不计空闲）")
            if once:
                return "unknown"
            time.sleep(interval)
            continue
        except APIError as e:
            # snapshot 等 API 瞬时错误：不能让看门狗崩溃，也不累计空闲
            if log:
                log(f"  API 暂时出错（{e}），本轮跳过")
            if once:
                return "unknown"
            time.sleep(interval)
            continue

        # once 模式只做一次判定、绝不关机（避免单次探测就触发 power_off）
        if once:
            return "idle" if idle else "active"
        now = time.time()
        if idle:
            if idle_start is None:
                idle_start = now
            elapsed = now - idle_start
            if log:
                log(f"  空闲 {int(elapsed)}/{need}s（{detail}）")
            if elapsed >= need:
                ctx.power_off_with_cost(uuid, log=log)
                if log:
                    log(f"实例 {uuid} 连续空闲，已自动关机。")
                return "powered_off"
        else:
            if idle_start is not None and log:
                log(f"  恢复活跃（{detail}），空闲计时清零")
            idle_start = None
        time.sleep(interval)


def balance_watch(ctx, warn_yuan, stop_yuan, interval=300, stop_mode="mine",
                  once=False, notify=None, log=print):
    """余额守护：低于 warn 通知；低于 stop 触发止损。
    stop_mode: mine（默认，只关我的 running 实例）| active（只关活动实例）| stop_all（全账号，慎）。"""
    notify = notify or (lambda msg: log(f"[通知] {msg}") if log else None)
    prev_bal, prev_t = None, None
    while True:
        try:
            bal = ctx.api.balance_yuan()
        except APIError as e:
            # 余额查询瞬时失败：守护进程绝不能因此退出，跳过本轮
            if log:
                log(f"  余额查询出错（{e}），本轮跳过")
            if once:
                return "error"
            time.sleep(interval)
            continue
        rate_txt = ""
        now = time.time()
        if prev_bal is not None and now > prev_t:
            rate = (prev_bal - bal) / ((now - prev_t) / 3600.0)  # 元/小时
            if rate > 0:
                hrs = (bal - stop_yuan) / rate
                rate_txt = f"，约 ¥{rate:.2f}/h，距急停线还能撑 ~{hrs:.1f}h"
        prev_bal, prev_t = bal, now
        if log:
            log(f"余额 ¥{bal:.2f}{rate_txt}")

        if bal < stop_yuan:
            notify(f"余额 ¥{bal:.2f} 低于急停线 ¥{stop_yuan:.2f}，执行止损（{stop_mode}）")
            if stop_mode == "active":
                a = ctx.reg.get_active()
                if a:
                    ctx.power_off_with_cost(a, log=log)
            else:
                ctx.stop_all_running(release=False, scope="all" if stop_mode == "stop_all" else "mine",
                                     log=log)
            return "stopped"
        if bal < warn_yuan:
            notify(f"余额 ¥{bal:.2f} 低于预警线 ¥{warn_yuan:.2f}{rate_txt}")
        if once:
            return "ok" if bal >= warn_yuan else "warned"
        time.sleep(interval)


def budget_guard(ctx, interval=300, once=False, notify=None, log=print):
    """个人预算守护：定期对账，(1) 今日/本周/本月花费触顶 → 关停**我的**全部 running 实例；
    (2) 某台连续开机超过 budget.max_session_hours → 只关那一台。绝不碰别人的实例。
    返回 "ok" | "over_budget" | "session_limit"（once 模式只判一次不动作，返回判定）。"""
    notify = notify or (lambda msg: log(f"[通知] {msg}") if log else None)
    b = ctx.cfg.budget
    while True:
        try:
            cost.reconcile(ctx, log=log)
            u = cost.usage(ctx)
        except APIError as e:
            if log:
                log(f"  对账出错（{e}），本轮跳过")
            if once:
                return "error"
            time.sleep(interval)
            continue
        if log:
            log(f"我的花费 今日 ¥{u['today']:.2f} / 本周 ¥{u['week']:.2f} / 本月 ¥{u['month']:.2f}，"
                f"running {u['running']} 台，¥{u['burn_rate']:.2f}/h")
        over = next((lab for key, cap, lab in (("today", b.daily_yuan, "今日"), ("week", b.weekly_yuan, "本周"),
                                                ("month", b.monthly_yuan, "本月")) if cap and u[key] >= cap), None)
        long_sessions = [s for s in u["sessions_open"]
                         if b.max_session_hours and s["hours"] >= b.max_session_hours]
        if once:
            return "over_budget" if over else ("session_limit" if long_sessions else "ok")
        if over:
            notify(f"{over}花费 ¥{u[{'今日': 'today', '本周': 'week', '本月': 'month'}[over]]:.2f} 触顶，关停我的全部 running 实例")
            ctx.stop_all_running(release=False, scope="mine", log=log)
            return "over_budget"
        for s in long_sessions:
            notify(f"实例 {s['instance_uuid']} 已连续开机 {s['hours']:.1f}h ≥ {b.max_session_hours:g}h，关机")
            try:
                ctx.power_off_with_cost(s["instance_uuid"], log=log)
            except APIError as e:
                if log:
                    log(f"  关机 {s['instance_uuid']} 失败: {e}")
        if long_sessions:
            return "session_limit"
        time.sleep(interval)
