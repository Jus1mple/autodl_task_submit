"""桩测试：gitsync 脚本生成/解析 + .env 加载优先级 + CLI 接线。
FakeSSH 用本地 bash 真执行 gitsync 生成的脚本（针对本地临时 git 仓库），
这样脚本语法和 git 行为都是真实验证，只有 SSH 传输是假的。"""
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from autodl import cli, gitsync
from autodl.config import Config, load_config
from autodl.errors import AutoDLError

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd}\n{r.stdout}{r.stderr}"
    return r.stdout.strip()


# ---------- 本地造一对 git 仓库：origin(bare) + 实例工作区 ----------
tmp = Path(tempfile.mkdtemp(prefix="gitsync-test-"))
origin = tmp / "origin.git"
seed = tmp / "seed"
sh(f"git init -q --bare {origin}")
seed.mkdir()
sh("git init -q -b main .", cwd=seed)
sh("git config user.email t@t && git config user.name t", cwd=seed)
(seed / "train.sh").write_text("echo v1\n")
sh("git add -A && git commit -qm v1 && git remote add origin " + str(origin) + " && git push -q origin main", cwd=seed)


class LocalSSH:
    """把"远端执行"落到本地 bash，真实验证 gitsync 生成的脚本。"""
    def __init__(self):
        self.files = {}

    def run(self, snap, command, uuid=None, stream=None):
        r = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        return r.stdout, r.stderr, r.returncode

    def write_file(self, snap, path, content, uuid=None):
        self.files[path] = content


class Ctx:
    def __init__(self, workdir):
        self.cfg = Config()
        self.cfg.git.repo = str(origin)
        self.cfg.git.dir = str(workdir / "repo")
        self.ssh = LocalSSH()


ctx = Ctx(tmp)
snap = {}

# ---------- clone ----------
res = gitsync.clone(ctx, snap, "pro-1")
check("clone 新仓库", res["created"] is True and "v1" in res["head"], str(res))
res2 = gitsync.clone(ctx, snap, "pro-1")
check("clone 幂等（已有则 fetch）", res2["created"] is False and "v1" in res2["head"], str(res2))

# ---------- sync：无更新 ----------
r = gitsync.sync(ctx, snap, "pro-1")
check("sync 已是最新", r["updated"] is False and not r["blocked"] and not r["error"], str(r))

# ---------- sync：远端有新提交，干净快进 ----------
(seed / "train.sh").write_text("echo v2\n")
sh("git commit -qam v2 && git push -q origin main", cwd=seed)
r = gitsync.sync(ctx, snap, "pro-1")
check("sync 干净快进", r["updated"] and r["old"] != r["new"] and r["message"] == "v2", str(r))
check("sync 后文件已更新", (Path(ctx.cfg.git.dir) / "train.sh").read_text() == "echo v2\n")

# ---------- sync：跟踪文件被改脏，ff 拒绝 ----------
repo = Path(ctx.cfg.git.dir)
(repo / "train.sh").write_text("echo hacked\n")
(repo / "output.log").write_text("产物\n")   # 未跟踪文件
(seed / "train.sh").write_text("echo v3\n")
sh("git commit -qam v3 && git push -q origin main", cwd=seed)
r = gitsync.sync(ctx, snap, "pro-1")
check("sync ff 脏则拒绝", r["blocked"] is True and r["updated"] is False, str(r))
check("sync 回传改动清单", any("train.sh" in l for l in r["dirty"]), str(r["dirty"]))
check("sync 拒绝时不动工作区", (repo / "train.sh").read_text() == "echo hacked\n")

# ---------- sync：reset 丢弃改动但保留未跟踪产物 ----------
r = gitsync.sync(ctx, snap, "pro-1", mode="reset")
check("sync reset 更新成功", r["updated"] and r["message"] == "v3", str(r))
check("reset 丢弃跟踪文件改动", (repo / "train.sh").read_text() == "echo v3\n")
check("reset 保留未跟踪产物", (repo / "output.log").read_text() == "产物\n")

# ---------- sync：stash 收起改动再快进 ----------
(repo / "train.sh").write_text("echo hacked2\n")
(seed / "train.sh").write_text("echo v4\n")
sh("git commit -qam v4 && git push -q origin main", cwd=seed)
r = gitsync.sync(ctx, snap, "pro-1", mode="stash")
check("sync stash 更新成功", r["updated"] and r["message"] == "v4", str(r))
check("stash 后工作区是远端版", (repo / "train.sh").read_text() == "echo v4\n")
stashes = sh("git stash list", cwd=repo)
check("改动进了 stash", "autodl-sync" in stashes, stashes)

# ---------- update：目录缺失自动 clone ----------
ctx2 = Ctx(tmp / "fresh")
(tmp / "fresh").mkdir()
r = gitsync.update(ctx2, snap, "pro-1")
check("update 缺仓库自动 clone", r["updated"] and r["message"] == "(fresh clone)", str(r))

# ---------- 非 git 目录报错 ----------
bad = Ctx(tmp)
bad.cfg.git.dir = str(tmp / "notrepo")
(tmp / "notrepo").mkdir()
try:
    gitsync.sync(bad, snap, "pro-1")
    check("sync 非仓库应报错", False)
except AutoDLError as e:
    check("sync 非仓库报错清晰", "clone" in str(e), str(e))

# ---------- 私有库凭据 ----------
cred = Ctx(tmp)
os.environ["AUTODL_GIT_TOKEN"] = "s3cret/+tok"
ok = gitsync._setup_credentials(cred, snap, "pro-1", "https://github.com/you/proj.git")
line = cred.ssh.files.get("/root/.git-credentials", "")
check("凭据写入实例文件", ok and line.startswith("https://x-access-token:") and "@github.com" in line, line)
check("token URL 转义", "s3cret%2F%2Btok" in line, line)
del os.environ["AUTODL_GIT_TOKEN"]

# ---------- .env 加载优先级 ----------
proj = tmp / "proj"; proj.mkdir()
home = tmp / "home"; (home / ".autodl").mkdir(parents=True)
(proj / ".env").write_text("AUTODL_TOKEN=from-cwd-env\n")
(home / ".autodl" / ".env").write_text("AUTODL_TOKEN=from-home\nAUTODL_GIT_TOKEN=git-home\n")
old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
os.environ["HOME"] = str(home)
os.environ.pop("AUTODL_TOKEN", None); os.environ.pop("AUTODL_GIT_TOKEN", None)
os.chdir(proj)
cfg = load_config()
check("cwd .env 优先于 home", cfg.token == "from-cwd-env", cfg.token)
check("home 兜底补缺(GIT_TOKEN)", os.getenv("AUTODL_GIT_TOKEN") == "git-home", os.getenv("AUTODL_GIT_TOKEN"))

os.environ.pop("AUTODL_TOKEN", None); os.environ.pop("AUTODL_GIT_TOKEN", None)
os.environ["AUTODL_TOKEN"] = "from-real-env"
cfg = load_config()
check("项目 .env 覆盖全局环境变量", cfg.token == "from-cwd-env", cfg.token)

os.environ.pop("AUTODL_TOKEN", None); os.environ.pop("AUTODL_GIT_TOKEN", None)
empty = tmp / "empty"; empty.mkdir(); os.chdir(empty)
os.environ["AUTODL_TOKEN"] = "from-real-env"
cfg = load_config()
check("无项目 .env 时环境变量 > home", cfg.token == "from-real-env", cfg.token)

os.environ.pop("AUTODL_TOKEN", None); os.environ.pop("AUTODL_GIT_TOKEN", None)
cfg = load_config()
check("任意目录 home 兜底生效", cfg.token == "from-home", cfg.token)

os.chdir(old_cwd); os.environ["HOME"] = old_home
os.environ.pop("AUTODL_TOKEN", None); os.environ.pop("AUTODL_GIT_TOKEN", None)

# ---------- CLI 接线 ----------
class CliCtx:
    def __init__(self):
        self.cfg = Config()
        self.cfg.git.repo = str(origin)
        self.cfg.git.dir = str(tmp / "repo")
        self.ssh = LocalSSH()
        class Reg:
            def get_active(self):
                return "pro-1"
        self.reg = Reg()

    def ensure_specific(self, uuid, log=print):
        log("目标实例 pro-1 状态: running")  # 若打到 stdout 会污染 --json 契约
        return {}


args = types.SimpleNamespace(instance=None, mode="ff", dir=None, branch=None, json=True)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_sync(CliCtx(), args)
# --json 契约：stdout 必须是纯 JSON（开机日志走 stderr，不得混入）
check("cli sync --json stdout 纯净", buf.getvalue().count("\n") == 1, repr(buf.getvalue()))
d = json.loads(buf.getvalue())
check("cli sync --json", rc == 0 and d["branch"] == "main" and "new" in d, buf.getvalue())

args2 = types.SimpleNamespace(instance=None, repo=None, branch=None, dir=None, json=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_clone(CliCtx(), args2)
d = json.loads(buf.getvalue())
check("cli clone --json", rc == 0 and d["head"], buf.getvalue())

# ---------- 本地 patch 自动应用 ----------
# 造一个新的干净仓库对（origin + 实例工作区），并在本地生成 patch 文件
porigin = tmp / "porigin.git"
pseed = tmp / "pseed"
sh(f"git init -q --bare {porigin}")
pseed.mkdir()
sh("git init -q -b main .", cwd=pseed)
sh("git config user.email t@t && git config user.name t", cwd=pseed)
(pseed / "app.py").write_text(
    "VERSION = 1\nimport flash_attn  # 本机没有，需 patch 掉\nimport os\nimport sys\nDATA = 'x'\n")
sh("git add -A && git commit -qm v1 && git remote add origin " + str(porigin) + " && git push -q origin main", cwd=pseed)

patch_dir = tmp / "patches"; patch_dir.mkdir()
# 用 git diff 生成标准格式 patch（手写 hunk 行数易失配）：把 import flash_attn 注释掉
_app = pseed / "app.py"; _orig = _app.read_text()
_app.write_text(_orig.replace("import flash_attn  # 本机没有，需 patch 掉",
                              "# import flash_attn  # patched out by autodl"))
# 用原始 stdout（不能 strip：会剥掉 diff 末尾换行，patch 会 corrupt）
_diff = subprocess.run("git diff app.py", shell=True, cwd=str(pseed),
                       capture_output=True, text=True).stdout
_app.write_text(_orig)  # 还原 pseed（origin 保持干净 v1）
(patch_dir / "01-drop-flashattn.patch").write_text(_diff)

class RealWriteSSH(LocalSSH):
    # apply_patches 要把 patch 真写到"远端"路径；本地 bash 场景下就是真实文件
    def write_file(self, snap, path, content, uuid=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(content)


(tmp / "pwork").mkdir()
pctx = Ctx(tmp / "pwork")
pctx.ssh = RealWriteSSH()
pctx.cfg.ssh.remote_workdir = str(tmp / "pwork" / "wd")  # patch 上传落这里（tmp 下，不污染真实 /root）
pctx.cfg.git.repo = str(porigin)
pctx.cfg.git.dir = str(tmp / "pwork" / "repo")
pctx.cfg.git.patches = str(patch_dir)
pctx.cfg._base_dir = tmp  # 绝对路径下 base 无关

check("local_patches 发现 patch", len(gitsync.local_patches(pctx.cfg)) == 1)

gitsync.clone(pctx, snap, "pro-1")
r = gitsync.apply_patches(pctx, snap, "pro-1")
check("patch 首次应用", r["applied"] == ["01-drop-flashattn.patch"] and not r["failed"], str(r))
repo_app = Path(pctx.cfg.git.dir) / "app.py"
check("patch 生效(注释掉 import)", "# import flash_attn" in repo_app.read_text())

# 幂等：再应用一次 → skip，不重复
r2 = gitsync.apply_patches(pctx, snap, "pro-1")
check("patch 幂等(已在则跳过)", r2["skipped"] == ["01-drop-flashattn.patch"] and not r2["applied"], str(r2))

# 上游更新（改动不碰 patch context）+ sync：先 reset 回干净上游、更新，再重放 patch
(pseed / "app.py").write_text(
    "VERSION = 1\nimport flash_attn  # 本机没有，需 patch 掉\nimport os\nimport sys\nDATA = 'x'\n# v2-marker\n")
sh("git commit -qam v2 && git push -q origin main", cwd=pseed)
u = gitsync.update(pctx, snap, "pro-1")
check("update 拉到新版", u["new"] and u.get("message") == "v2", str(u))
check("update 重放 patch", u.get("patches", {}).get("applied") == ["01-drop-flashattn.patch"], str(u.get("patches")))
txt = repo_app.read_text()
check("update 后是新版且 patch 仍在", "# v2-marker" in txt and "# import flash_attn" in txt, txt)

# patch 从不 commit：实例仓库工作区脏但 HEAD 未变（不会 push）
head_before = sh("git rev-parse HEAD", cwd=Path(pctx.cfg.git.dir))
st = sh("git status --porcelain", cwd=Path(pctx.cfg.git.dir))
check("patch 是工作区改动(未提交)", "app.py" in st, st)
log_cnt = sh("git rev-list --count HEAD", cwd=Path(pctx.cfg.git.dir))
check("patch 未进 git 历史(不会被 push)", log_cnt == "2", log_cnt)  # 只有 origin 的 v1,v2

# 冲突 patch：上游把被 patch 的行删了 → apply 失败但记 failed，不崩
(patch_dir / "02-bad.patch").write_text(
    "--- a/nonexist.py\n+++ b/nonexist.py\n@@ -1 +1 @@\n-old\n+new\n")
r3 = gitsync.apply_patches(pctx, snap, "pro-1")
check("冲突 patch 记 failed 不崩", "02-bad.patch" in r3["failed"], str(r3))
check("冲突不影响其它 patch", "01-drop-flashattn.patch" not in r3["failed"], str(r3))

# CLI cmd_patch
class PatchCliCtx(Ctx):
    def __init__(self, base):
        super().__init__(base)
        class R(object):
            def get_active(self): return "pro-1"
        self.reg = R()
    def ensure_specific(self, uuid, log=print): return {}
(patch_dir / "02-bad.patch").unlink()  # 去掉坏 patch
pcli = PatchCliCtx(tmp / "pwork")
pcli.ssh = RealWriteSSH()
pcli.cfg.ssh.remote_workdir = str(tmp / "pwork" / "wd")
pcli.cfg.git.repo = str(porigin); pcli.cfg.git.dir = str(tmp / "pwork" / "repo")
pcli.cfg.git.patches = str(patch_dir); pcli.cfg._base_dir = tmp
pargs = types.SimpleNamespace(instance=None, dir=None, json=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_patch(pcli, pargs)
d = json.loads(buf.getvalue())
check("cli patch --json 纯净", buf.getvalue().count("\n") == 1, repr(buf.getvalue()))
check("cli patch 结果", rc == 0 and d["total"] == 1, buf.getvalue())

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
