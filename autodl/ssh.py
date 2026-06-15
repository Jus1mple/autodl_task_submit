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
import re
import socket
import subprocess
import time
from pathlib import Path

import paramiko

from .errors import SSHUnavailable

_RUN_ID_RE = re.compile(r"[A-Za-z0-9_.\-]+")


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
    def run(self, snapshot, command, instance_uuid=None):
        """同步执行一条命令，返回 (stdout, stderr, exit_code)。"""
        client = self.connect(snapshot, instance_uuid)
        try:
            _in, out, err = client.exec_command(command)
            so = out.read().decode("utf-8", "replace")
            se = err.read().decode("utf-8", "replace")
            code = out.channel.recv_exit_status()
            return so, se, code
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
            sftp.close()
        finally:
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

    # ---------------- 后台非阻塞执行 ----------------
    def run_script(self, snapshot, script_text, run_id, instance_uuid=None):
        """上传一段脚本到数据盘并**同步**执行，返回 (stdout, stderr, exit_code)。"""
        run_id = _safe_run_id(run_id)
        wd = self.cfg.remote_workdir
        task = f"{wd}/task_{run_id}.sh"
        client = self.connect(snapshot, instance_uuid)
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, wd)
            with sftp.open(task, "w") as f:
                f.write(script_text)
            sftp.close()
            _in, out, err = client.exec_command(f"bash {task}")
            so = out.read().decode("utf-8", "replace")
            se = err.read().decode("utf-8", "replace")
            code = out.channel.recv_exit_status()
            return so, se, code
        finally:
            client.close()

    def run_background_command(self, snapshot, command, run_id, instance_uuid=None):
        """把任意命令拉起为脱离会话的后台进程，立即返回 {pid, log, exit_file, workdir}。"""
        run_id = _safe_run_id(run_id)
        wd = self.cfg.remote_workdir
        logs = f"{wd}/logs"
        log_file = f"{logs}/{run_id}.log"
        exit_file = f"{logs}/{run_id}.exit"
        pid_file = f"{logs}/{run_id}.pid"

        client = self.connect(snapshot, instance_uuid)
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, logs)
            sftp.close()
            # setsid 完全脱离会话；exit code 落 exit_file；pid 落 pid_file。路径一律转义。
            inner = f"{command} > {_shq(log_file)} 2>&1; echo $? > {_shq(exit_file)}"
            launch = (
                f"setsid bash -c {_shq(inner)} "
                f"</dev/null >/dev/null 2>&1 & echo $! > {_shq(pid_file)}; cat {_shq(pid_file)}"
            )
            _in, out, err = client.exec_command(launch)
            pid = out.read().decode().strip()
            out.channel.recv_exit_status()
        finally:
            client.close()
        return {"pid": pid, "log": log_file, "exit_file": exit_file, "workdir": wd}

    def run_background(self, snapshot, script_text, run_id, instance_uuid=None):
        """上传一段脚本到数据盘并后台执行。返回 {pid, log, exit_file, workdir}。"""
        wd = self.cfg.remote_workdir
        task_file = f"{wd}/task_{run_id}.sh"
        client = self.connect(snapshot, instance_uuid)
        try:
            sftp = client.open_sftp()
            _mkdirs(sftp, wd)
            with sftp.open(task_file, "w") as f:
                f.write(script_text)
            sftp.close()
        finally:
            client.close()
        return self.run_background_command(snapshot, f"cd {_shq(wd)} && bash {_shq(task_file)}",
                                           run_id, instance_uuid)

    def poll(self, snapshot, run_meta, instance_uuid=None):
        """返回 ('running'|'done', exit_code|None)。"""
        pid = run_meta.get("pid")
        exit_file = run_meta["exit_file"]
        cmd = (
            f"if [ -f {_shq(exit_file)} ]; then echo DONE $(cat {_shq(exit_file)}); "
            f"elif kill -0 {pid} 2>/dev/null; then echo RUNNING; "
            f"else echo DONE unknown; fi"
        )
        so, _se, _c = self.run(snapshot, cmd, instance_uuid)
        parts = so.split()
        if parts and parts[0] == "RUNNING":
            return "running", None
        code = None
        if len(parts) >= 2 and parts[1].isdigit():
            code = int(parts[1])
        return "done", code

    def tail(self, snapshot, log_file, lines=50, instance_uuid=None):
        so, _se, _c = self.run(snapshot, f"tail -n {int(lines)} {_shq(log_file)} 2>/dev/null", instance_uuid)
        return so

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


# ---------------- 小工具 ----------------
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
