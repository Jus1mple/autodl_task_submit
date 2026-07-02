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
from pathlib import Path
from urllib.parse import quote

from .errors import AutoDLError
from .ssh import _shq

SYNC_MODES = ("ff", "stash", "reset")


def repo_dir(cfg):
    return cfg.git.dir or posixpath.join(cfg.ssh.remote_workdir, "repo")


def local_patches(cfg):
    """本地 patch 文件列表（排序，*.patch）。cfg.git.patches 是相对 autodl.yaml 的目录或单文件。
    用 NN-name.patch 前缀可控制应用顺序。"""
    pd = (cfg.git.patches or "").strip()
    if not pd:
        return []
    p = Path(pd)
    if not p.is_absolute():
        p = Path(getattr(cfg, "_base_dir", Path.cwd())) / p
    if p.is_dir():
        return sorted(str(f) for f in p.glob("*.patch"))
    return [str(p)] if p.exists() else []


def apply_patches(ctx, snap, uuid, dir=None, log=None):
    """把本地 patch 目录的 *.patch 上传到实例并 git apply（幂等）。
    - 已应用（git apply --reverse --check 通过）→ 跳过
    - 能干净应用 → git apply
    - 冲突（多半是上游改了同一处）→ 记 failed，不中断其余
    返回 {applied, skipped, failed, total, dir}。**只改工作区，绝不 commit/push**——
    改的是别人的仓库时，你的适配补丁不会进实例 git 历史，也就不可能被推回上游。"""
    cfg = ctx.cfg
    d = dir or repo_dir(cfg)
    patches = local_patches(cfg)
    res = {"applied": [], "skipped": [], "failed": [], "total": len(patches), "dir": d}
    if not patches:
        return res
    remote_dir = posixpath.join(cfg.ssh.remote_workdir, ".autodl_patches")
    for pf in patches:
        name = os.path.basename(pf)
        try:
            content = Path(pf).read_text(encoding="utf-8")
        except OSError as e:
            res["failed"].append(name)
            if log:
                log(f"  ⚠️ patch 读取失败 {name}: {e}")
            continue
        remote_pf = posixpath.join(remote_dir, name)
        ctx.ssh.write_file(snap, remote_pf, content, uuid)
        script = (
            f'cd {_shq(d)} 2>/dev/null || {{ echo "@@NOREPO"; exit 0; }}\n'
            f'if git apply --reverse --check {_shq(remote_pf)} 2>/dev/null; then echo "@@SKIP"; \n'
            f'elif git apply --check {_shq(remote_pf)} 2>/dev/null; then '
            f'git apply {_shq(remote_pf)} && echo "@@APPLIED"; else echo "@@FAIL"; fi'
        )
        out, _e, _c = ctx.ssh.run(snap, script, uuid)
        if "@@APPLIED" in out:
            res["applied"].append(name)
            if log:
                log(f"  patch 应用: {name}")
        elif "@@SKIP" in out:
            res["skipped"].append(name)
            if log:
                log(f"  patch 已在: {name}（跳过）")
        else:
            res["failed"].append(name)
            if log:
                log(f"  ⚠️ patch 冲突: {name}（上游可能已改动同处，需更新 patch）")
    return res


def _net_prelude(cfg):
    """clone/fetch 前置：国内实例拉 GitHub 大仓库常因传输中断失败，这里
    可选走 AutoDL 学术加速 + 放大缓冲/放宽慢速超时，显著降低 GnuTLS/EOF 中断。"""
    lines = []
    if cfg.git.turbo:
        lines.append("source /etc/network_turbo 2>/dev/null || true")
    lines += [
        "git config --global http.postBuffer 524288000 2>/dev/null || true",
        "git config --global http.lowSpeedLimit 1000 2>/dev/null || true",
        "git config --global http.lowSpeedTime 60 2>/dev/null || true",
    ]
    return "".join(l + "\n" for l in lines)


def _clone_depth_flag(cfg):
    """仅用于初次 clone：浅克隆规避国内拉大仓库中断。后续 fetch 不限深——
    浅仓库的增量 fetch 本就很小，且保留历史连接让 ff merge 仍可用。"""
    return f" --depth {int(cfg.git.depth)}" if cfg.git.depth and int(cfg.git.depth) > 0 else ""


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
        f'{_net_prelude(cfg)}'
        f'if [ -d {_shq(d)}/.git ]; then\n'
        f'  cd {_shq(d)} && git fetch origin {_shq(branch)} 2>&1 && echo "@@EXISTS"\n'
        f'else\n'
        f'  mkdir -p {_shq(parent)} 2>/dev/null\n'
        f'  git clone{_clone_depth_flag(cfg)} --branch {_shq(branch)} {_shq(repo)} {_shq(d)} 2>&1 && echo "@@CLONED"\n'
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
        f'{_net_prelude(cfg)}'
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


def update(ctx, snap, uuid, mode="ff", repo=None, branch=None, dir=None, log=None):
    """clone-if-missing + sync + 应用本地 patch：run --sync 用的一步到位入口。
    配了 git.patches 时，为了让 patch 每次叠在干净上游之上：更新走 reset（丢弃上一轮
    的 patch 改动、保留未跟踪产物），再重放 patch。返回 sync() 的结构 + "patches" 字段。"""
    cfg = ctx.cfg
    d = dir or repo_dir(cfg)
    has_patches = bool(local_patches(cfg))
    out, _e, _c = ctx.ssh.run(snap, f'[ -d {_shq(d)}/.git ] && echo YES || echo NO', uuid)
    if out.strip().startswith("NO"):
        info = clone(ctx, snap, uuid, repo=repo, branch=branch, dir=d)
        result = {"dir": d, "branch": info["branch"], "mode": mode, "old": None,
                  "new": info["head"].split()[0] if info["head"] else None, "updated": True,
                  "dirty": [], "blocked": False, "error": "", "message": "(fresh clone)"}
    else:
        # 有 patch 时用 reset：旧 patch 是 tracked 改动，reset 丢弃它们、保留产物，再重放
        result = sync(ctx, snap, uuid, mode="reset" if has_patches else mode, dir=d, branch=branch)
    if has_patches and not result.get("error") and not result.get("blocked"):
        result["patches"] = apply_patches(ctx, snap, uuid, dir=d, log=log)
    return result
