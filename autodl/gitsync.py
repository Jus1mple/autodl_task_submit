"""实例上的项目仓库管理 —— 支撑"本地改码 → git push → 实例 sync → 重跑"的迭代循环。

设计原则：
- 实例上的仓库是**部署副本**：只从 remote 拉取，绝不在实例上提交。远端（GitHub 等）
  是唯一真相源，所以更新用 fetch + ff/reset，不做双向合并。
- 产物写到仓库外（约定 <数据盘>/outputs/），这样 sync 永远是干净快进。真被改脏时
  三种策略（mode）：
    ff（默认）  仅当跟踪文件无改动才快进；脏则拒绝并回传改动清单，绝不丢数据
    stash       把改动（含未跟踪文件）收进 git stash 再快进，事后可在实例上恢复
    reset       reset --hard 到 origin/<branch>：丢弃跟踪文件的改动，
                但**保留未跟踪文件**（生成的产物/数据不会被删）
- 私有 https 仓库：本地环境变量 AUTODL_GIT_TOKEN 有值时，clone 会把凭据写进实例的
  ~/.git-credentials（0600 + credential.helper store），token 不进命令行/日志。
- 每个操作单次 SSH 往返：一段脚本 + @@ 标记回传，本地解析成结构化结果。
"""
from __future__ import annotations

import os
import posixpath
from urllib.parse import quote

from .errors import AutoDLError
from .ssh import _shq

SYNC_MODES = ("ff", "stash", "reset")


def repo_dir(cfg):
    return cfg.git.dir or posixpath.join(cfg.ssh.remote_workdir, "repo")


def _ensure_git(ctx, snap, uuid):
    out, _e, _c = ctx.ssh.run(snap, "command -v git || true", uuid)
    if out.strip():
        return True
    ctx.ssh.run(snap, "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 || true", uuid)
    out, _e, _c = ctx.ssh.run(snap, "command -v git || true", uuid)
    return bool(out.strip())


def _setup_credentials(ctx, snap, uuid, repo):
    """AUTODL_GIT_TOKEN 有值且是 https 仓库时，把凭据写进实例（写文件，不走命令行）。"""
    token = os.getenv("AUTODL_GIT_TOKEN", "")
    if not token or not repo.startswith("https://"):
        return False
    host = repo[len("https://"):].split("/", 1)[0]
    line = f"https://x-access-token:{quote(token, safe='')}@{host}\n"
    ctx.ssh.write_file(snap, "/root/.git-credentials", line, uuid)
    ctx.ssh.run(snap, "chmod 600 /root/.git-credentials && "
                      "git config --global credential.helper store", uuid)
    return True


def _parse_marker(out, name):
    for line in out.splitlines():
        if line.startswith(f"@@{name} "):
            return line[len(name) + 3:].strip()
        if line.strip() == f"@@{name}":
            return ""
    return None


def _parse_block(out, name):
    lines, inside = [], False
    for line in out.splitlines():
        if line.strip() == f"@@{name}_BEGIN":
            inside = True
        elif line.strip() == f"@@{name}_END":
            inside = False
        elif inside:
            lines.append(line)
    return lines


def clone(ctx, snap, uuid, repo=None, branch=None, dir=None):
    """在实例上确保项目仓库存在（幂等）：没有则 clone，已有则 fetch。
    返回 {dir, branch, head, created, credentials}。"""
    cfg = ctx.cfg
    repo = repo or cfg.git.repo
    if not repo:
        raise AutoDLError("未配置仓库地址：加 --repo，或在 autodl.yaml 写 git.repo")
    branch = branch or cfg.git.branch or "main"
    d = dir or repo_dir(cfg)
    if not _ensure_git(ctx, snap, uuid):
        raise AutoDLError("实例上没有 git 且自动安装失败")
    cred = _setup_credentials(ctx, snap, uuid, repo)
    parent = posixpath.dirname(d.rstrip("/")) or "/"
    script = (
        f'export GIT_TERMINAL_PROMPT=0\n'
        f'if [ -d {_shq(d)}/.git ]; then\n'
        f'  cd {_shq(d)} && git fetch origin {_shq(branch)} 2>&1 && echo "@@EXISTS"\n'
        f'else\n'
        f'  mkdir -p {_shq(parent)} 2>/dev/null\n'
        f'  git clone --branch {_shq(branch)} {_shq(repo)} {_shq(d)} 2>&1 && echo "@@CLONED"\n'
        f'fi\n'
        f'cd {_shq(d)} && echo "@@HEAD $(git rev-parse --short HEAD) $(git log -1 --format=%s)"'
    )
    out, err, code = ctx.ssh.run(snap, script, uuid)
    head = _parse_marker(out, "HEAD")
    if code != 0 or head is None:
        raise AutoDLError(f"clone/fetch 失败: {(out + err)[-400:].strip()}")
    return {"dir": d, "branch": branch, "head": head,
            "created": _parse_marker(out, "CLONED") is not None, "credentials": cred}


def sync(ctx, snap, uuid, mode="ff", dir=None, branch=None):
    """把实例仓库更新到 origin/<branch>。返回结构化结果：
    {dir, branch, mode, old, new, updated, dirty(改动清单), blocked, error}
    - blocked=True：ff 模式发现跟踪文件被改，拒绝更新（改动清单在 dirty 里）
    - error 非空：fetch 失败 / 快进失败（如未跟踪文件与新提交同名冲突）等，工作区未动
    """
    if mode not in SYNC_MODES:
        raise ValueError(f"未知 sync 模式: {mode}（应为 {'/'.join(SYNC_MODES)}）")
    cfg = ctx.cfg
    branch = branch or cfg.git.branch or "main"
    d = dir or repo_dir(cfg)
    target = f"origin/{branch}"
    if mode == "ff":
        apply_cmd = (
            'if [ -n "$TRACKED" ]; then echo "@@BLOCKED"; else\n'
            f'  OUT=$(git merge --ff-only {_shq(target)} 2>&1) || '
            '{ echo "@@FAIL_BEGIN"; echo "$OUT"; echo "@@FAIL_END"; }\n'
            'fi'
        )
    elif mode == "stash":
        apply_cmd = (
            'git stash push -u -m "autodl-sync" >/dev/null 2>&1 || true\n'
            f'OUT=$(git merge --ff-only {_shq(target)} 2>&1) || '
            '{ echo "@@FAIL_BEGIN"; echo "$OUT"; echo "@@FAIL_END"; }'
        )
    else:  # reset：丢弃跟踪文件改动，未跟踪文件（产物/数据）保留
        apply_cmd = (
            f'OUT=$(git reset --hard {_shq(target)} 2>&1) || '
            '{ echo "@@FAIL_BEGIN"; echo "$OUT"; echo "@@FAIL_END"; }'
        )
    script = (
        f'export GIT_TERMINAL_PROMPT=0\n'
        f'cd {_shq(d)} 2>/dev/null || {{ echo "@@NO_REPO"; exit 0; }}\n'
        f'[ -d .git ] || {{ echo "@@NO_REPO"; exit 0; }}\n'
        f'FOUT=$(git fetch origin {_shq(branch)} 2>&1) || '
        '{ echo "@@FAIL_BEGIN"; echo "$FOUT"; echo "@@FAIL_END"; exit 0; }\n'
        'echo "@@OLD $(git rev-parse --short HEAD)"\n'
        'ST=$(git status --porcelain)\n'
        'TRACKED=$(git status --porcelain | grep -v "^??" || true)\n'
        'if [ -n "$ST" ]; then echo "@@DIRTY_BEGIN"; echo "$ST"; echo "@@DIRTY_END"; fi\n'
        f'{apply_cmd}\n'
        'echo "@@NEW $(git rev-parse --short HEAD)"\n'
        'git log -1 --format="@@MSG %s"'
    )
    out, err, code = ctx.ssh.run(snap, script, uuid)
    if _parse_marker(out, "NO_REPO") is not None:
        raise AutoDLError(f"实例上 {d} 不是 git 仓库——先 `autodl clone`")
    old, new = _parse_marker(out, "OLD"), _parse_marker(out, "NEW")
    fail = _parse_block(out, "FAIL")
    if code != 0 or (old is None and not fail):
        raise AutoDLError(f"sync 失败: {(out + err)[-400:].strip()}")
    return {
        "dir": d, "branch": branch, "mode": mode,
        "old": old, "new": new, "updated": bool(old and new and old != new),
        "dirty": _parse_block(out, "DIRTY"),
        "blocked": _parse_marker(out, "BLOCKED") is not None,
        "error": "\n".join(fail).strip(),
        "message": _parse_marker(out, "MSG") or "",
    }


def update(ctx, snap, uuid, mode="ff", repo=None, branch=None, dir=None):
    """clone-if-missing + sync：run --sync 用的一步到位入口。返回 sync() 的结构。"""
    cfg = ctx.cfg
    d = dir or repo_dir(cfg)
    out, _e, _c = ctx.ssh.run(snap, f'[ -d {_shq(d)}/.git ] && echo YES || echo NO', uuid)
    if out.strip().startswith("NO"):
        info = clone(ctx, snap, uuid, repo=repo, branch=branch, dir=d)
        return {"dir": d, "branch": info["branch"], "mode": mode, "old": None,
                "new": info["head"].split()[0] if info["head"] else None, "updated": True,
                "dirty": [], "blocked": False, "error": "", "message": "(fresh clone)"}
    return sync(ctx, snap, uuid, mode=mode, dir=d, branch=branch)
