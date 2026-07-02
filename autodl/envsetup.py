"""实例环境准备（autodl setup / run --setup）—— 按仓库里的依赖声明装环境。

来自场景分析的设计决策：
- **幂等**：把「安装脚本 + 各依赖声明文件内容」的 hash 记在实例系统盘
  ~/.autodl_env_hash；未变则秒级跳过（--force 强制）。hash 跟系统盘走：
  换实例/换镜像自然失效 → 自动重装；snapshot-env 固化的镜像自带 hash → 秒跳。
- **声明来源优先级**：env.setup 自定义命令（最高）> setup.sh > environment.yml
  > requirements.txt > pyproject.toml；都没有则明确报错，绝不瞎猜。
- **国内网络**：pip 自动挂镜像源（env.pip_index）；env.academic_turbo 先
  source /etc/network_turbo（对 github/HF 有效）。pip 一律 --no-cache-dir，
  防止缓存撑爆系统盘。
- **conda/pip 可用性**：ssh exec 是非交互 shell 不读 .bashrc，安装前先探测并
  source conda profile（与 AutoDL 官方镜像的目录约定一致）。
- **执行走 tasks 管线**：安装本身就是一次 run（记台账、可 --background 脱机、
  autodl logs 查进度），失败非零退出，调用方（run --setup）必须阻断任务。
"""
from __future__ import annotations

from . import tasks
from .errors import AutoDLError
from .gitsync import repo_dir
from .ssh import _shq

HASH_NAME = ".autodl_env_hash"

# 自动探测顺序（文件名, 安装命令模板）。idx 为 pip 镜像源参数。
_DETECT_ORDER = ("setup.sh", "environment.yml", "requirements.txt", "pyproject.toml")

# ssh exec 是非交互 shell，conda/pip 不一定在 PATH；先按官方镜像目录约定 source
_CONDA_SRC = (
    'for c in /root/miniconda3 /opt/conda /root/anaconda3 /root/miniconda; do '
    '[ -f "$c/etc/profile.d/conda.sh" ] && . "$c/etc/profile.d/conda.sh" && '
    'conda activate base 2>/dev/null && break; done; true'
)


def _auto_command(fname, pip_index):
    idx = f" -i {pip_index}" if pip_index else ""
    return {
        "setup.sh": "bash setup.sh",
        "environment.yml": "conda env update -n base -f environment.yml",
        "requirements.txt": f"pip install --no-cache-dir{idx} -r requirements.txt",
        "pyproject.toml": f"pip install --no-cache-dir{idx} -e .",
    }[fname]


def plan(ctx, snap, uuid, dir=None):
    """决定怎么装：返回 (安装命令, 来源说明)。探测不到则抛错（绝不瞎猜）。"""
    cfg = ctx.cfg
    d = dir or repo_dir(cfg)
    if cfg.env.setup.strip():
        return cfg.env.setup.strip(), "env.setup"
    if not cfg.env.auto:
        raise AutoDLError("env.auto=false 且未配置 env.setup，不知道怎么装环境")
    probes = " ".join(_shq(f) for f in _DETECT_ORDER)
    script = (
        f'cd {_shq(d)} 2>/dev/null || {{ echo "@@NODIR"; exit 0; }}\n'
        f'for f in {probes}; do if [ -f "$f" ]; then echo "@@FOUND $f"; exit 0; fi; done\n'
        f'echo "@@NONE"'
    )
    out, _e, _c = ctx.ssh.run(snap, script, uuid)
    if "@@NODIR" in out:
        raise AutoDLError(f"实例上没有 {d} —— 先 `autodl clone`（或 --dir 指定仓库目录）")
    found = next((l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("@@FOUND ")), None)
    if not found:
        raise AutoDLError(
            f"{d} 里没找到依赖声明（{'/'.join(_DETECT_ORDER)}）——"
            "请在 autodl.yaml 配 env.setup 自定义安装命令")
    return _auto_command(found, cfg.env.pip_index), found


def _compose(cfg, d, command):
    """拼出实际执行的安装脚本（也是 hash 的输入之一：改配置即触发重装）。"""
    lines = [f'cd {_shq(d)} || exit 1', _CONDA_SRC]
    if cfg.env.academic_turbo:
        lines.append("source /etc/network_turbo 2>/dev/null || true")
    lines.append(command)
    return "\n".join(lines)


def _hashes(ctx, snap, uuid, d, script):
    """一次往返拿 (当前 hash, 实例上已记录的 hash)。"""
    files = " ".join(_DETECT_ORDER)
    cmd = (
        f'cd {_shq(d)} 2>/dev/null || {{ echo "@@NODIR"; exit 0; }}\n'
        f'H=$( (echo {_shq(script)}; cat {files} 2>/dev/null) | md5sum | cut -d" " -f1)\n'
        f'echo "@@CUR $H"\n'
        f'echo "@@OLD $(cat "$HOME/{HASH_NAME}" 2>/dev/null)"'
    )
    out, _e, _c = ctx.ssh.run(snap, cmd, uuid)
    if "@@NODIR" in out:
        raise AutoDLError(f"实例上没有 {d} —— 先 `autodl clone`")
    cur = next((l[6:].strip() for l in out.splitlines() if l.startswith("@@CUR ")), "")
    old = next((l[6:].strip() for l in out.splitlines() if l.startswith("@@OLD")), "")
    return cur, old


def run_setup(ctx, snap, uuid, *, dir=None, force=False, background=False,
              stream=None, log=None):
    """准备环境。返回结构化结果：
    - 跳过：{skipped: True, source, command, hash}
    - 后台：{skipped: False, background: True, run_id, pid, log, ...}
    - 前台：{skipped: False, background: False, run_id, exit_code, stdout, stderr, ...}
    安装成功才写 hash 文件（失败下次仍会重试）。"""
    cfg = ctx.cfg
    d = dir or repo_dir(cfg)
    command, source = plan(ctx, snap, uuid, dir=d)
    script = _compose(cfg, d, command)
    cur, old = _hashes(ctx, snap, uuid, d, script)
    if cur and cur == old and not force:
        return {"skipped": True, "source": source, "command": command, "hash": cur}
    # 成功（&& 链）才记 hash；退出码 = 安装命令的
    value = script + f' && echo {cur} > "$HOME/{HASH_NAME}"'
    rid = tasks.make_run_id("setup")
    common = dict(mode="remote", value=value, run_id=rid,
                  name=f"setup({source})", tag="setup",
                  config_extra={"setup_source": source, "setup_command": command})
    if background:
        meta = tasks.run_background(ctx, snap, uuid, **common)
        return {"skipped": False, "background": True, "source": source, "command": command, **meta}
    res = tasks.run_foreground(ctx, snap, uuid, stream=stream, log=log, **common)
    return {"skipped": False, "background": False, "source": source, "command": command, **res}
