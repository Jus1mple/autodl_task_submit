"""批量并行调度器：把 N 个任务铺到至多 max_parallel 台实例上跑。

- 每个 worker 独占一台实例，从共享队列里取任务跑，一个跑完取下一个（实例复用）。
- 任务执行/记台账/抓指标走 tasks.run_foreground（与 CLI run、web 同一条管线），
  run_id = "<batch_id>:<job_id>"，支持 resume（已成功的跳过）。
- 收尾统一走 Context.finish_instance：release（释放）/ power_off（关机保留）/ keep（不动）。
- 并发安全：队列用 queue.Queue；registry 每次操作独立 sqlite 连接（WAL+busy_timeout）。
  worker 各自管自己的实例，不碰单值 active 实例，避免互相打架。
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from . import tasks
from .errors import AutoDLError, SSHUnavailable


def _run_job(ctx, snap, uuid, job, batch_id, retries, log, widx):
    """跑一个 job（记台账 + 重试 + 抓指标都在 tasks.run_foreground 里）。返回退出码。"""
    rid = f"{batch_id}:{job['id']}"
    wlog = (lambda m: log(f"[w{widx}] {m}")) if log else None
    try:
        res = tasks.run_foreground(
            ctx, snap, uuid, mode=job["mode"], value=job["value"], run_id=rid,
            name=job["id"], retries=retries, tag=batch_id,
            config_extra={"script": job["display"]} if job.get("display") else None,
            log=wlog,
        )
        code = res["exit_code"]
    except SSHUnavailable as e:
        code = -1
        if wlog:
            wlog(f"job {job['id']} SSH 失败: {e}")
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
        wlog = (lambda m: log(f"[w{widx}] {m}")) if log else None
        try:
            try:
                if select_region:
                    with create_lock:  # 选区与创建之间不被其它 worker 插入
                        region = ctx.select_region(log=None)
                        uuid = ctx.api.create(data_center_list=[region] if region else None)
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
                if uuid:  # 创建成功但 wait 失败 → 尽力释放（失败会显著告警，不静默）
                    ctx.finish_instance(uuid, "release", log=wlog)
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
                ctx.finish_instance(uuid, on_finish, log=wlog)

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
                    ctx.finish_instance(u, "release", log=log)
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
