"""桩测试：scheduler.run_batch 与 cli.cmd_run 的接线。"""
import sys
import types

from autodl import cli, scheduler, tasks
from autodl.config import Config

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---------------- scheduler ----------------
class Reg:
    def __init__(self):
        self.runs, self.instances, self.metrics = {}, {}, {}

    def get_run(self, rid):
        return self.runs.get(rid)

    def record_run(self, rid, uuid, config, log_path, exit_file, pid, experiment_id=None, tag=None):
        self.runs[rid] = {"run_id": rid, "status": "running", "instance_uuid": uuid, "tag": tag}

    def finish_run(self, rid, code):
        self.runs[rid].update(status="succeeded" if code == 0 else "failed", exit_code=code)

    def record_metrics(self, rid, m, step=None):
        self.metrics.setdefault(rid, {}).update(m)

    def upsert_instance(self, uuid, **kw):
        self.instances.setdefault(uuid, {}).update(kw)

    def remove_instance(self, uuid):
        self.instances.pop(uuid, None)

    def get_active(self):
        return None

    def set_active(self, uuid):
        pass


class SSH:
    def __init__(self):
        self.pulls = []

    def run(self, snap, command, uuid=None, stream=None, max_capture=None):
        if command.startswith("cat "):
            return "", "", 0
        # job2 故意失败
        return ("", "", 1) if "fail_me" in command else ("out", "", 0)

    def run_script(self, snap, text, rid, uuid=None, prelude="", stream=None, max_capture=None, runner="bash"):
        return "out", "", 0

    def write_file(self, *a, **kw):
        pass

    def ensure_key_access(self, snap, uuid=None):
        return "/fake/key"

    def pull_artifacts(self, snap, patterns, local_dir, key, base=None, instance_uuid=None):
        self.pulls.append({"patterns": patterns, "local_dir": local_dir})
        return {"files": ["r.json"], "local_dir": local_dir, "skipped_patterns": []}


class API:
    def __init__(self):
        self.n = 0

    def create(self, **kw):
        self.n += 1
        return f"pro-{self.n}"

    def wait_running(self, uuid, log=None):
        return {"proxy_host": "h", "ssh_port": 22, "root_password": "p", "region_sign": "westDC2"}

    def status_or_none(self, uuid):
        return "running"


class Ctx:
    def __init__(self):
        self.cfg = Config()
        self.reg, self.ssh, self.api = Reg(), SSH(), API()
        self.finishes = []

    def select_region(self, log=None):
        return "westDC2"

    def finish_instance(self, uuid, mode="power_off", log=print, release_retries=3):
        self.finishes.append((uuid, mode))
        return True


ctx = Ctx()
jobs = [{"id": "a", "mode": "remote", "value": "echo a"},
        {"id": "b", "mode": "remote", "value": "fail_me"},
        {"id": "c", "mode": "script_text", "value": "#!/bin/bash\necho c", "display": "c.sh"}]
res = scheduler.run_batch(ctx, jobs, max_parallel=2, on_finish="release", batch_id="bt", log=None)
by = {r["id"]: r for r in res}
check("batch a 成功", by["a"]["status"] == "succeeded", str(by))
check("batch b 失败", by["b"]["status"] == "failed", str(by))
check("batch c 成功", by["c"]["status"] == "succeeded", str(by))
check("batch 记 run 台账", ctx.reg.runs.get("bt:a", {}).get("status") == "succeeded", str(ctx.reg.runs))
check("batch run 带 tag", ctx.reg.runs.get("bt:a", {}).get("tag") == "bt")
check("batch 收尾 release", all(m == "release" for _, m in ctx.finishes) and len(ctx.finishes) == 2, str(ctx.finishes))

# resume：a/c 已成功，只重跑 b
ctx2 = Ctx()
ctx2.reg.runs = {"bt:a": {"status": "succeeded"}, "bt:c": {"status": "succeeded"}}
res2 = scheduler.run_batch(ctx2, jobs, max_parallel=2, on_finish="keep", batch_id="bt", log=None)
by2 = {r["id"]: r for r in res2}
check("batch resume 跳过 a/c", by2["a"]["status"] == "skipped" and by2["c"]["status"] == "skipped", str(by2))
check("batch resume 重跑 b", by2["b"]["status"] == "failed", str(by2))
# finish_instance("keep") 在真实实现里是空操作；这里只断言没有关机/释放动作
check("batch keep 不关机/释放", all(m == "keep" for _, m in ctx2.finishes), str(ctx2.finishes))

# batch 每个 job 拉回各自产物到 <pull_to>/<job_id>/
ctxp = Ctx()
jobs_p = [{"id": "j1", "mode": "remote", "value": "echo j1", "pull": "out/*.json", "pull_to": "./res"}]
scheduler.run_batch(ctxp, jobs_p, max_parallel=1, on_finish="keep", batch_id="bp", log=None)
check("batch job 拉产物到 job 子目录", any(p["local_dir"] == "./res/j1" for p in ctxp.ssh.pulls), str(ctxp.ssh.pulls))
check("batch job 拉对 patterns", any(p["patterns"] == "out/*.json" for p in ctxp.ssh.pulls), str(ctxp.ssh.pulls))

# ---------------- cli.cmd_run 接线 ----------------
calls = {}
real_submit = tasks.submit


def fake_submit(ctx, **kw):
    calls.update(kw)
    if kw.get("background"):
        return {"run_id": "r-bg", "pid": "9", "log": "/l/r.log", "exit_file": "/l/r.exit",
                "workdir": "/w", "instance": "pro-1"}
    return {"run_id": "r-fg", "exit_code": 0, "stdout": "ok", "stderr": "", "instance": "pro-1"}


tasks.submit = fake_submit
cli.tasks.submit = fake_submit


class CliCtx:
    def use_instance(self, uuid, log=None):
        return "running"


def mkargs(**kw):
    base = dict(script=None, remote_script=None, remote=None, instance=None, background=False,
                name=None, select_region=False, down=False, release=False, json=False,
                sync=False, sync_mode="ff", setup=False, pull=None, pull_to="./results", dry_run=False, max_hours=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


rc = cli.cmd_run(CliCtx(), mkargs(remote_script="~/p/run.sh", down=True))
check("cli fg rc=0", rc == 0)
check("cli fg teardown=power_off", calls.get("teardown") == "power_off", str(calls))
check("cli fg mode 正确", calls.get("mode") == "remote_script" and calls.get("value") == "~/p/run.sh")
check("cli fg 有实时流", calls.get("stream") is not None)

rc = cli.cmd_run(CliCtx(), mkargs(remote="echo hi", background=True))
check("cli bg rc=0", rc == 0)
check("cli bg 无 teardown", calls.get("teardown") == "keep", str(calls.get("teardown")))
check("cli bg 不流式", calls.get("stream") is None)

rc = cli.cmd_run(CliCtx(), mkargs(remote="x", background=True, down=True))
check("cli bg+down 拒绝", rc == cli.EXIT_USAGE, str(rc))

rc = cli.cmd_run(CliCtx(), mkargs())
check("cli 无模式拒绝", rc == cli.EXIT_USAGE, str(rc))

rc = cli.cmd_run(CliCtx(), mkargs(remote="x", script="y.sh"))
check("cli 双模式拒绝", rc == cli.EXIT_USAGE, str(rc))

# cli --pull 组装 artifacts spec 传给 submit
cli.cmd_run(CliCtx(), mkargs(remote="x", pull="metrics.json,out/*", pull_to="./r"))
check("cli --pull 组装 artifacts", calls.get("artifacts") == {"patterns": "metrics.json,out/*", "local_dir": "./r"}, str(calls.get("artifacts")))
cli.cmd_run(CliCtx(), mkargs(remote="x"))
check("cli 无 --pull 时 artifacts=None", calls.get("artifacts") is None, str(calls.get("artifacts")))

tasks.submit = real_submit
print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
