"""FastAPI 后端：把 autodl 的能力暴露成 REST，供单页大盘调用。

设计要点：
- 复用一个全局 Context（api 已是 per-thread Session，registry 每次独立连接，线程安全）。
- 耗时操作（创建/开机实例）走 JobStore 后台线程 + 前端轮询，浏览器不卡。
- 实验(experiment)是可复用的定义：名称/标签/YAML配置/执行方式/要抓取的指标/实例规格。
  每次"运行"产生一个 run，完成时按 metrics_spec 从 metrics.json 与日志(正则)抓取 ASR/Accuracy 等。
"""
from __future__ import annotations

import base64
import json
import posixpath
import re
import shlex
import threading
import time
import uuid as uuidlib

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from ..api import GPU_SPECS, GPU_SPEC_LABELS, REGIONS
from ..config import load_config, require_token
from ..core import Context
from ..errors import APIError, SSHUnavailable

_STATIC = Path(__file__).parent / "static"


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _billing(status):
    if status == "running":
        return "带卡计费"
    if status in ("shutdown", "power_off", "shutting_down"):
        return "仅磁盘计费"
    return status or "?"


def _build_command(mode, value):
    """remote_script/remote → 远端命令字符串；script_text → None（走上传）。"""
    if mode == "remote_script":
        p = value.strip()
        # 用双引号让远端 shell 展开 ~ / $HOME（单引号会导致 ~ 不展开、cd 失败）
        if p.startswith("~/"):
            p = "$HOME/" + p[2:]
        elif p == "~":
            p = "$HOME"
        d = posixpath.dirname(p) or "."
        n = posixpath.basename(p)
        return f'cd "{d}" && bash "{n}"'
    if mode == "remote":
        return value
    return None


def _slug(s):
    """实验 id：保留 unicode 字母数字（中文可读），仅替换空格/符号。"""
    return "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in str(s))[:60] or "exp"


def _ascii_id(s):
    """远端 run_id：必须 ASCII（ssh._safe_run_id 只允许 [A-Za-z0-9_.-]）。中文等会被剥成可读前缀。"""
    out = "".join(ch if ((ch.isascii() and ch.isalnum()) or ch in "_.-") else "_" for ch in str(s))[:50]
    return out.strip("_") or "exp"


def create_app(cfg=None):
    cfg = cfg or load_config()
    require_token(cfg)
    ctx = Context(cfg)
    from .jobs import JobStore
    jobs = JobStore()

    app = FastAPI(title="autodl dashboard", version="1.2.0")

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
                        "billing": _billing(it.get("status")),
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
                    try:
                        ctx.api.power_off(uuid)
                    except APIError:
                        pass
                    for _ in range(8):  # 等实例真正关机再释放（release 前需 shutdown）
                        time.sleep(2)
                        if ctx.api.status_or_none(uuid) in ("shutdown", "power_off", None, "removed"):
                            break
                    ctx.api.release(uuid)  # raw 容错：已释放/进行中也不抛
                    ctx.reg.remove_instance(uuid)
                    if ctx.reg.get_active() == uuid:
                        ctx.reg.set_active(None)
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
                uuid = ctx.api.create_custom(**opts)
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
        # 高熵后缀：避免不同(中文)实验或同秒重复运行产生相同 run_id 而覆盖/串台
        run_id = _ascii_id(eid) + f"-{int(time.time())}-{uuidlib.uuid4().hex[:6]}"
        wd = cfg.ssh.remote_workdir
        metrics_file = f"{wd}/metrics.json"
        mode, val = e["exec_mode"], e["exec_value"]
        command = _build_command(mode, val)
        try:
            snap = ctx.api.snapshot(uuid)
            # 把实验的 YAML 配置写到实例（命令里可读 config.yaml）
            if e.get("config_yaml"):
                _write_remote_file(ctx, snap, uuid, f"{wd}/config.yaml", e["config_yaml"])
            if command is None:  # script_text
                meta_run = ctx.ssh.run_background(snap, val, run_id, uuid)
            else:
                meta_run = ctx.ssh.run_background_command(snap, command, run_id, uuid)
        except (SSHUnavailable, APIError, KeyError, ValueError) as ex:
            raise HTTPException(502, f"任务启动失败：{ex}")
        cfg_d = {"name": e["name"], "command": command or "(script)", "metrics_file": metrics_file,
                 "metrics_spec": json.loads(e.get("metrics_spec") or "[]"),
                 "config_yaml": e.get("config_yaml", "")}
        ctx.reg.record_run(run_id, uuid, cfg_d, meta_run["log"], meta_run["exit_file"], meta_run["pid"],
                           experiment_id=eid, tag=e.get("tag"))
        return {"run_id": run_id, "pid": meta_run["pid"]}

    @app.get("/api/experiments/{eid}/submit_script")
    def experiment_script(eid: str):
        e = ctx.reg.get_experiment(eid)
        if not e:
            raise HTTPException(404, "experiment not found")
        return PlainTextResponse(_generate_submit_script(e, cfg))

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
        cfg_d = json.loads(r.get("config_json") or "{}")
        log, note = "", ""
        uuid = r["instance_uuid"]
        if r["status"] == "running" and r.get("exit_file"):
            try:
                snap = ctx.api.snapshot(uuid)
                state, code = ctx.ssh.poll(snap, {"pid": r.get("pid"), "exit_file": r["exit_file"]}, uuid)
                if r.get("log_path"):
                    log = ctx.ssh.tail(snap, r["log_path"], lines=lines, instance_uuid=uuid)
                if state == "done":
                    ctx.reg.finish_run(run_id, code if code is not None else -1)
                    _extract_metrics(ctx, snap, uuid, run_id, cfg_d.get("metrics_spec"),
                                     cfg_d.get("metrics_file"), r.get("log_path"))
                    r = ctx.reg.get_run(run_id)
            except SSHUnavailable as e:
                note = f"实例 SSH 不可达（{e.reason}），仅显示已存状态"
            except APIError as e:
                note = f"API 出错：{e}"
        return {"run_id": run_id, "status": r["status"], "exit_code": r["exit_code"], "instance": uuid,
                "experiment_id": r.get("experiment_id"), "tag": r.get("tag"), "config": cfg_d,
                "metrics": ctx.reg.get_metrics(run_id), "series": ctx.reg.get_metric_series(run_id),
                "log": log, "note": note}

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
    """对所有 status==running 且有 exit_file 的 run 做一次轻量 poll；完成则 finish + 抓指标。"""
    for r in ctx.reg.list_runs():
        if r.get("status") != "running" or not r.get("exit_file"):
            continue
        uuid = r["instance_uuid"]
        try:
            if ctx.api.status_or_none(uuid) != "running":
                continue  # 实例已关机/释放，SSH 不可达，跳过（不误判完成）
            snap = ctx.api.snapshot(uuid)
            state, code = ctx.ssh.poll(snap, {"pid": r.get("pid"), "exit_file": r["exit_file"]}, uuid)
            if state == "done":
                ctx.reg.finish_run(r["run_id"], code if code is not None else -1)
                cfg_d = json.loads(r.get("config_json") or "{}")
                _extract_metrics(ctx, snap, uuid, r["run_id"], cfg_d.get("metrics_spec"),
                                 cfg_d.get("metrics_file"), r.get("log_path"))
        except (SSHUnavailable, APIError):
            continue
        except Exception:  # noqa: BLE001 - 对账线程绝不能崩
            continue


# ---------------- 模块级辅助 ----------------
def _safe_balance(ctx):
    try:
        return ctx.api.balance_yuan()
    except APIError:
        return None


def _write_remote_file(ctx, snap, uuid, path, content):
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    ctx.ssh.run(snap, f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(path)}", uuid)


def _extract_metrics(ctx, snap, uuid, run_id, metrics_spec, metrics_file, log_path):
    """完成时抓指标：metrics.json 顶层标量 + series 曲线 + metrics_spec 声明的 (json/正则/auto)。
    本函数自己吞掉所有异常——抓指标失败绝不应让 run_detail/对账线程 500 或崩溃。"""
    try:
        import shlex as _shlex
        jdata = {}
        if metrics_file:
            try:
                out, _e, _c = ctx.ssh.run(snap, f"cat {_shlex.quote(metrics_file)} 2>/dev/null || true", uuid)
                if out.strip():
                    jdata = json.loads(out)
            except (SSHUnavailable, APIError, ValueError):
                jdata = {}
        # 正则抓取用独立的大窗口日志（与 UI 展示的 lines 解耦）
        log_text = ""
        if log_path:
            try:
                log_text = ctx.ssh.tail(snap, log_path, lines=2000, instance_uuid=uuid)
            except (SSHUnavailable, APIError):
                log_text = ""
        scalars = {}
        if isinstance(jdata, dict):
            for k, v in jdata.items():
                if k != "series" and isinstance(v, (int, float, str)):
                    scalars[k] = v
            series = jdata.get("series")
            if isinstance(series, list):
                for pt in series:
                    if isinstance(pt, dict) and isinstance(pt.get("step"), (int, float)):
                        stepped = {k: v for k, v in pt.items() if k != "step" and isinstance(v, (int, float))}
                        if stepped:
                            try:
                                ctx.reg.record_metrics(run_id, stepped, step=pt["step"])
                            except Exception:  # noqa: BLE001
                                pass
        for spec in (metrics_spec or []):
            name = spec.get("name")
            if not name:
                continue
            src, pat = spec.get("source", "auto"), spec.get("pattern")
            if src in ("json", "auto") and isinstance(jdata, dict) and name in jdata:
                scalars[name] = jdata[name]
                continue
            if src in ("regex", "auto") and log_text:
                rx = pat or (re.escape(name) + r"\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)")
                try:
                    ms = re.findall(rx, log_text)
                except re.error:
                    ms = []
                if ms:
                    last = ms[-1]
                    if isinstance(last, tuple):  # 多分组：取最后一个非空组
                        last = next((g for g in reversed(last) if g), "")
                    if last != "":
                        scalars[name] = last
        if scalars:
            ctx.reg.record_metrics(run_id, scalars)
    except Exception:  # noqa: BLE001 - 抓指标永不致命
        pass


def _generate_submit_script(e, cfg):
    pref = json.loads(e.get("instance_pref") or "{}")
    mode, val = e["exec_mode"], e["exec_value"]
    command = _build_command(mode, val) or "bash task.sh"
    name, tag = e["name"], e.get("tag") or ""
    slug = _ascii_id(name)
    wd = cfg.ssh.remote_workdir
    return f'''#!/usr/bin/env python3
"""自动生成 · 实验「{name}」(tag={tag}) 提交脚本。

前置：pip install "autodl-task-submit"，且 .env 里有 AUTODL_TOKEN。
运行：python {slug}_submit.py
脚本会：创建/复用实例 -> 写 config.yaml -> 跑实验 -> 拉回 metrics.json -> 关机（保留以便复用）。
"""
import json
import time
from autodl.config import load_config
from autodl.core import Context

CONFIG_YAML = {json.dumps(e.get("config_yaml") or "", ensure_ascii=False)}
COMMAND = {json.dumps(command, ensure_ascii=False)}
TASK_SCRIPT = {json.dumps(val if mode == "script_text" else "", ensure_ascii=False)}

cfg = load_config()
# —— 实例规格（来自实验定义，可改）——
cfg.gpu_spec_uuid = {json.dumps(pref.get("gpu_spec_uuid") or cfg.gpu_spec_uuid)}
cfg.req_gpu_amount = {_int(pref.get("req_gpu_amount"), 1)}
cfg.expand_disk_gb = {_int(pref.get("expand_disk_gb"), cfg.expand_disk_gb)}
cfg.image_uuid = {json.dumps(pref.get("image_uuid") or cfg.image_uuid)}
if {json.dumps(pref.get("region") or "")}:
    cfg.data_center_list = [{json.dumps(pref.get("region") or "")}]

ctx = Context(cfg)
uuid, snap = ctx.ensure_instance()
print("实例:", uuid)
key = ctx.ssh.ensure_key_access(snap, uuid)

wd = {json.dumps(wd)}
if CONFIG_YAML:
    ctx.ssh.run(snap, "mkdir -p " + wd, uuid)
    import base64
    b64 = base64.b64encode(CONFIG_YAML.encode()).decode()
    ctx.ssh.run(snap, "echo " + b64 + " | base64 -d > " + wd + "/config.yaml", uuid)

run_id = "{slug}-" + str(int(time.time()))
if TASK_SCRIPT:
    out, err, code = ctx.ssh.run_script(snap, TASK_SCRIPT, run_id, uuid)
else:
    out, err, code = ctx.ssh.run(snap, COMMAND, uuid)
print(out)
if err.strip():
    print("[stderr]", err)
print("退出码:", code)

# 拉回 metrics.json（实验把指标写到 {wd}/metrics.json）
m, _e, _c = ctx.ssh.run(snap, "cat " + wd + "/metrics.json 2>/dev/null || true", uuid)
if m.strip():
    print("指标:", m)

ctx.power_off_with_cost(uuid)  # 关机省 GPU 费；下次 ensure_instance 自动开机复用
'''


def serve(host="127.0.0.1", port=8848, cfg=None):
    import uvicorn
    uvicorn.run(create_app(cfg), host=host, port=port)
