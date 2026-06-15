"""批量并行调度器：把 N 个任务铺到至多 max_parallel 台实例上跑。

- 每个 worker 独占一台实例，从共享队列里取任务跑，一个跑完取下一个（实例复用）。
- 任务结果记入 registry 的 runs 表（run_id = "<batch_id>:<job_id>"），支持 resume（已成功的跳过）。
- on_finish 控制收尾：release（释放）/ power_off（关机保留）/ keep（不动）。
- 并发安全：队列用 queue.Queue；registry 每次操作独立 sqlite 连接（WAL+busy_timeout）。
  worker 各自管自己的实例，不碰单值 active 实例，避免互相打架。
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .errors import AutoDLError, SSHUnavailable


def _safe_power_off(ctx, uuid, log, widx):
    try:
        ctx.api.power_off(uuid)
        ctx.reg.upsert_instance(uuid, status_cached="shutdown")
        return True
    except AutoDLError as e:
        if log:
            log(f"[w{widx}] ⚠️ 关机 {uuid} 失败: {e}")
        return False


def _safe_release(ctx, uuid, log, widx, retries=3):
    """尽力释放：先关机，再带重试地 release。彻底失败不静默——保留 registry 记录并显著告警。"""
    _safe_power_off(ctx, uuid, log, widx)
    for a in range(retries):
        time.sleep(15 if a == 0 else 8)  # 给关机留时间，release 前需已 shutdown
        try:
            ctx.api.release(uuid)
            ctx.reg.remove_instance(uuid)
            if log:
                log(f"[w{widx}] 实例 {uuid} 已释放")
            return True
        except AutoDLError as e:
            if log:
                log(f"[w{widx}] release {uuid} 第{a + 1}/{retries}次失败: {e}")
    # 保留 registry 记录（带 tags），便于事后 `autodl stop-all --release` 兜底
    if log:
        log(f"[w{widx}] ⚠️⚠️ 实例 {uuid} 释放失败、仍在计费！请尽快 `autodl stop-all --release` 或去控制台处理。")
    return False


def _finish_instance(ctx, uuid, on_finish, log, widx):
    if on_finish == "keep":
        return
    if on_finish == "release":
        _safe_release(ctx, uuid, log, widx)
    else:  # power_off
        _safe_power_off(ctx, uuid, log, widx)


def _run_job(ctx, snap, uuid, job, batch_id, retries, log, widx):
    rid = f"{batch_id}:{job['id']}"
    # 开始时记一次（正确的 started_at，重试不重置）；结束时 finish_run 一次
    ctx.reg.record_run(rid, uuid, {k: job.get(k) for k in ("command", "id")}, "(sync)", "", "")
    attempts = retries + 1
    code, err = None, ""
    for a in range(attempts):
        try:
            if job.get("script_text"):
                _out, err, code = ctx.ssh.run_script(snap, job["script_text"], rid.replace(":", "_"), uuid)
            else:
                _out, err, code = ctx.ssh.run(snap, job["command"], uuid)
        except SSHUnavailable as e:
            code, err = -1, str(e)
        if code == 0:
            break
        if log and a + 1 < attempts:
            log(f"[w{widx}] job {job['id']} 第{a+1}次失败(code={code})，重试")
    ctx.reg.finish_run(rid, code if code is not None else -1)
    if log:
        log(f"[w{widx}] job {job['id']} -> exit {code}")
    return code


def run_batch(ctx, jobs, max_parallel=2, on_finish="release", select_region=True,
              resume=True, batch_id="batch", job_retries=0, log=print):
    # resume：跳过已成功的任务
    pending, skipped = [], []
    for j in jobs:
        if resume:
            r = ctx.reg.get_run(f"{batch_id}:{j['id']}")
            if r and r.get("status") == "succeeded":
                skipped.append(j["id"])
                continue
        pending.append(j)
    if log:
        log(f"批次 {batch_id}: 待跑 {len(pending)}，跳过(已成功) {len(skipped)}，并发上限 {max_parallel}")

    q = queue.Queue()
    for j in pending:
        q.put(j)
    results, rlock = [], threading.Lock()
    created, clock = [], threading.Lock()  # 已创建实例，供中断时兜底释放
    create_lock = threading.Lock()         # 串行化"选区+创建"，避免多 worker 抢同一稀缺区(TOCTOU)

    def add(d):
        with rlock:
            results.append(d)

    def worker(widx):
        uuid, snap = None, None
        try:
            try:
                if select_region:
                    with create_lock:  # 选区与创建之间不被其它 worker 插入
                        region = ctx.select_region(log=None)
                        uuid = ctx.api.create_in_region([region]) if region else ctx.api.create()
                else:
                    uuid = ctx.api.create()
                with clock:
                    created.append(uuid)
                ctx.reg.upsert_instance(uuid, name=f"{ctx.cfg.instance_name}-{batch_id}",
                                        tags=batch_id, status_cached="creating")
                snap = ctx.api.wait_running(uuid, log=None)
                ctx.reg.upsert_instance(uuid, status_cached="running", region=snap.get("region_sign"))
                if log:
                    log(f"[w{widx}] 实例就绪 {uuid}")
            except AutoDLError as e:
                if log:
                    log(f"[w{widx}] 创建实例失败，放弃该 worker: {e}")
                if uuid:  # 创建成功但 wait 失败 → 尽力释放（power_off/release 各自重试，不静默）
                    _safe_release(ctx, uuid, log, widx)
                return
            while True:
                try:
                    job = q.get_nowait()
                except queue.Empty:
                    break
                try:
                    code = _run_job(ctx, snap, uuid, job, batch_id, job_retries, log, widx)
                    add({"id": job["id"], "instance": uuid, "exit_code": code,
                         "status": "succeeded" if code == 0 else "failed"})
                except Exception as e:  # noqa: BLE001 - 单个任务异常不应吞掉/卡队列
                    add({"id": job["id"], "instance": uuid, "exit_code": None, "status": "failed"})
                    if log:
                        log(f"[w{widx}] job {job['id']} 未预期异常: {e}")
                finally:
                    q.task_done()
        finally:
            if uuid:
                _finish_instance(ctx, uuid, on_finish, log, widx)

    n_workers = min(max_parallel, len(pending)) if pending else 0
    if n_workers:
        try:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                for i in range(n_workers):
                    ex.submit(worker, i)
                ex.shutdown(wait=True)
        except BaseException as e:  # noqa: BLE001 - 含 KeyboardInterrupt：兜底释放已创建实例
            if log:
                log(f"批次中断（{type(e).__name__}），尽力释放已创建实例…")
            with clock:
                to_clean = list(created)
            for u in to_clean:
                if ctx.api.status_or_none(u) not in (None, "removed"):
                    _safe_release(ctx, u, log, "X")
            raise

    # 没被消费的（worker 全部创建失败时）标记 not_run
    while True:
        try:
            j = q.get_nowait()
        except queue.Empty:
            break
        add({"id": j["id"], "instance": None, "exit_code": None, "status": "not_run"})
    for sid in skipped:
        add({"id": sid, "instance": None, "exit_code": 0, "status": "skipped"})
    return results
