"""SSH 连接层 —— 半数功能的命根子。

要点：
- 连接带重试/退避；失败时**回退查实例状态做交叉确认**，把根因分类为
  实例已关机 / 跳板不通 / 端口未就绪 / 认证失败 / 超时（见 errors.SSHUnavailable）。
  这样 idle-guard 等不会把"网络不通"误判成"空闲该关机"。
- 凭据安全：首连后把本地公钥注入远端 authorized_keys，之后 ssh/rsync 走密钥免密，
  root_password 只在内存用一次，绝不进命令行/ps/日志。
- 后台非阻塞执行：setsid+nohup 把任务拉起为脱离会话的后台进程，记录 pid/log/exit 文件，
  命令立即返回；poll() 用 `kill -0` 探活 + 读 exit 文件判定完成。
"""
from __future__ import annotations

import os
import posixpath
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import paramiko

from .errors import SSHUnavailable

_RUN_ID_RE = re.compile(r"[A-Za-z0-9_.\-]+")
# 产物 glob 会拼进远端 shell 做 glob 展开，只允许安全字符，防注入
_ARTIFACT_PAT_RE = re.compile(r"^[\w./*?\[\]-]+$")


def _safe_run_id(run_id: str) -> str:
    """run_id 会拼进远端文件路径，必须只含安全字符，防 shell 注入/路径穿越。"""
    rid = str(run_id)
    if not _RUN_ID_RE.fullmatch(rid):
        raise ValueError(f"非法 run_id: {run_id!r}（只允许字母数字与 _.- ）")
    return rid

_SSH_CONFIG_PATH = Path.home() / ".ssh" / "config"
_KEY_CANDIDATES = ("id_ed25519", "id_rsa", "id_ecdsa")
_BEGIN = "# >>> autodl managed >>>"
_END = "# <<< autodl managed <<<"


def _mask(s: str) -> str:
    return "****" if s else ""


def detect_or_create_key() -> str:
    """返回本地私钥路径，没有则生成 ed25519。返回 '' 表示无法获取（不致命）。"""
    ssh_dir = Path.home() / ".ssh"
    for name in _KEY_CANDIDATES:
        pub = ssh_dir / f"{name}.pub"
        if pub.exists():
            return str(ssh_dir / name)
    # 没有任何 key，生成一个 ed25519
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
            check=True,
        )
        return str(key_path)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


class SSHManager:
    def __init__(self, cfg, client_api):
        """cfg: SSHConfig; client_api: AutoDLClient（用于失败时交叉确认实例状态）。"""
        self.cfg = cfg
        self.api = client_api

    # ---------------- 连接 ----------------
    def _classify_failure(self, exc, instance_uuid):
        st = self.api.status_or_none(instance_uuid) if instance_uuid else None
        if st is not None and st != "running":
            return SSHUnavailable(SSHUnavailable.INSTANCE_NOT_RUNNING,
                                  f"实例状态={st}", status=st)
        if isinstance(exc, paramiko.AuthenticationException):
            return SSHUnavailable(SSHUnavailable.AUTH_FAILED, str(exc), status=st)
        if isinstance(exc, socket.timeout):
            return SSHUnavailable(SSHUnavailable.TIMEOUT, str(exc), status=st)
        if isinstance(exc, (ConnectionRefusedError, paramiko.SSHException)):
            # running 但连不上：多半端口/ sshd 还没就绪
            return SSHUnavailable(SSHUnavailable.PORT_NOT_READY, str(exc), status=st)
        if isinstance(exc, (OSError, socket.error)):
            return SSHUnavailable(SSHUnavailable.PROXY_UNREACHABLE, str(exc), status=st)
        return SSHUnavailable(SSHUnavailable.UNKNOWN, str(exc), status=st)

    def connect(self, snapshot, instance_uuid=None):
        """用密码连上，返回 paramiko client。失败按根因抛 SSHUnavailable。"""
        host = snapshot["proxy_host"]
        port = snapshot["ssh_port"]
        password = snapshot["root_password"]
        last = None
        for attempt in range(self.cfg.connect_retries):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(hostname=host, port=port, username=self.cfg.user,
                               password=password, timeout=self.cfg.connect_timeout,
                               banner_timeout=self.cfg.connect_timeout,
                               auth_timeout=self.cfg.connect_timeout)
                return client
            except Exception as e:  # noqa: BLE001 - 统一分类
                last = e
                client.close()
                # 认证失败重试无意义，直接抛
                if isinstance(e, paramiko.AuthenticationException):
                    raise self._classify_failure(e, instance_uuid)
                if attempt + 1 < self.cfg.connect_retries:
                    time.sleep(self.cfg.retry_interval)
        raise self._classify_failure(last, instance_uuid)

    # ---------------- 同步执行 ----------------
    def run(self, snapshot, command, instance_uuid=None, stream=None, max_capture=None):
        """同步执行一条命令，返回 (stdout, stderr, exit_code)。
        stream: 可选回调，边执行边收到输出块（长任务实时回显）；stdout/stderr 都会喂给它。
        max_capture: 累积到内存的 stdout/stderr 各自只保留末尾约这么多字符（防大输出 OOM）。"""
        client = self.connect(snapshot, instance_uuid)
        try:
            return _exec(client, command, stream, max_capture)
        finally:
            client.close()

    # ---------------- 凭据安全：注入公钥免密 ----------------
    def ensure_key_access(self, snapshot, instance_uuid=None):
        """把本地公钥写入远端 authorized_keys（幂等）。返回本地私钥路径或 ''。"""
        key_path = self.cfg.identity_file or detect_or_create_key()
        if not key_path:
            return ""
        pub = Path(key_path + ".pub")
        if not pub.exists():
            return ""
        pubkey = pub.read_text().strip()
        client = self.connect(snapshot, instance_uuid)
        sftp = None
        try:
            sftp = client.open_sftp()
            try:
                sftp.mkdir("/root/.ssh")
            except IOError:
                pass
            # 幂等追加：仅当不存在时写入
            cmd = (
                "mkdir -p /root/.ssh && chmod 700 /root/.ssh && touch /root/.ssh/authorized_keys && "
                "grep -qxF %s /root/.ssh/authorized_keys || echo %s >> /root/.ssh/authorized_keys"
                % (_shq(pubkey), _shq(pubkey))
            )
            _in, out, err = client.exec_command(cmd)
            out.channel.recv_exit_status()
        finally:
            if sftp:
                sftp.close()
            client.close()
        return key_path

    def write_ssh_config(self, snapshot, alias=None, identity_file=None):
        """在 ~/.ssh/config 写入/更新一个 Host 别名块，便于 `ssh <alias>` 与 VSCode Remote-SSH。"""
        alias = alias or self.cfg.config_alias
        host = snapshot["proxy_host"]
        port = snapshot["ssh_port"]
        block = [
            _BEGIN,
            f"Host {alias}",
            f"    HostName {host}",
            f"    Port {port}",
            f"    User {self.cfg.user}",
            "    StrictHostKeyChecking accept-new",
            "    ServerAliveInterval 30",
        ]
        if identity_file:
            block.append(f"    IdentityFile {identity_file}")
            block.append("    IdentitiesOnly yes")
        block.append(_END)
        block_text = "\n".join(block) + "\n"

        _SSH_CONFIG_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        existing = _SSH_CONFIG_PATH.read_text() if _SSH_CONFIG_PATH.exists() else ""
        new = _replace_block(existing, block_text)
        _SSH_CONFIG_PATH.write_text(new)
        try:
            os.chmod(_SSH_CONFIG_PATH, 0o600)
        except OSError:
            pass
        return alias

    # ---------------- 文件写入 ----------------
    def write_file(self, snapshot, path, content, instance_uuid=None):
        """把文本写到实例上的指定路径（自动建父目录）。"""
        client = self.connect(snapshot, instance_uuid)
        sftp = None
        try:
            sftp = client.open_sftp()
            parent = posixpath.dirname(path)
            if parent:
                _mkdirs(sftp, parent)
            with sftp.open(path, "w") as f:
                f.write(content)
        finally:
            if sftp:
                sftp.close()
            client.close()

    # ---------------- 后台非阻塞执行 ----------------
    def run_script(self, snapshot, script_text, run_id, instance_uuid=None, prelude="",
                   stream=None, max_capture=None, runner="bash"):
        """上传一段脚本到数据盘并**同步**执行，返回 (stdout, stderr, exit_code)。
        prelude: 在脚本前于同一 shell 里执行的命令（如清理旧 metrics.json），以 ';' 结尾。
        max_capture: 见 run()——限制累积到内存的输出量，防大输出 OOM。
        runner: 执行器，默认 bash；任务时限时为 'timeout -k 60 N bash'。"""
        run_id = _safe_run_id(run_id)
        wd = self.cfg.remote_workdir
        task = f"{wd}/task_{run_id}.sh"
        client = self.connect(snapshot, instance_uuid)
        sftp = None
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, wd)
            with sftp.open(task, "w") as f:
                f.write(script_text)
            sftp.close(); sftp = None
            return _exec(client, f"{prelude}{runner} {task}", stream, max_capture)
        finally:
            if sftp:
                sftp.close()
            client.close()

    def run_background_command(self, snapshot, command, run_id, instance_uuid=None, prelude=""):
        """把任意命令拉起为脱离会话的后台进程，立即返回 {pid, log, exit_file, workdir}。"""
        run_id = _safe_run_id(run_id)
        wd = self.cfg.remote_workdir
        logs = f"{wd}/logs"
        log_file = f"{logs}/{run_id}.log"
        exit_file = f"{logs}/{run_id}.exit"
        pid_file = f"{logs}/{run_id}.pid"

        client = self.connect(snapshot, instance_uuid)
        sftp = None
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, logs)
            sftp.close(); sftp = None
            # setsid 完全脱离会话；exit code 落 exit_file；pid 落 pid_file。路径一律转义。
            # prelude 在重定向之外执行，exit_file 只记任务本体的退出码。
            inner = _bg_inner(prelude, command, log_file, exit_file)
            launch = (
                f"setsid bash -c {_shq(inner)} "
                f"</dev/null >/dev/null 2>&1 & echo $! > {_shq(pid_file)}; cat {_shq(pid_file)}"
            )
            _in, out, err = client.exec_command(launch)
            pid = out.read().decode().strip()
            out.channel.recv_exit_status()
            if not pid.isdigit():
                pid = ""  # 捕获失败则置空，poll 仅凭 exit_file 判定，避免把运行中误报为完成
        finally:
            if sftp:
                sftp.close()
            client.close()
        return {"pid": pid, "log": log_file, "exit_file": exit_file, "workdir": wd}

    def run_background(self, snapshot, script_text, run_id, instance_uuid=None, prelude="",
                       runner="bash"):
        """上传一段脚本到数据盘并后台执行。返回 {pid, log, exit_file, workdir}。"""
        run_id = _safe_run_id(run_id)
        wd = self.cfg.remote_workdir
        task_file = f"{wd}/task_{run_id}.sh"
        client = self.connect(snapshot, instance_uuid)
        sftp = None
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, wd)
            with sftp.open(task_file, "w") as f:
                f.write(script_text)
            sftp.close(); sftp = None
        finally:
            if sftp:
                sftp.close()
            client.close()
        return self.run_background_command(snapshot, f"cd {_shq(wd)} && {runner} {_shq(task_file)}",
                                           run_id, instance_uuid, prelude=prelude)

    def poll(self, snapshot, run_meta, instance_uuid=None):
        """返回 ('running'|'done', exit_code|None)。"""
        pid = (run_meta.get("pid") or "").strip()
        exit_file = run_meta["exit_file"]
        if pid.isdigit():
            cmd = (
                f"if [ -f {_shq(exit_file)} ]; then echo DONE $(cat {_shq(exit_file)}); "
                f"elif kill -0 {pid} 2>/dev/null; then echo RUNNING; "
                f"else echo DONE unknown; fi"
            )
        else:
            # 没有可靠 pid：只凭 exit_file 判定，没有就当仍在运行（不误报完成）
            cmd = (
                f"if [ -f {_shq(exit_file)} ]; then echo DONE $(cat {_shq(exit_file)}); "
                f"else echo RUNNING; fi"
            )
        so, _se, _c = self.run(snapshot, cmd, instance_uuid)
        parts = so.split()
        if parts and parts[0] == "RUNNING":
            return "running", None
        code = None
        if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
            code = int(parts[1])
        return "done", code

    def kill_background(self, snapshot, run_meta, instance_uuid=None, grace=5):
        """终止后台任务：pid 是 setsid 会话首进程，kill 整个进程组（含子进程）。
        先 TERM、等 grace 秒、再 KILL；exit_file 不存在则写 137，让 poll 判定为完成。
        返回 True=发出了 kill；pid 不可用时只写 exit_file 返回 False。"""
        pid = (run_meta.get("pid") or "").strip()
        exit_file = run_meta["exit_file"]
        if pid.isdigit():
            cmd = (f"kill -TERM -- -{pid} 2>/dev/null; sleep {int(grace)}; "
                   f"kill -KILL -- -{pid} 2>/dev/null; "
                   f"[ -f {_shq(exit_file)} ] || echo 137 > {_shq(exit_file)}; echo KILLED")
        else:
            cmd = f"[ -f {_shq(exit_file)} ] || echo 137 > {_shq(exit_file)}; echo NOPID"
        so, _se, _c = self.run(snapshot, cmd, instance_uuid)
        return "KILLED" in so

    def tail(self, snapshot, log_file, lines=50, instance_uuid=None):
        so, _se, _c = self.run(snapshot, f"tail -n {int(lines)} {_shq(log_file)} 2>/dev/null", instance_uuid)
        return so

    def follow(self, snapshot, log_file, exit_file, pid, lines=40, stream=None, instance_uuid=None):
        """实时跟随后台任务日志（tail -f），任务结束（exit_file 出现或 pid 消失）后自动退出。
        输出通过 stream 回调实时回显（tqdm 进度条会随 \\r 刷新）。阻塞直到任务完成或连接断开。
        跟随命令随 SSH 连接断开而终止，但任务本体是 setsid 脱离会话的，不受影响。"""
        pid = (pid or "").strip()
        cond = f"[ ! -f {_shq(exit_file)} ]"
        if pid.isdigit():
            cond += f" && kill -0 {pid} 2>/dev/null"
        # tail -f 后台跟随；主循环等任务结束；再 sleep 让最后输出 flush，然后收掉 tail
        cmd = (f"tail -n {int(lines)} -f {_shq(log_file)} 2>/dev/null & TP=$!; "
               f"while {cond}; do sleep 2; done; sleep 3; kill $TP 2>/dev/null; true")
        return self.run(snapshot, cmd, instance_uuid, stream=stream, max_capture=1)

    def gpu_brief(self, snapshot, instance_uuid=None):
        cmd = ("nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
               "--format=csv,noheader,nounits 2>/dev/null || echo NA")
        so, _se, _c = self.run(snapshot, cmd, instance_uuid)
        return so.strip()

    # ---------------- rsync 文件同步（走密钥，密码不进命令行） ----------------
    def _rsync(self, snapshot, src, dst, key_path, excludes, instance_uuid=None):
        if not key_path:
            raise SSHUnavailable(SSHUnavailable.AUTH_FAILED,
                                 "rsync 需要密钥免密：请先 ensure_key_access")
        host = snapshot["proxy_host"]
        port = snapshot["ssh_port"]
        ssh_cmd = (f"ssh -p {port} -i {key_path} -o StrictHostKeyChecking=accept-new "
                   f"-o IdentitiesOnly=yes")
        # 仅用可移植选项：macOS 自带 rsync 2.6.9 不支持 --info=progress2 等新参数
        cmd = ["rsync", "-az", "--partial", "-e", ssh_cmd]
        for ex in excludes or []:
            cmd += ["--exclude", ex]
        cmd += [src, dst]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SSHUnavailable(SSHUnavailable.UNKNOWN,
                                 f"rsync 失败({proc.returncode}): {proc.stderr[:300]}")
        return proc.stdout

    def ensure_rsync(self, snapshot, instance_uuid=None):
        """远端缺 rsync 则尝试 apt 安装（AutoDL 基础镜像未必预装）。返回 True 表示可用。"""
        so, _se, _c = self.run(snapshot, "command -v rsync || true", instance_uuid)
        if so.strip():
            return True
        self.run(snapshot,
                 "apt-get update -qq && apt-get install -y -qq rsync >/dev/null 2>&1 || true",
                 instance_uuid)
        so2, _se2, _c2 = self.run(snapshot, "command -v rsync || true", instance_uuid)
        return bool(so2.strip())

    def push(self, snapshot, local_path, remote_subdir, key_path, excludes=None, instance_uuid=None):
        if not self.ensure_rsync(snapshot, instance_uuid):
            raise SSHUnavailable(SSHUnavailable.UNKNOWN, "远端无 rsync 且自动安装失败")
        host = snapshot["proxy_host"]
        remote = f"{self.cfg.user}@{host}:{self.cfg.remote_workdir}/{remote_subdir}".rstrip("/")
        # 确保远端目录存在
        self.run(snapshot, f"mkdir -p {_shq(self.cfg.remote_workdir + '/' + remote_subdir)}", instance_uuid)
        src = local_path.rstrip("/") + "/" if Path(local_path).is_dir() else local_path
        return self._rsync(snapshot, src, remote + "/", key_path,
                           excludes or [".git", "__pycache__", ".venv", "*.pyc"], instance_uuid)

    def pull(self, snapshot, remote_subpath, local_path, key_path, instance_uuid=None):
        if not self.ensure_rsync(snapshot, instance_uuid):
            raise SSHUnavailable(SSHUnavailable.UNKNOWN, "远端无 rsync 且自动安装失败")
        host = snapshot["proxy_host"]
        remote = f"{self.cfg.user}@{host}:{self.cfg.remote_workdir}/{remote_subpath}"
        Path(local_path).mkdir(parents=True, exist_ok=True)
        return self._rsync(snapshot, remote, local_path.rstrip("/") + "/", key_path, [], instance_uuid)

    def pull_artifacts(self, snapshot, patterns, local_dir, key_path, base=None, instance_uuid=None):
        """按 glob 拉回结果文件：从远端 base（默认数据盘）拉回匹配 patterns 的文件/目录到
        local_dir，保留相对结构。patterns 可为逗号分隔字符串或列表，每项是相对 base 的
        glob（如 'output/checkpoint-*/adapter_model.safetensors,metrics.json'）。
        返回 {files, local_dir, skipped_patterns}。需要密钥免密。"""
        if not key_path:
            raise SSHUnavailable(SSHUnavailable.AUTH_FAILED, "拉产物需要密钥免密：先 ensure_key_access")
        base = (base or self.cfg.remote_workdir).rstrip("/")
        raw = patterns.split(",") if isinstance(patterns, str) else list(patterns or [])
        safe, bad = [], []
        for p in (x.strip() for x in raw if x and x.strip()):
            (safe if _ARTIFACT_PAT_RE.match(p) else bad).append(p)
        if not safe:
            return {"files": [], "local_dir": local_dir, "skipped_patterns": bad}
        if not self.ensure_rsync(snapshot, instance_uuid):
            raise SSHUnavailable(SSHUnavailable.UNKNOWN, "远端无 rsync 且自动安装失败")
        # 远端展开 glob（patterns 已校验只含安全字符，故可不加引号让 shell 展开）
        expand = (f"cd {_shq(base)} 2>/dev/null || exit 0\n"
                  f"for p in {' '.join(safe)}; do ls -d $p 2>/dev/null; done")
        out, _e, _c = self.run(snapshot, expand, instance_uuid)
        files = sorted({l.strip().lstrip("./") for l in out.splitlines() if l.strip()})
        if not files:
            return {"files": [], "local_dir": local_dir, "skipped_patterns": bad}
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        host, port = snapshot["proxy_host"], snapshot["ssh_port"]
        ssh_cmd = (f"ssh -p {port} -i {key_path} -o StrictHostKeyChecking=accept-new "
                   f"-o IdentitiesOnly=yes")
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("\n".join(files) + "\n")
            listpath = tf.name
        try:
            # --files-from：从 base 拉列表里的相对路径，保留结构（-a 含 -r，目录会递归）
            cmd = ["rsync", "-az", f"--files-from={listpath}", "-e", ssh_cmd,
                   f"{self.cfg.user}@{host}:{base}/", local_dir.rstrip("/") + "/"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise SSHUnavailable(SSHUnavailable.UNKNOWN, f"拉产物 rsync 失败: {proc.stderr[:300]}")
        finally:
            os.unlink(listpath)
        return {"files": files, "local_dir": local_dir, "skipped_patterns": bad}


# ---------------- 小工具 ----------------
def _bg_inner(prelude, command, log_file, exit_file):
    """后台执行的内层脚本。command 必须包成**子 shell** ( … ) 再整体重定向：
    - 不包组：`a; b; c` 链只有最后一个子命令的输出进日志（且会覆盖任务自己的重定向）
    - 用 { } 组：命令里的 `exit N` 会杀掉外层 shell，exit_file 永远写不上
    子 shell 两个问题都没有：输出全量进日志，exit N 变成子 shell 退出码。"""
    return (f"{prelude}( {command}\n) > {_shq(log_file)} 2>&1; "
            f"echo $? > {_shq(exit_file)}")


class _TailBuf:
    """累积输出，可选只保留末尾约 limit 个字符（None=不限）。
    超过 2×limit 才裁剪一次（均摊 O(1)）；截断时 text() 前置一行标记。"""
    def __init__(self, limit=None):
        self.limit = limit
        self.parts = []
        self.n = 0
        self.truncated = False

    def add(self, chunk):
        self.parts.append(chunk)
        self.n += len(chunk)
        if self.limit and self.n > self.limit * 2:
            merged = "".join(self.parts)[-self.limit:]
            self.parts, self.n, self.truncated = [merged], len(merged), True

    def text(self):
        s = "".join(self.parts)
        if self.truncated:
            s = (f"[autodl] 输出过大已截断，仅保留末尾约 {self.limit} 字符；"
                 f"完整日志请改用 --background 后 autodl logs\n") + s
        return s


def _exec(client, command, stream=None, max_capture=None):
    """在已连接的 client 上执行命令，返回 (stdout, stderr, exit_code)。
    - stream=None 且无 max_capture：一次性读完（快路径，用于已知小输出的内部调用）。
    - stream：边跑边把输出块喂给回调实时回显；回显本身不受 max_capture 限制。
    - max_capture：累积到内存的 stdout/stderr 各自只保留末尾约这么多字符，防止
      巨量输出（训练日志几个 GB）把前台调用的本地内存撑爆。"""
    _in, out, err = client.exec_command(command)
    if stream is None and max_capture is None:
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        return so, se, out.channel.recv_exit_status()
    chan = out.channel
    so, se = _TailBuf(max_capture), _TailBuf(max_capture)
    while True:
        got = False
        while chan.recv_ready():
            chunk = chan.recv(4096).decode("utf-8", "replace")
            so.add(chunk)
            if stream:
                stream(chunk)
            got = True
        while chan.recv_stderr_ready():
            chunk = chan.recv_stderr(4096).decode("utf-8", "replace")
            se.add(chunk)
            if stream:
                stream(chunk)
            got = True
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if not got:
            time.sleep(0.1)
    return so.text(), se.text(), chan.recv_exit_status()


def _shq(s: str) -> str:
    """POSIX shell 单引号转义。"""
    return "'" + s.replace("'", "'\\''") + "'"


def _mkdirs(sftp, path):
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.mkdir(cur)
        except IOError:
            pass


def _replace_block(existing: str, block_text: str) -> str:
    if _BEGIN in existing and _END in existing:
        pre = existing.split(_BEGIN)[0].rstrip("\n")
        post = existing.split(_END, 1)[1].lstrip("\n")
        pieces = [p for p in (pre, block_text.rstrip("\n"), post) if p]
        return "\n".join(pieces) + "\n"
    sep = "" if (existing == "" or existing.endswith("\n")) else "\n"
    return existing + sep + block_text
