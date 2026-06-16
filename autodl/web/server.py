"""FastAPI 后端：把 autodl 的能力暴露成 REST，供单页大盘调用。

设计要点：
- 复用一个全局 Context（api 已是 per-thread Session，registry 每次独立连接，线程安全）。
- 耗时操作（创建/开机实例）走 JobStore 后台线程 + 前端轮询，浏览器不卡。
- 提交任务用远端后台启动（run_background_command，瞬时返回 run_id）；前端轮询 run 详情时
  SSH 探活/读退出码，完成时自动从 metrics.json 抓取指标入库。
"""
from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_config, require_token
from ..core import Context
from ..errors import APIError, SSHUnavailable

_STATIC = Path(__file__).parent / "static"


def _billing(status):
    if status == "running":
        return "带卡计费"
    if status in ("shutdown", "power_off", "shutting_down"):
        return "仅磁盘计费"
    return status or "?"


def create_app(cfg=None):
    cfg = cfg or load_config()
    require_token(cfg)
    ctx = Context(cfg)
    from .jobs import JobStore
    jobs = JobStore()

    app = FastAPI(title="autodl dashboard", version="1.1.0")

    # ---------- 只读 ----------
    @app.get("/api/summary")
    def summary():
        try:
            bal = ctx.api.balance_yuan()
        except APIError:
            bal = None
        instances = ctx.api.list_instances()
        inst_by_status = {}
        for it in instances:
            inst_by_status[it.get("status")] = inst_by_status.get(it.get("status"), 0) + 1
        runs = ctx.reg.list_runs()
        run_by_status = {}
        for r in runs:
            run_by_status[r["status"]] = run_by_status.get(r["status"], 0) + 1
        durations = [(r["ended_at"] - r["started_at"]) for r in runs
                     if r.get("ended_at") and r.get("started_at")]
        return {
            "balance_yuan": round(bal, 2) if bal is not None else None,
            "instances_total": len(instances),
            "instances_by_status": inst_by_status,
            "runs_total": len(runs),
            "runs_by_status": run_by_status,
            "avg_duration_sec": round(sum(durations) / len(durations), 1) if durations else 0,
            "active_instance": ctx.reg.get_active(),
        }

    @app.get("/api/instances")
    def instances():
        active = ctx.reg.get_active()
        out = []
        for it in ctx.api.list_instances():
            uid = it.get("instance_uuid") or it.get("uuid")
            out.append({"instance_uuid": uid, "status": it.get("status"),
                        "billing": _billing(it.get("status")), "region": it.get("region_sign"),
                        "active": uid == active})
        return {"instances": out}

    @app.get("/api/stock")
    def stock(region: str = ""):
        regions = [region] if region else ctx.cfg.regions
        out = {}
        for rg in regions:
            try:
                data = ctx.api.gpu_stock(rg, cuda_v_from=ctx.cfg.cuda_v_from) or []
                out[rg] = {n: s for e in data for n, s in e.items()}
            except APIError as e:
                out[rg] = {"_error": str(e)}
        return out

    @app.get("/api/runs")
    def runs():
        allm = ctx.reg.all_metrics()
        out = []
        for r in ctx.reg.list_runs():
            cfg_d = json.loads(r.get("config_json") or "{}")
            dur = (r["ended_at"] - r["started_at"]) if r.get("ended_at") and r.get("started_at") else None
            out.append({"run_id": r["run_id"], "name": cfg_d.get("name") or r["run_id"],
                        "instance": r["instance_uuid"], "status": r["status"],
                        "exit_code": r["exit_code"], "started_at": r["started_at"],
                        "duration_sec": round(dur, 1) if dur else None,
                        "config": cfg_d.get("config", {}), "metrics": allm.get(r["run_id"], {})})
        out.sort(key=lambda x: x["started_at"] or 0, reverse=True)
        return {"runs": out}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str, lines: int = 80):
        r = ctx.reg.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        cfg_d = json.loads(r.get("config_json") or "{}")
        log, note = "", ""
        uuid = r["instance_uuid"]
        # 若仍 running 且有 pid/exit_file，尝试实时探活 + 完成时抓指标
        if r["status"] == "running" and r.get("exit_file"):
            try:
                snap = ctx.api.snapshot(uuid)
                state, code = ctx.ssh.poll(snap, {"pid": r.get("pid"), "exit_file": r["exit_file"]}, uuid)
                if state == "done":
                    ctx.reg.finish_run(run_id, code if code is not None else -1)
                    _autopull_metrics(ctx, snap, uuid, run_id, cfg_d.get("metrics_file"))
                    r = ctx.reg.get_run(run_id)
                if r.get("log_path"):
                    log = ctx.ssh.tail(snap, r["log_path"], lines=lines, instance_uuid=uuid)
            except SSHUnavailable as e:
                note = f"实例 SSH 不可达（{e.reason}），仅显示已存状态"
            except APIError as e:
                note = f"API 出错：{e}"
        return {"run_id": run_id, "status": r["status"], "exit_code": r["exit_code"],
                "instance": uuid, "config": cfg_d, "metrics": ctx.reg.get_metrics(run_id),
                "log": log, "note": note}

    @app.get("/api/metrics")
    def metrics():
        allm = ctx.reg.all_metrics()
        names = {r["run_id"]: json.loads(r.get("config_json") or "{}").get("name") or r["run_id"]
                 for r in ctx.reg.list_runs()}
        keys = sorted({k for m in allm.values() for k in m})
        rows = [{"run_id": rid, "name": names.get(rid, rid), "metrics": m} for rid, m in allm.items()]
        return {"keys": keys, "rows": rows}

    # ---------- 变更（快） ----------
    @app.post("/api/instances/{uuid}/{action}")
    def instance_action(uuid: str, action: str):
        try:
            if action == "power_on":
                ctx.api.power_on(uuid); ctx.reg.upsert_instance(uuid, status_cached="starting")
            elif action == "power_off":
                ctx.power_off_with_cost(uuid, log=None)
            elif action == "release":
                ctx.api.power_off(uuid); time.sleep(2)
                ctx.api.release(uuid); ctx.reg.remove_instance(uuid)
                if ctx.reg.get_active() == uuid:
                    ctx.reg.set_active(None)
            else:
                raise HTTPException(400, f"未知动作 {action}")
        except APIError as e:
            raise HTTPException(502, str(e))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/metrics")
    def record_metrics(run_id: str, payload: dict = Body(...)):
        ctx.reg.record_metrics(run_id, payload or {})
        return {"ok": True, "metrics": ctx.reg.get_metrics(run_id)}

    # ---------- 耗时（后台 job + 轮询） ----------
    @app.post("/api/up")
    def up(payload: dict = Body(default={})):
        select_region = bool(payload.get("select_region"))

        def task(log):
            uuid, snap = ctx.ensure_instance(select_region=select_region, log=log)
            key = ctx.ssh.ensure_key_access(snap, uuid)
            alias = ctx.ssh.write_ssh_config(snap, identity_file=key or None)
            return {"instance": uuid, "ssh": f"ssh {alias}",
                    "host": snap["proxy_host"], "port": snap["ssh_port"]}
        return {"job_id": jobs.submit(task)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    @app.post("/api/run")
    def run(payload: dict = Body(...)):
        uuid = payload.get("instance") or ctx.reg.get_active()
        if not uuid:
            raise HTTPException(400, "未指定实例，且无活动实例（请先 up 开机）")
        st = ctx.api.status_or_none(uuid)
        if st != "running":
            raise HTTPException(409, f"实例状态 {st}，请先开机（up / 实例页 power_on）")
        # 构造远端命令
        if payload.get("remote_script"):
            rs = payload["remote_script"]
            command = f"cd {shlex.quote(str(Path(rs).parent) or '.')} && bash {shlex.quote(Path(rs).name)}"
        elif payload.get("remote"):
            command = payload["remote"]
        elif payload.get("script_text"):
            command = None  # 走上传脚本
        else:
            raise HTTPException(400, "需提供 remote_script / remote / script_text 之一")
        run_id = payload.get("name") or f"run-{int(time.time())}"
        run_id = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in run_id)
        wd = ctx.cfg.ssh.remote_workdir
        metrics_file = payload.get("metrics_file") or f"{wd}/metrics.json"
        cfg_d = {"name": payload.get("name") or run_id, "config": payload.get("config", {}),
                 "metrics_file": metrics_file, "command": command or "(script)"}
        try:
            snap = ctx.api.snapshot(uuid)
            if command is None:
                meta = ctx.ssh.run_background(snap, payload["script_text"], run_id, uuid)
            else:
                meta = ctx.ssh.run_background_command(snap, command, run_id, uuid)
        except SSHUnavailable as e:
            raise HTTPException(502, f"任务启动失败：{e}")
        ctx.reg.record_run(run_id, uuid, cfg_d, meta["log"], meta["exit_file"], meta["pid"])
        return {"run_id": run_id, "pid": meta["pid"]}

    # ---------- 静态前端 ----------
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(_STATIC / "index.html"))

    return app


def _autopull_metrics(ctx, snap, uuid, run_id, metrics_file):
    if not metrics_file:
        return
    try:
        out, _e, _c = ctx.ssh.run(snap, f"cat {shlex.quote(metrics_file)} 2>/dev/null || true", uuid)
        if out.strip():
            data = json.loads(out)
            if isinstance(data, dict):
                ctx.reg.record_metrics(run_id, data)
    except (SSHUnavailable, APIError, ValueError):
        pass


def serve(host="127.0.0.1", port=8848, cfg=None):
    import uvicorn
    uvicorn.run(create_app(cfg), host=host, port=port)
