"""桩测试：不触网验证 tasks 管线 + core.finish_instance 的关键行为。"""
import sys

from autodl import tasks
from autodl.config import Config
from autodl.errors import SSHUnavailable

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---------- build_command ----------
c = tasks.build_command("remote_script", "~/proj/submit_exec.sh")
check("build_command ~ 展开", c == 'cd "$HOME/proj" && bash "submit_exec.sh"', c)
c = tasks.build_command("remote_script", "/root/a b/run.sh")
check("build_command 空格路径", c == 'cd "/root/a b" && bash "run.sh"', c)
check("build_command remote 原样", tasks.build_command("remote", "echo hi") == "echo hi")
check("build_command script_text→None", tasks.build_command("script_text", "#!/bin/bash") is None)

# ---------- run_id ----------
a, b = tasks.make_run_id("实验A"), tasks.make_run_id("实验A")
check("make_run_id 唯一", a != b, f"{a} vs {b}")
check("make_run_id ASCII 安全", all(ch.isascii() for ch in a), a)
check("ascii_id 中文剥离", tasks.ascii_id("基线:实验 1") == "1" or tasks.ascii_id("基线:实验 1"), tasks.ascii_id("基线:实验 1"))


# ---------- 桩 ctx ----------
class FakeReg:
    def __init__(self):
        self.runs, self.metrics, self.calls = {}, {}, []

    def record_run(self, run_id, uuid, config, log_path, exit_file, pid, experiment_id=None, tag=None):
        self.calls.append(("record", run_id))
        self.runs[run_id] = {"run_id": run_id, "instance_uuid": uuid, "config_json": None,
                             "status": "running", "log_path": log_path, "exit_file": exit_file,
                             "pid": pid, "exit_code": None, "experiment_id": experiment_id, "tag": tag,
                             "config": config}

    def finish_run(self, run_id, code):
        self.calls.append(("finish", run_id, code))
        self.runs[run_id]["status"] = "succeeded" if code == 0 else "failed"
        self.runs[run_id]["exit_code"] = code

    def record_metrics(self, run_id, mapping, step=None):
        self.metrics.setdefault(run_id, {}).update(mapping)

    def get_metrics(self, run_id):
        return self.metrics.get(run_id, {})


class FakeSSH:
    def __init__(self, out="ok\nASR: 0.75\n", code=0, fail_times=0):
        self.out, self.code, self.fail_times = out, code, fail_times
        self.commands, self.files = [], {}

    def write_file(self, snap, path, content, uuid=None):
        self.files[path] = content

    def run(self, snap, command, uuid=None, stream=None, max_capture=None):
        self.commands.append(command)
        if command.startswith("cat "):
            return '{"Accuracy": 0.9, "series": [{"step": 1, "loss": 0.5}]}', "", 0
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SSHUnavailable(SSHUnavailable.TIMEOUT, "boom")
        if stream:
            stream(self.out)
        return self.out, "", self.code

    def run_script(self, snap, text, run_id, uuid=None, prelude="", stream=None, max_capture=None):
        self.commands.append(f"{prelude}bash task_{run_id}.sh")
        return self.out, "", self.code

    def run_background_command(self, snap, command, run_id, uuid=None, prelude=""):
        self.commands.append(prelude + command)
        return {"pid": "123", "log": f"/l/{run_id}.log", "exit_file": f"/l/{run_id}.exit", "workdir": "/w"}

    def run_background(self, snap, text, run_id, uuid=None, prelude=""):
        return self.run_background_command(snap, f"bash task_{run_id}.sh", run_id, uuid, prelude)

    def poll(self, snap, meta, uuid=None):
        return "done", 0

    def tail(self, snap, log_file, lines=50, instance_uuid=None):
        return "line1\nASR: 0.88\n"

    def ensure_key_access(self, snap, uuid=None):
        return "/fake/key"

    def pull_artifacts(self, snap, patterns, local_dir, key, base=None, instance_uuid=None):
        self.pulled = {"patterns": patterns, "local_dir": local_dir, "key": key}
        return {"files": ["output/a.bin", "metrics.json"], "local_dir": local_dir, "skipped_patterns": []}


class FakeAPI:
    def __init__(self):
        self.balance = 100.0

    def balance_yuan(self):
        return self.balance

    def status_or_none(self, uuid):
        return "running"

    def snapshot(self, uuid):
        return {"proxy_host": "h", "ssh_port": 22, "root_password": "p"}


class FakeCtx:
    def __init__(self):
        self.cfg = Config()
        self.reg, self.ssh, self.api = FakeReg(), FakeSSH(), FakeAPI()
        self.finishes = []

    def ensure_instance(self, select_region=False, log=print):
        return "pro-1", self.api.snapshot("pro-1")

    def ensure_specific(self, uuid, log=print):
        return self.api.snapshot(uuid)

    def finish_instance(self, uuid, mode="power_off", log=print, release_retries=3):
        self.finishes.append((uuid, mode))
        return True


# ---------- run_foreground ----------
ctx = FakeCtx()
snap = ctx.api.snapshot("pro-1")
res = tasks.run_foreground(ctx, snap, "pro-1", mode="remote", value="echo hi", run_id="r1",
                           metrics_spec=[{"name": "ASR", "source": "regex"}])
check("fg 退出码", res["exit_code"] == 0)
check("fg 记台账", ("record", "r1") in ctx.reg.calls and ("finish", "r1", 0) in ctx.reg.calls)
check("fg 清理旧 metrics.json", any(c.startswith("rm -f ") for c in ctx.reg.runs and ctx.ssh.commands), str(ctx.ssh.commands))
m = ctx.reg.get_metrics("r1")
check("fg 抓 json 指标", m.get("Accuracy") == 0.9, str(m))
check("fg 正则抓 stdout 指标", m.get("ASR") == "0.75", str(m))

# fg 重试：第一次 SSH 挂、第二次成功
ctx2 = FakeCtx()
ctx2.ssh = FakeSSH(fail_times=1)
res2 = tasks.run_foreground(ctx2, snap, "pro-1", mode="remote", value="x", run_id="r2", retries=1)
check("fg 重试后成功", res2["exit_code"] == 0)

# fg 全部失败 → 登记 -1 并抛
ctx3 = FakeCtx()
ctx3.ssh = FakeSSH(fail_times=9)
try:
    tasks.run_foreground(ctx3, snap, "pro-1", mode="remote", value="x", run_id="r3")
    check("fg 全失败应抛 SSHUnavailable", False)
except SSHUnavailable:
    check("fg 全失败抛 SSHUnavailable", True)
check("fg 全失败登记 -1", ("finish", "r3", -1) in ctx3.reg.calls, str(ctx3.reg.calls))

# ---------- run_background ----------
ctx4 = FakeCtx()
meta = tasks.run_background(ctx4, snap, "pro-1", mode="remote_script", value="~/p/run.sh",
                            run_id="bg1", config_yaml="lr: 0.1")
check("bg 返回 pid/log", meta["pid"] == "123" and meta["log"].endswith("bg1.log"))
check("bg 写 config.yaml", ctx4.ssh.files.get("/root/autodl-tmp/config.yaml") == "lr: 0.1")
check("bg 命令含 $HOME", any('cd "$HOME/p"' in c for c in ctx4.ssh.commands), str(ctx4.ssh.commands))
check("bg 命令含 rm metrics", any(c.startswith("rm -f ") for c in ctx4.ssh.commands), str(ctx4.ssh.commands))

# ---------- refresh_run ----------
run = dict(ctx4.reg.runs["bg1"])
run["config_json"] = '{"metrics_spec": [{"name": "ASR", "source": "regex"}], "metrics_file": "/w/metrics.json"}'
info = tasks.refresh_run(ctx4, run, lines=10)
check("refresh 判定完成", info["state"] == "done" and info["exit_code"] == 0, str(info))
check("refresh 登记完成", ctx4.reg.runs["bg1"]["status"] == "succeeded")
check("refresh tail 日志", "line1" in info["log"], info["log"])
check("refresh 正则抓日志指标", ctx4.reg.get_metrics("bg1").get("ASR") == "0.88", str(ctx4.reg.get_metrics("bg1")))

# refresh 对已完成 run 不再 poll
run2 = dict(ctx4.reg.runs["bg1"])
info2 = tasks.refresh_run(ctx4, run2, lines=5)
check("refresh 已完成只 tail", info2["state"] == "succeeded" and "line1" in info2["log"], str(info2))

# ---------- submit ----------
ctx5 = FakeCtx()
res5 = tasks.submit(ctx5, mode="remote", value="echo hi", teardown="power_off", log=None)
check("submit fg 成功", res5["exit_code"] == 0)
check("submit fg 收尾 power_off", ctx5.finishes == [("pro-1", "power_off")], str(ctx5.finishes))

ctx6 = FakeCtx()
ctx6.ssh = FakeSSH(code=7)
res6 = tasks.submit(ctx6, mode="remote", value="false", teardown="release", log=None)
check("submit 失败也收尾(止损)", ctx6.finishes == [("pro-1", "release")], str(ctx6.finishes))
check("submit 返回失败码", res6["exit_code"] == 7)

ctx7 = FakeCtx()
try:
    tasks.submit(ctx7, mode="remote", value="x", background=True, teardown="power_off", log=None)
    check("submit bg+teardown 应拒绝", False)
except ValueError:
    check("submit bg+teardown 拒绝", True)

ctx8 = FakeCtx()
ctx8.api.balance = 1.0
try:
    tasks.submit(ctx8, mode="remote", value="x", log=None)
    check("submit 余额护栏应拦截", False)
except Exception as e:
    check("submit 余额护栏拦截", type(e).__name__ == "InsufficientBalance", repr(e))

ctx9 = FakeCtx()
res9 = tasks.submit(ctx9, mode="remote", value="x", background=True, log=None)
check("submit bg 返回 meta", res9["pid"] == "123" and ctx9.finishes == [], str(res9))

# script 模式：本地文件不存在 → 开机前失败
ctx10 = FakeCtx()
booted = []
ctx10.ensure_instance = lambda **kw: booted.append(1) or ("pro-1", snap)
try:
    tasks.submit(ctx10, mode="script", value="/nonexistent/x.sh", log=None)
    check("submit script 缺文件应抛", False)
except OSError:
    check("submit script 缺文件先于开机失败", booted == [], str(booted))

# ---------- ssh._bg_inner：命令组重定向（真 bash 验证） ----------
import subprocess
import tempfile
from pathlib import Path as _P

from autodl.ssh import _bg_inner

td = _P(tempfile.mkdtemp(prefix="bginner-"))
log, ex = td / "run.log", td / "run.exit"
inner = _bg_inner("", f'cd {td} && echo first; echo second; echo data > sub.txt', str(log), str(ex))
subprocess.run(["bash", "-c", inner], capture_output=True)
check("bg 组重定向：链上所有输出进日志", log.read_text() == "first\nsecond\n", repr(log.read_text()))
check("bg 任务自己的重定向不被覆盖", (td / "sub.txt").read_text() == "data\n")
check("bg 退出码 0", ex.read_text().strip() == "0")

inner2 = _bg_inner("", "echo before-fail; exit 7", str(log), str(ex))
subprocess.run(["bash", "-c", inner2], capture_output=True)
check("bg 失败退出码透传", ex.read_text().strip() == "7", ex.read_text())
check("bg 失败前输出也在日志", log.read_text() == "before-fail\n", repr(log.read_text()))

inner3 = _bg_inner("rm -f x 2>/dev/null; ", "echo ok", str(log), str(ex))
subprocess.run(["bash", "-c", inner3], capture_output=True)
check("bg prelude 不进日志", log.read_text() == "ok\n" and ex.read_text().strip() == "0")

# ---------- _TailBuf：大输出尾部滚动 ----------
from autodl.ssh import _TailBuf

b = _TailBuf(None)
for _ in range(100):
    b.add("x" * 1000)
check("TailBuf 不限时全留", len(b.text()) == 100000 and not b.truncated)

b = _TailBuf(1000)
for i in range(100):
    b.add(f"line{i:04d}-" + "y" * 90 + "\n")  # 每块 100 字符，共 10000
t = b.text()
check("TailBuf 截断标记", "输出过大已截断" in t)
# 稳态长度在 limit~2*limit 之间波动（超 2*limit 才裁到 limit）；关键是远小于全量 10000
check("TailBuf 只留末尾(远小于全量)", 1000 <= len(t) <= 2500, len(t))
check("TailBuf 保留的是末尾内容", "line0099" in t and "line0000" not in t)

# 指标在末尾能被保留（前台 metrics 从 stdout 抓）
b = _TailBuf(1000)
b.add("early junk " * 500)     # 5000 字符前置垃圾
b.add("ASR: 0.91\n")           # 末尾指标
check("TailBuf 末尾指标保留", "ASR: 0.91" in b.text())

# ---------- refresh_run：指标幂等补抽 ----------
class RRReg(FakeReg):
    def __init__(self, has_metrics=False):
        super().__init__()
        self._m = {"ASR": "0.9"} if has_metrics else {}
        self.extract_calls = 0

    def get_metrics(self, run_id):
        return dict(self._m)


class RRApi:
    def __init__(self, running=True):
        self.running = running
    def status_or_none(self, uuid):
        return "running" if self.running else "shutdown"
    def snapshot(self, uuid):
        return {"proxy_host": "h", "ssh_port": 22, "root_password": "p"}


class RRSSH:
    def __init__(self):
        self.extract_reads = 0
    def run(self, snap, command, uuid=None, stream=None, max_capture=None):
        if command.startswith("cat "):
            self.extract_reads += 1
            return '{"ASR": 0.9}', "", 0
        return "", "", 0
    def tail(self, snap, log_file, lines=50, instance_uuid=None):
        return "log tail\n"


class RRCtx:
    def __init__(self, has_metrics=False, running=True):
        self.cfg = Config()
        self.reg = RRReg(has_metrics)
        self.api = RRApi(running)
        self.ssh = RRSSH()


# 场景 A：已完成、无指标、实例 running → 补抽（extract 会 cat metrics.json）
rc = RRCtx(has_metrics=False, running=True)
recorded = {}
rc.reg.record_metrics = lambda rid, m, step=None: recorded.update(m)
run = {"run_id": "r1", "instance_uuid": "pro-1", "status": "succeeded", "exit_code": 0,
       "exit_file": "/l/r1.exit", "pid": "1", "log_path": "/l/r1.log",
       "config_json": '{"metrics_file": "/w/metrics.json"}'}
info = tasks.refresh_run(rc, run)
check("补抽：读了 metrics.json", rc.ssh.extract_reads >= 1, rc.ssh.extract_reads)
check("补抽：抓到指标入库", recorded.get("ASR") == 0.9, str(recorded))

# 场景 B：已完成、已有指标、实例 running → 不重复读文件
rc2 = RRCtx(has_metrics=True, running=True)
run2 = dict(run, run_id="r2")
tasks.refresh_run(rc2, run2)
check("已有指标不再补抽", rc2.ssh.extract_reads == 0, rc2.ssh.extract_reads)

# 场景 C：已完成、无指标、实例已关机 → 不抽，但给补抓指引
rc3 = RRCtx(has_metrics=False, running=False)
info3 = tasks.refresh_run(rc3, dict(run, run_id="r3"))
check("关机不补抽(SSH不通)", rc3.ssh.extract_reads == 0)
check("关机给补抓指引", "可补抓" in info3["note"], info3["note"])

# 场景 D：仍在 running 的任务，poll 到 done → 正常首抽（不受补抽逻辑影响）
rc4 = RRCtx(has_metrics=False, running=True)
recorded4 = {}
rc4.reg.record_metrics = lambda rid, m, step=None: recorded4.update(m)
rc4.ssh.poll = lambda snap, meta, uuid=None: ("done", 0)
rc4.reg.finish_run = lambda rid, code: None
run4 = dict(run, run_id="r4", status="running")
info4 = tasks.refresh_run(rc4, run4)
check("running->done 首抽", info4["state"] == "done" and recorded4.get("ASR") == 0.9, str(info4))

# ---------- artifacts 拉回 ----------
from autodl.ssh import _ARTIFACT_PAT_RE

check("glob 校验 允许", bool(_ARTIFACT_PAT_RE.match("output/checkpoint-*/adapter*.safetensors")))
check("glob 校验 允许逗号项(单项)", bool(_ARTIFACT_PAT_RE.match("metrics.json")))
check("glob 校验 拒绝分号", not _ARTIFACT_PAT_RE.match("x; rm -rf /"))
check("glob 校验 拒绝命令替换", not _ARTIFACT_PAT_RE.match("$(evil)"))
check("glob 校验 拒绝空格", not _ARTIFACT_PAT_RE.match("a b"))

# 前台：配了 artifacts → 完成后拉回
ctxA = FakeCtx()
resA = tasks.run_foreground(ctxA, snap, "pro-1", mode="remote", value="x", run_id="ra",
                            artifacts={"patterns": "output/*,metrics.json", "local_dir": "./out"})
check("fg 拉产物", resA["artifacts"] and len(resA["artifacts"]["files"]) == 2, str(resA.get("artifacts")))
check("fg 拉到指定目录", ctxA.ssh.pulled["local_dir"] == "./out")
check("fg config 存 artifacts", ctxA.reg.runs["ra"]["config"].get("artifacts", {}).get("local_dir") == "./out")

# 前台：没配 artifacts → 不拉
ctxB = FakeCtx()
resB = tasks.run_foreground(ctxB, snap, "pro-1", mode="remote", value="x", run_id="rb")
check("fg 无 artifacts 不拉", resB["artifacts"] is None and not hasattr(ctxB.ssh, "pulled"))

# collect_artifacts：无 spec → None；有 spec → 拉
ctxC = FakeCtx()
check("collect 无 spec 返回 None", tasks.collect_artifacts(ctxC, snap, "pro-1", None) is None)
r = tasks.collect_artifacts(ctxC, snap, "pro-1", {"patterns": "m.json", "local_dir": "./z"})
check("collect 有 spec 拉回", r and r["local_dir"] == "./z")

# 后台：artifacts 存进 config，refresh done 时拉
ctxD = FakeCtx()
tasks.run_background(ctxD, snap, "pro-1", mode="remote", value="x", run_id="rd",
                     artifacts={"patterns": "ckpt/*", "local_dir": "./bg_out"})
import json as _json
run = dict(ctxD.reg.runs["rd"])
run["config_json"] = _json.dumps(ctxD.reg.runs["rd"]["config"])
info = tasks.refresh_run(ctxD, run, lines=0)
check("bg config 存 artifacts", "artifacts" in ctxD.reg.runs["rd"]["config"])
check("refresh done 拉产物", info.get("artifacts") and info["artifacts"]["local_dir"] == "./bg_out", str(info.get("artifacts")))
check("refresh 拉到正确 patterns", ctxD.ssh.pulled["patterns"] == "ckpt/*")

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
