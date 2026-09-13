"""envsetup 桩测试：LocalSSH 用本地 bash 真执行生成的脚本（HOME 重定向防污染）。"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from autodl import envsetup
from autodl.config import Config
from autodl.errors import AutoDLError

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


tmp = Path(tempfile.mkdtemp(prefix="envsetup-test-"))
home = tmp / "home"; home.mkdir()
os.environ["HOME"] = str(home)  # hash 文件写到假 HOME
repo = tmp / "repo"; repo.mkdir()


class LocalSSH:
    def run(self, snap, command, uuid=None, stream=None, max_capture=None):
        r = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(home)})
        if stream and r.stdout:
            stream(r.stdout)
        return r.stdout, r.stderr, r.returncode

    def run_script(self, snap, text, rid, uuid=None, prelude="", stream=None, max_capture=None, runner="bash"):
        return self.run(snap, prelude + text, uuid, stream)

    def write_file(self, snap, path, content, uuid=None):
        pass


class Reg:
    def __init__(self):
        self.runs, self.metrics = {}, {}

    def record_run(self, rid, uuid, config, log_path, exit_file, pid, experiment_id=None, tag=None):
        self.runs[rid] = {"run_id": rid, "status": "running", "tag": tag, "config": config}

    def finish_run(self, rid, code):
        self.runs[rid].update(status="succeeded" if code == 0 else "failed", exit_code=code)

    def record_metrics(self, rid, m, step=None):
        self.metrics.setdefault(rid, {}).update(m)

    def get_metrics(self, rid):
        return self.metrics.get(rid, {})


class Ctx:
    def __init__(self):
        self.cfg = Config()
        self.cfg.git.dir = str(repo)
        self.cfg.ssh.remote_workdir = str(tmp / "workdir")  # metrics prelude 的 rm 落在这里
        self.ssh, self.reg = LocalSSH(), Reg()


ctx = Ctx()
snap = {}

# ---------- plan：自动探测 ----------
try:
    envsetup.plan(ctx, snap, "pro-1")
    check("plan 无声明应报错", False)
except AutoDLError as e:
    check("plan 无声明报错清晰", "env.setup" in str(e), str(e))

(repo / "requirements.txt").write_text("numpy\n")
cmd, src = envsetup.plan(ctx, snap, "pro-1")
check("plan 探测 requirements", src == "requirements.txt", src)
check("plan pip 带镜像源+no-cache", "-i https://pypi.tuna" in cmd and "--no-cache-dir" in cmd, cmd)

(repo / "setup.sh").write_text("echo custom\n")
cmd, src = envsetup.plan(ctx, snap, "pro-1")
check("plan setup.sh 优先", src == "setup.sh" and cmd == "bash setup.sh", f"{src} {cmd}")

ctx.cfg.env.setup = "pip install -e . && pip install extra"
cmd, src = envsetup.plan(ctx, snap, "pro-1")
check("plan env.setup 最高优先", src == "env.setup" and cmd.startswith("pip install -e ."), src)

# ---------- compose：turbo 开关 ----------
ctx.cfg.env.academic_turbo = True
s = envsetup._compose(ctx.cfg, str(repo), "echo x")
check("compose 含学术加速", "network_turbo" in s, s)
ctx.cfg.env.academic_turbo = False
s = envsetup._compose(ctx.cfg, str(repo), "echo x")
check("compose 默认无 turbo", "network_turbo" not in s)
check("compose 先 source conda", "conda.sh" in s and s.index("conda.sh") < s.index("echo x"))

# ---------- run_setup 全流程（真跑 bash） ----------
ctx.cfg.env.setup = "echo installing > installed.txt"
res = envsetup.run_setup(ctx, snap, "pro-1")
check("setup 首次执行", res["skipped"] is False and res["exit_code"] == 0, str(res))
check("setup 安装命令生效", (repo / "installed.txt").read_text().strip() == "installing")
check("setup 写了 hash", (home / ".autodl_env_hash").exists())
check("setup 进台账(tag=setup)", any(r["tag"] == "setup" for r in ctx.reg.runs.values()))

res2 = envsetup.run_setup(ctx, snap, "pro-1")
check("setup 第二次秒跳", res2["skipped"] is True and res2["source"] == "env.setup", str(res2))

res3 = envsetup.run_setup(ctx, snap, "pro-1", force=True)
check("setup --force 强制重装", res3["skipped"] is False and res3["exit_code"] == 0, str(res3))

# 依赖声明变化 → hash 变 → 重装
(repo / "requirements.txt").write_text("numpy\npandas\n")
res4 = envsetup.run_setup(ctx, snap, "pro-1")
check("依赖文件变了触发重装", res4["skipped"] is False, str(res4))

# 换配置命令 → hash 变 → 重装
ctx.cfg.env.setup = "echo v2 > installed.txt"
res5 = envsetup.run_setup(ctx, snap, "pro-1")
check("安装命令变了触发重装", res5["skipped"] is False and (repo / "installed.txt").read_text().strip() == "v2")

# 安装失败：非零退出 + 不写 hash（下次仍会重试）
old_hash = (home / ".autodl_env_hash").read_text()
ctx.cfg.env.setup = "false"
res6 = envsetup.run_setup(ctx, snap, "pro-1")
check("setup 失败非零退出", res6["exit_code"] != 0, str(res6))
check("失败不更新 hash", (home / ".autodl_env_hash").read_text() == old_hash)
res7 = envsetup.run_setup(ctx, snap, "pro-1")
check("失败后下次不秒跳", res7["skipped"] is False, str(res7))

# ---------- CLI 接线 ----------
from autodl import cli
import io, json, contextlib


class CliCtx(Ctx):
    def __init__(self):
        super().__init__()
        class R2(Reg):
            def get_active(self):
                return "pro-1"
        self.reg = R2()

    def ensure_specific(self, uuid, log=print):
        return {}


cctx = CliCtx()
cctx.cfg.env.setup = "echo ok"
args = types.SimpleNamespace(instance=None, dir=None, force=True, background=False, json=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_setup(cctx, args)
d = json.loads(buf.getvalue())
check("cli setup --json", rc == 0 and d["exit_code"] == 0 and d["source"] == "env.setup", buf.getvalue())

cctx.cfg.env.setup = "false"
args.force = True
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_setup(cctx, args)
check("cli setup 失败退出码 6", rc == cli.EXIT_TASK, str(rc))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
