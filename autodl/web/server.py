"""FastAPI 后端：把 autodl 的能力暴露成 REST，供单页大盘调用。

设计要点：
- 复用一个全局 Context（api 已是 per-thread Session，registry 每次独立连接，线程安全）。
- 耗时操作（创建/开机实例）走 JobStore 后台线程 + 前端轮询，浏览器不卡。
- 实验(experiment)是可复用的定义：名称/标签/YAML配置/执行方式/要抓取的指标/实例规格。
  每次"运行"产生一个 run，完成时按 metrics_spec 从 metrics.json 与日志(正则)抓取 ASR/Accuracy 等。
"""
from __future__ import annotations

import json
import threading
import time

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .. import tasks
from ..api import GPU_SPECS, GPU_SPEC_LABELS, REGIONS
from ..config import load_config, require_token
from ..core import Context, billing_class
from ..errors import APIError, SSHUnavailable

_STATIC = Path(__file__).parent / "static"


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _slug(s):
    """实验 id：保留 unicode 字母数字（中文可读），仅替换空格/符号。"""
    return "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in str(s))[:60] or "exp"


def create_app(cfg=None):
    cfg = cfg or load_config()
    require_token(cfg)
    ctx = Context(cfg)
    from .jobs import JobStore
    jobs = JobStore()

    app = FastAPI(title="autodl dashboard", version="1.4.0")

    # ---------- 元信息：GPU 规格 / 区域（给创建表单用）----------
    @app.get("/api/meta")
    def meta():
        return {"gpu_specs": GPU_SPECS, "regions": REGIONS,
                "default": {"gpu_spec_uuid": cfg.gpu_spec_uuid, "image_uuid": cfg.image_uuid,
                            "cuda_v_from": cfg.cuda_v_from, "expand_disk_gb": cfg.expand_disk_gb,
                            "regions": cfg.regions, "workdir": cfg.ssh.remote_workdir}}

    # ---------- 概览 ----------
    @app.get("/api/summary")
    def summary():
        try:
            bal = ctx.api.balance_yuan()
        except APIError:
            bal = None
        instances = ctx.api.list_instances()
        inst_by = {}
        for it in instances:
            inst_by[it.get("status")] = inst_by.get(it.get("status"), 0) + 1
        runs = ctx.reg.list_runs()
        run_by = {}
        for r in runs:
            run_by[r["status"]] = run_by.get(r["status"], 0) + 1
        durs = [(r["ended_at"] - r["started_at"]) for r in runs if r.get("ended_at") and r.get("started_at")]
        return {"balance_yuan": round(bal, 2) if bal is not None else None,
                "instances_total": len(instances), "instances_by_status": inst_by,
                "runs_total": len(runs), "runs_by_status": run_by,
                "experiments_total": len(ctx.reg.list_experiments()),
                "avg_duration_sec": round(sum(durs) / len(durs), 1) if durs else 0,
                "active_instance": ctx.reg.get_active()}

    # ---------- 实例 ----------
    @app.get("/api/instances")
    def instances():
        active = ctx.reg.get_active()
        out = []
        for it in ctx.api.list_instances():
            uid = it.get("instance_uuid") or it.get("uuid")
            spec = it.get("gpu_spec_uuid")
            out.append({"instance_uuid": uid, "status": it.get("status"),
                        "billing": billing_class(it.get("status")),
                        "gpu": GPU_SPEC_LABELS.get(spec, spec or "?"), "gpu_spec": spec,
                        "gpu_amount": it.get("req_gpu_amount"),
                        "region": it.get("region_name") or it.get("region_sign"),
                        "name": it.get("name"), "active": uid == active})
        return {"instances": out}

    @app.get("/api/stock")
    def stock(region: str = "", gpu_name: str = ""):
        regions = [region] if region else cfg.regions
        out = {}
        for rg in regions:
            try:
                body_names = [gpu_name] if gpu_name else None
                data = ctx.api.gpu_stock(rg, cuda_v_from=cfg.cuda_v_from) or []
                items = {n: s for e in data for n, s in e.items()}
                if body_names:
                    items = {k: v for k, v in items.items() if gpu_name.lower() in k.lower()}
                out[rg] = items
            except APIError as e:
                out[rg] = {"_error": str(e)}
        return out

    @app.post("/api/instances/{uuid}/{action}")
    def instance_action(uuid: str, action: str):
        try:
            if action == "power_on":
                ctx.api.power_on(uuid); ctx.reg.upsert_instance(uuid, status_cached="starting")
            elif action == "power_off":
                ctx.power_off_with_cost(uuid, log=None)
            elif action == "release":
                with ctx.reg.lock():
                    if not ctx.finish_instance(uuid, "release", log=None):
                        raise HTTPException(502, f"释放 {uuid} 失败，实例可能仍在计费，"
                                                 "请稍后重试或去控制台处理")
            else:
                raise HTTPException(400, f"未知动作 {action}")
        except APIError as e:
            raise HTTPException(502, str(e))
        return {"ok": True}

    @app.post("/api/create")
    def create(payload: dict = Body(...)):
        """按表单参数创建实例（后台 job）。"""
        amount = _int(payload.get("req_gpu_amount"), 1)
        if not 1 <= amount <= 4:
            raise HTTPException(400, "GPU 数量需为 1–4")
        disk = _int(payload.get("expand_disk_gb"), cfg.expand_disk_gb)
        if not 0 <= disk <= 500:
            raise HTTPException(400, "系统盘扩容需为 0–500 GB")
        opts = dict(
            gpu_spec_uuid=payload.get("gpu_spec_uuid"), req_gpu_amount=amount, expand_disk_gb=disk,
            data_center_list=[payload["region"]] if payload.get("region") else None,
            image_uuid=payload.get("image_uuid") or None,
            cuda_v_from=_int(payload.get("cuda_v_from"), cfg.cuda_v_from),
            instance_name=payload.get("instance_name") or None,
        )

        def task(log):
            log(f"创建实例：{opts.get('gpu_spec_uuid')} x{opts['req_gpu_amount']} @ {opts.get('data_center_list')}")
            with ctx.reg.lock():
                uuid = ctx.api.create(**opts)
                ctx.reg.upsert_instance(uuid, name=opts.get("instance_name") or cfg.instance_name,
                                        gpu_spec=opts.get("gpu_spec_uuid"), status_cached="creating")
                ctx.reg.set_active(uuid)
                ctx.reg.upsert_instance(uuid, last_balance=_safe_balance(ctx))
            try:
                snap = ctx.api.wait_running(uuid, log=log)
                ctx.reg.upsert_instance(uuid, status_cached="running", region=snap.get("region_sign"))
                key = ctx.ssh.ensure_key_access(snap, uuid)
                alias = ctx.ssh.write_ssh_config(snap, identity_file=key or None)
                return {"instance": uuid, "ssh": f"ssh {alias}", "host": snap["proxy_host"], "port": snap["ssh_port"]}
            except Exception as e:  # noqa: BLE001 - 已创建实例若后续失败必须止损，绝不泄漏带卡计费
                try:
                    ctx.api.power_off(uuid)
                    ctx.reg.upsert_instance(uuid, status_cached="shutdown")
                    log(f"⚠️ 实例 {uuid} 已创建但初始化失败，已自动关机止损")
                except Exception:  # noqa: BLE001
                    log(f"⚠️⚠️ 实例 {uuid} 已创建但初始化失败、且关机也失败，请尽快去实例页释放！")
                raise RuntimeError(f"实例 {uuid} 初始化失败（已尝试关机）：{e}")
        return {"job_id": jobs.submit(task)}

    @app.post("/api/up")
    def up(payload: dict = Body(default={})):
        select_region = bool(payload.get("select_region"))

        def task(log):
            try:
                uuid, snap = ctx.ensure_instance(select_region=select_region, log=log)
            except Exception as e:  # noqa: BLE001 - 新建后若失败需止损
                a = ctx.reg.get_active()
                if a and ctx.api.status_or_none(a) in ("starting", "creating", "sys_volume_creating"):
                    try:
                        ctx.api.power_off(a); ctx.reg.upsert_instance(a, status_cached="shutdown")
                        log(f"⚠️ 实例 {a} 创建后初始化失败，已自动关机止损")
                    except Exception:  # noqa: BLE001
                        log(f"⚠️⚠️ 实例 {a} 可能已创建且仍在计费，请去实例页释放！")
                raise
            key = ctx.ssh.ensure_key_access(snap, uuid)
            alias = ctx.ssh.write_ssh_config(snap, identity_file=key or None)
            return {"instance": uuid, "ssh": f"ssh {alias}", "host": snap["proxy_host"], "port": snap["ssh_port"]}
        return {"job_id": jobs.submit(task)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    # ---------- 实验（可复用定义）----------
    @app.get("/api/tags")
    def tags():
        return {"tags": ctx.reg.list_tags()}

    @app.get("/api/experiments")
    def experiments():
        allm = ctx.reg.all_metrics()
        runs = ctx.reg.list_runs()
        last_by_exp = {}
        for r in runs:
            eid = r.get("experiment_id")
            if eid and (eid not in last_by_exp or (r["started_at"] or 0) > (last_by_exp[eid]["started_at"] or 0)):
                last_by_exp[eid] = r
        out = []
        for e in ctx.reg.list_experiments():
            lr = last_by_exp.get(e["experiment_id"])
            out.append({"experiment_id": e["experiment_id"], "name": e["name"], "tag": e["tag"],
                        "exec_mode": e["exec_mode"], "updated_at": e["updated_at"],
                        "last_run": lr["run_id"] if lr else None,
                        "last_status": lr["status"] if lr else None,
                        "last_metrics": allm.get(lr["run_id"], {}) if lr else {}})
        return {"experiments": out}

    @app.get("/api/experiments/{eid}")
    def experiment_get(eid: str):
        e = ctx.reg.get_experiment(eid)
        if not e:
            raise HTTPException(404, "experiment not found")
        e["metrics_spec"] = json.loads(e.get("metrics_spec") or "[]")
        e["instance_pref"] = json.loads(e.get("instance_pref") or "{}")
        runs = [r for r in ctx.reg.list_runs() if r.get("experiment_id") == eid]
        allm = ctx.reg.all_metrics()
        e["runs"] = [{"run_id": r["run_id"], "status": r["status"], "exit_code": r["exit_code"],
                      "started_at": r["started_at"],
                      "duration_sec": round(r["ended_at"] - r["started_at"], 1)
                      if r.get("ended_at") and r.get("started_at") else None,
                      "metrics": allm.get(r["run_id"], {})}
                     for r in sorted(runs, key=lambda x: x["started_at"] or 0, reverse=True)]
        return e

    @app.post("/api/experiments")
    def experiment_save(payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "实验名必填")
        # 客户端传入的 experiment_id 也必须过 slug（去掉引号/HTML，杜绝注入/XSS）
        eid = _slug(payload.get("experiment_id") or name)
        pref = payload.get("instance_pref") or {}
        if isinstance(pref, dict):  # 数值字段强转，防止生成脚本/创建时被注入
            for k in ("req_gpu_amount", "expand_disk_gb", "cuda_v_from"):
                if pref.get(k) is not None:
                    pref[k] = _int(pref[k], None)
        ctx.reg.upsert_experiment(
            eid, name=name, tag=(payload.get("tag") or "").strip(),
            config_yaml=payload.get("config_yaml") or "",
            exec_mode=payload.get("exec_mode") or "remote_script",
            exec_value=payload.get("exec_value") or "",
            metrics_spec=json.dumps(payload.get("metrics_spec") or [], ensure_ascii=False),
            instance_pref=json.dumps(pref, ensure_ascii=False),
        )
        return {"experiment_id": eid}

    @app.delete("/api/experiments/{eid}")
    def experiment_delete(eid: str):
        ctx.reg.delete_experiment(eid)
        return {"ok": True}

    @app.post("/api/experiments/{eid}/run")
    def experiment_run(eid: str, payload: dict = Body(default={})):
        e = ctx.reg.get_experiment(eid)
        if not e:
            raise HTTPException(404, "experiment not found")
        uuid = payload.get("instance") or ctx.reg.get_active()
        if not uuid:
            raise HTTPException(400, "未指定实例，且无活动实例（请先创建/开机）")
        if ctx.api.status_or_none(uuid) != "running":
            raise HTTPException(409, "实例非 running，请先开机")
        # make_run_id 自带时间戳+高熵后缀：不同(中文)实验或同秒重复运行也不会覆盖/串台
        run_id = tasks.make_run_id(eid)
        try:
            snap = ctx.api.snapshot(uuid)
            res = tasks.run_background(
                ctx, snap, uuid, mode=e["exec_mode"], value=e["exec_value"], run_id=run_id,
                name=e["name"], config_yaml=e.get("config_yaml") or None,
                metrics_spec=json.loads(e.get("metrics_spec") or "[]"),
                experiment_id=eid, tag=e.get("tag"),
            )
        except (SSHUnavailable, APIError, KeyError, ValueError) as ex:
            raise HTTPException(502, f"任务启动失败：{ex}")
        return {"run_id": run_id, "pid": res["pid"]}

    @app.get("/api/experiments/{eid}/submit_script")
    def experiment_script(eid: str):
        e = ctx.reg.get_experiment(eid)
        if not e:
            raise HTTPException(404, "experiment not found")
        return PlainTextResponse(_generate_submit_script(e))

    # ---------- runs / 指标 ----------
    @app.get("/api/runs")
    def runs():
        allm = ctx.reg.all_metrics()
        out = []
        for r in ctx.reg.list_runs():
            cfg_d = json.loads(r.get("config_json") or "{}")
            dur = (r["ended_at"] - r["started_at"]) if r.get("ended_at") and r.get("started_at") else None
            out.append({"run_id": r["run_id"], "name": cfg_d.get("name") or r["run_id"],
                        "experiment_id": r.get("experiment_id"), "tag": r.get("tag"),
                        "instance": r["instance_uuid"], "status": r["status"], "exit_code": r["exit_code"],
                        "started_at": r["started_at"], "duration_sec": round(dur, 1) if dur else None,
                        "metrics": allm.get(r["run_id"], {})})
        out.sort(key=lambda x: x["started_at"] or 0, reverse=True)
        return {"runs": out}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str, lines: int = 100):
        r = ctx.reg.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        info = tasks.refresh_run(ctx, r, lines=lines)  # 探活/tail/完成登记/抓指标一步到位
        if info["state"] == "done":
            r = ctx.reg.get_run(run_id) or r
        cfg_d = json.loads(r.get("config_json") or "{}")
        return {"run_id": run_id, "status": r["status"], "exit_code": r["exit_code"],
                "instance": r["instance_uuid"],
                "experiment_id": r.get("experiment_id"), "tag": r.get("tag"), "config": cfg_d,
                "metrics": ctx.reg.get_metrics(run_id), "series": ctx.reg.get_metric_series(run_id),
                "log": info["log"], "note": info["note"]}

    @app.post("/api/runs/{run_id}/metrics")
    def record_metrics(run_id: str, payload: dict = Body(...)):
        step = payload.pop("_step", None) if isinstance(payload, dict) else None
        ctx.reg.record_metrics(run_id, payload or {}, step=step)
        return {"ok": True, "metrics": ctx.reg.get_metrics(run_id)}

    @app.get("/api/metrics")
    def metrics():
        allm = ctx.reg.all_metrics()
        runs = {r["run_id"]: r for r in ctx.reg.list_runs()}
        keys = sorted({k for m in allm.values() for k in m})
        rows = []
        for rid, m in allm.items():
            r = runs.get(rid, {})
            cfg_d = json.loads(r.get("config_json") or "{}")
            rows.append({"run_id": rid, "name": cfg_d.get("name") or rid,
                         "tag": r.get("tag"), "experiment_id": r.get("experiment_id"), "metrics": m})
        return {"keys": keys, "rows": rows, "tags": ctx.reg.list_tags()}

    # ---------- 静态前端 ----------
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(_STATIC / "index.html"))

    # ---------- 后台对账：让 run 即使没人打开详情也能完成并抓取指标 ----------
    def _reconcile_loop():
        while True:
            time.sleep(20)
            try:
                _reconcile_running(ctx)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_reconcile_loop, daemon=True).start()

    return app


def _reconcile_running(ctx):
    """对所有 status==running 且有 exit_file 的 run 做一次轻量 poll；完成则 finish + 抓指标。
    refresh_run 自己消化 SSH/API 错误（实例关机/释放时跳过，不误判完成）。"""
    for r in ctx.reg.list_runs():
        if r.get("status") != "running" or not r.get("exit_file"):
            continue
        try:
            tasks.refresh_run(ctx, r, lines=0)
        except Exception:  # noqa: BLE001 - 对账线程绝不能崩
            continue


# ---------------- 模块级辅助 ----------------
def _safe_balance(ctx):
    try:
        return ctx.api.balance_yuan()
    except APIError:
        return None


def _generate_submit_script(e):
    """生成独立可运行的提交脚本：只是 tasks.submit() 的一层参数壳，逻辑不再复制。"""
    pref = json.loads(e.get("instance_pref") or "{}")
    name, tag = e["name"], e.get("tag") or ""
    slug = tasks.ascii_id(name)
    spec = json.loads(e.get("metrics_spec") or "[]")
    overrides = []
    if pref.get("gpu_spec_uuid"):
        overrides.append(f"cfg.gpu_spec_uuid = {json.dumps(pref['gpu_spec_uuid'])}")
    if pref.get("req_gpu_amount") is not None:
        overrides.append(f"cfg.req_gpu_amount = {_int(pref['req_gpu_amount'], 1)}")
    if pref.get("expand_disk_gb") is not None:
        overrides.append(f"cfg.expand_disk_gb = {_int(pref['expand_disk_gb'], 10)}")
    if pref.get("cuda_v_from") is not None:
        overrides.append(f"cfg.cuda_v_from = {_int(pref['cuda_v_from'], 111)}")
    if pref.get("image_uuid"):
        overrides.append(f"cfg.image_uuid = {json.dumps(pref['image_uuid'])}")
    if pref.get("region"):
        overrides.append(f"cfg.data_center_list = [{json.dumps(pref['region'])}]")
    override_block = "\n".join(overrides) or "# （无覆盖，全部用 autodl.yaml 默认）"
    return f'''#!/usr/bin/env python3
"""自动生成 · 实验「{name}」(tag={tag}) 提交脚本。

前置：pip install "autodl-task-submit"，且 .env 里有 AUTODL_TOKEN。
运行：python {slug}_submit.py
流程：余额护栏 -> 创建/复用实例 -> 写 config.yaml -> 跑实验（实时回显、记入本地台账）
      -> 抓取 metrics.json/日志指标 -> 关机（保留实例以便复用）。
"""
import json

from autodl import tasks
from autodl.config import load_config, require_token
from autodl.core import Context

cfg = load_config()
require_token(cfg)
# —— 实例规格（来自实验定义，可改；未列出的用 autodl.yaml 默认）——
{override_block}

ctx = Context(cfg)
res = tasks.submit(
    ctx,
    mode={json.dumps(e["exec_mode"])},
    value={json.dumps(e["exec_value"], ensure_ascii=False)},
    name={json.dumps(name, ensure_ascii=False)},
    config_yaml={json.dumps(e.get("config_yaml") or "", ensure_ascii=False)} or None,
    metrics_spec={json.dumps(spec, ensure_ascii=False)},
    experiment_id={json.dumps(e["experiment_id"], ensure_ascii=False)},
    tag={json.dumps(tag, ensure_ascii=False)} or None,
    teardown="power_off",  # 跑完关机省 GPU 费（下次自动开机复用）；要彻底释放改成 "release"
    stream=lambda chunk: print(chunk, end="", flush=True),
)
print("退出码:", res["exit_code"])
metrics = ctx.reg.get_metrics(res["run_id"])
if metrics:
    print("指标:", json.dumps(metrics, ensure_ascii=False))
'''


def serve(host="127.0.0.1", port=8848, cfg=None):
    import uvicorn
    uvicorn.run(create_app(cfg), host=host, port=port)
