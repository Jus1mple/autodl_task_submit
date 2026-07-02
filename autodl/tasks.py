"""任务提交管线 —— CLI / batch 调度器 / Web 后端 / 生成脚本共用的唯一实现。

此前"构造命令 → 执行 → 记台账 → 抓指标 → 收尾"在 cli/scheduler/web/submit.py 里
各写了一遍且互相不一致（如 remote_script 的 ~ 展开只在 web 端修过）。现在收口到这里：

- build_command()：remote_script/remote → 远端命令（正确处理 ~ / $HOME）
- run_foreground()/run_background()：执行 + 记 registry + 抓指标，前后台同一套约定
- refresh_run()：轮询后台 run（探活/tail/完成登记/抓指标），logs 命令、web 详情页、
  对账线程共用
- submit()：一站式编排（余额护栏 → 起/复用实例 → 执行 → 按需收尾），CLI run 命令、
  生成的提交脚本、旧 submit.py 都只是它的薄壳

约定：每次运行前先清掉实例上的旧 metrics.json，避免上一轮的指标被算到本轮头上。
"""
from __future__ import annotations

import json
import posixpath
import re
import time
import uuid as uuidlib
from pathlib import Path

from .errors import InsufficientBalance, SSHUnavailable
from .ssh import _shq

EXEC_MODES = ("remote_script", "remote", "script_text")


# ---------------- 基础件 ----------------
def build_command(mode, value):
    """remote_script/remote → 远端命令字符串；script_text → None（走上传执行）。
    remote_script 用双引号让远端 shell 展开 ~ / $HOME（单引号会让 ~ 失效、cd 失败）。"""
    if mode == "remote_script":
        p = str(value).strip()
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


def ascii_id(s):
    """run_id 会拼进远端文件路径，必须 ASCII 且只含 [A-Za-z0-9_.-]；中文等剥成可读前缀。"""
    out = "".join(ch if ((ch.isascii() and ch.isalnum()) or ch in "_.-") else "_" for ch in str(s))[:50]
    return out.strip("_") or "run"


def make_run_id(base="run"):
    """唯一 run_id：时间戳 + 高熵后缀，避免同秒重复提交互相覆盖。"""
    return f"{ascii_id(base)}-{int(time.time())}-{uuidlib.uuid4().hex[:6]}"


def default_metrics_file(cfg):
    return f"{cfg.ssh.remote_workdir}/metrics.json"


def check_balance(ctx):
    """余额护栏：低于 min_balance_yuan 抛 InsufficientBalance。返回当前余额。"""
    bal = ctx.api.balance_yuan()
    if bal < ctx.cfg.min_balance_yuan:
        raise InsufficientBalance(
            f"余额 ¥{bal:.2f} 低于阈值 ¥{ctx.cfg.min_balance_yuan:.2f}，已中止。")
    return bal


def _run_config(run_id, name, command, metrics_file, metrics_spec, config_yaml, config_extra):
    cfg_d = {"name": name or run_id, "command": command or "(script)",
             "metrics_file": metrics_file, "metrics_spec": metrics_spec or [],
             "config_yaml": config_yaml or ""}
    cfg_d.update(config_extra or {})
    return cfg_d


def _prepare(ctx, snap, uuid, mode, value, config_yaml):
    """公共前置：校验 mode、写 config.yaml、构造命令与 metrics 清理 prelude。"""
    if mode not in EXEC_MODES:
        raise ValueError(f"未知执行方式: {mode}（应为 {'/'.join(EXEC_MODES)}）")
    if config_yaml:
        ctx.ssh.write_file(snap, f"{ctx.cfg.ssh.remote_workdir}/config.yaml", config_yaml, uuid)
    metrics_file = default_metrics_file(ctx.cfg)
    prelude = f"rm -f {_shq(metrics_file)} 2>/dev/null; "
    return build_command(mode, value), metrics_file, prelude


# ---------------- 执行：前台 / 后台 ----------------
def run_foreground(ctx, snap, uuid, *, mode, value, run_id, name=None, retries=0,
                   config_yaml=None, metrics_spec=None, config_extra=None,
                   experiment_id=None, tag=None, stream=None, log=None):
    """同步执行并等结果。全程记入 registry（此前前台运行不进台账），结束后抓指标。
    返回 {run_id, exit_code, stdout, stderr, instance}。
    最后一次尝试若 SSH 不可达则登记失败后抛 SSHUnavailable（调用方可分流退出码）。"""
    command, metrics_file, prelude = _prepare(ctx, snap, uuid, mode, value, config_yaml)
    ctx.reg.record_run(run_id, uuid,
                       _run_config(run_id, name, command, metrics_file, metrics_spec,
                                   config_yaml, config_extra),
                       "(sync)", "", "", experiment_id=experiment_id, tag=tag)
    out = err = ""
    code = None
    attempts = max(1, retries + 1)
    for a in range(attempts):
        try:
            if command is None:
                out, err, code = ctx.ssh.run_script(snap, value, ascii_id(run_id), uuid,
                                                    prelude=prelude, stream=stream)
            else:
                out, err, code = ctx.ssh.run(snap, prelude + command, uuid, stream=stream)
        except SSHUnavailable:
            if a + 1 >= attempts:
                ctx.reg.finish_run(run_id, -1)
                raise
            code = -1
        if code == 0:
            break
        if log and a + 1 < attempts:
            log(f"任务 {run_id} 第{a + 1}次失败(code={code})，重试")
    ctx.reg.finish_run(run_id, code if code is not None else -1)
    extract_metrics(ctx, snap, uuid, run_id, metrics_spec, metrics_file,
                    log_text=f"{out}\n{err}")
    return {"run_id": run_id, "exit_code": code, "stdout": out, "stderr": err, "instance": uuid}


def run_background(ctx, snap, uuid, *, mode, value, run_id, name=None,
                   config_yaml=None, metrics_spec=None, config_extra=None,
                   experiment_id=None, tag=None):
    """后台脱机执行，立即返回 {run_id, pid, log, exit_file, workdir, instance}。
    之后用 refresh_run()（logs 命令 / web 对账线程）判定完成并抓指标。"""
    command, _metrics_file, prelude = _prepare(ctx, snap, uuid, mode, value, config_yaml)
    remote_id = ascii_id(run_id)
    if command is None:
        meta = ctx.ssh.run_background(snap, value, remote_id, uuid, prelude=prelude)
    else:
        meta = ctx.ssh.run_background_command(snap, command, remote_id, uuid, prelude=prelude)
    ctx.reg.record_run(run_id, uuid,
                       _run_config(run_id, name, command, _metrics_file, metrics_spec,
                                   config_yaml, config_extra),
                       meta["log"], meta["exit_file"], meta["pid"],
                       experiment_id=experiment_id, tag=tag)
    return {"run_id": run_id, "instance": uuid, **meta}


# ---------------- 对账：轮询后台 run ----------------
def refresh_run(ctx, run, lines=100):
    """轻量 poll 一个 run：探活 + 可选 tail；发现完成则登记退出码并抓指标。
    返回 {state, exit_code, log, note}；SSH/API 出错只写 note，绝不抛（守护线程可直接用）。"""
    out = {"state": run.get("status"), "exit_code": run.get("exit_code"), "log": "", "note": ""}
    uuid = run.get("instance_uuid")
    if not uuid:
        out["note"] = "run 无实例信息"
        return out
    try:
        st = ctx.api.status_or_none(uuid)
        if st != "running":
            out["note"] = f"实例非 running（{st}），仅显示已存状态"
            return out
        snap = ctx.api.snapshot(uuid)
        if run.get("status") == "running" and run.get("exit_file"):
            state, code = ctx.ssh.poll(snap, {"pid": run.get("pid"), "exit_file": run["exit_file"]}, uuid)
            if state == "done":
                ctx.reg.finish_run(run["run_id"], code if code is not None else -1)
                cfg_d = json.loads(run.get("config_json") or "{}")
                extract_metrics(ctx, snap, uuid, run["run_id"], cfg_d.get("metrics_spec"),
                                cfg_d.get("metrics_file"), log_path=run.get("log_path"))
                out["state"], out["exit_code"] = "done", code
            else:
                out["state"] = "running"
        if lines and run.get("log_path") and run["log_path"] != "(sync)":
            out["log"] = ctx.ssh.tail(snap, run["log_path"], lines=lines, instance_uuid=uuid)
    except SSHUnavailable as e:
        out["note"] = f"实例 SSH 不可达（{e.reason}），仅显示已存状态"
    except Exception as e:  # noqa: BLE001 - API 瞬时错误等：降级为提示
        out["note"] = f"查询出错：{e}"
    return out


# ---------------- 指标抽取 ----------------
def extract_metrics(ctx, snap, uuid, run_id, metrics_spec=None, metrics_file=None,
                    log_path=None, log_text=None):
    """完成时抓指标：metrics.json 顶层标量 + series 曲线 + metrics_spec 声明的 (json/正则/auto)。
    本函数自己吞掉所有异常——抓指标失败绝不应让调用方（CLI/对账线程/接口）失败。"""
    try:
        jdata = {}
        if metrics_file:
            try:
                out, _e, _c = ctx.ssh.run(snap, f"cat {_shq(metrics_file)} 2>/dev/null || true", uuid)
                if out.strip():
                    jdata = json.loads(out)
            except Exception:  # noqa: BLE001 - 读不到/解析失败都当作没有
                jdata = {}
        # 正则抓取用独立的大窗口日志（与 UI 展示的 lines 解耦）；前台运行直接给 log_text
        if log_text is None:
            log_text = ""
            if log_path and log_path != "(sync)":
                try:
                    log_text = ctx.ssh.tail(snap, log_path, lines=2000, instance_uuid=uuid)
                except Exception:  # noqa: BLE001
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
                        stepped = {k: v for k, v in pt.items()
                                   if k != "step" and isinstance(v, (int, float))}
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


# ---------------- 一站式提交 ----------------
def submit(ctx, *, mode, value, instance=None, select_region=False, background=False,
           teardown="keep", run_id=None, name=None, config_yaml=None, metrics_spec=None,
           config_extra=None, experiment_id=None, tag=None, retries=0,
           check_balance_first=True, stream=None, log=print):
    """一站式提交：余额护栏 → 起/复用实例 → 执行 → （前台）按需收尾。

    mode: remote_script（实例上已有脚本）| remote（任意命令）| script（本地脚本路径，
          先读入再上传）| script_text（脚本内容字符串）
    teardown: keep | power_off | release —— 仅前台生效；无论任务成败都会执行（止损优先）。
    后台任务结束时机未知，禁止与 teardown 组合（请完成后自行 `autodl down`）。
    """
    if mode == "script":
        p = Path(value).expanduser()
        config_extra = {"script": str(p), **(config_extra or {})}
        value, mode = p.read_text(encoding="utf-8"), "script_text"  # 尽早读，失败不开机
    if mode not in EXEC_MODES:
        raise ValueError(f"未知执行方式: {mode}")
    if background and teardown != "keep":
        raise ValueError("background 与自动收尾(power_off/release)不能同时用：后台任务结束时机未知")
    if check_balance_first:
        check_balance(ctx)

    if instance:
        uuid = instance
        snap = ctx.ensure_specific(uuid, log=log)
    else:
        uuid, snap = ctx.ensure_instance(select_region=select_region, log=log)
    rid = ascii_id(run_id) if run_id else make_run_id(name or mode)

    if background:
        try:
            return run_background(ctx, snap, uuid, mode=mode, value=value, run_id=rid,
                                  name=name, config_yaml=config_yaml, metrics_spec=metrics_spec,
                                  config_extra=config_extra, experiment_id=experiment_id, tag=tag)
        except (SSHUnavailable, OSError) as e:
            # 实例已就绪（可能是本次刚创建的），但任务没起来——绝不能静默放任计费
            if log:
                log(f"⚠️ 后台任务启动失败: {e}")
                log(f"⚠️ 实例 {uuid} 已就绪但任务未启动、仍在计费！"
                    f"请尽快 `autodl down`（或 `autodl down --release`）。")
            raise

    cmd_preview = build_command(mode, value)
    if log and cmd_preview:
        log(f"执行: {cmd_preview}")
    try:
        return run_foreground(ctx, snap, uuid, mode=mode, value=value, run_id=rid, name=name,
                              retries=retries, config_yaml=config_yaml, metrics_spec=metrics_spec,
                              config_extra=config_extra, experiment_id=experiment_id, tag=tag,
                              stream=stream, log=log)
    finally:
        if teardown != "keep":
            ctx.finish_instance(uuid, teardown, log=log)
