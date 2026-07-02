"""统一异常类型，便于上层按类别分流处理。"""
from __future__ import annotations


class AutoDLError(Exception):
    """本工具所有异常的基类。"""


class ConfigError(AutoDLError):
    """配置缺失或非法。"""


class InsufficientBalance(AutoDLError):
    """余额低于安全阈值，拒绝开机/提交任务。"""


class APIError(AutoDLError):
    """AutoDL API 返回 code != Success，或 HTTP 层错误。"""

    def __init__(self, message, *, code=None, msg=None, request_id=None, status_code=None):
        super().__init__(message)
        self.code = code            # AutoDL 业务错误码，如 RequestParameterIsWrong
        self.msg = msg              # AutoDL 返回的中文提示
        self.request_id = request_id
        self.status_code = status_code  # HTTP 状态码


class RateLimited(APIError):
    """被限流（HTTP 429）。"""


# --- SSH 连接失败分类 ---
class SSHUnavailable(AutoDLError):
    """SSH 不可达。reason 用于区分根因，避免把"网络不通"误判成"实例已关机"。"""

    INSTANCE_NOT_RUNNING = "instance_not_running"  # 实例不在 running（已关机/创建中/已释放）
    PROXY_UNREACHABLE = "proxy_unreachable"        # 跳板/网络不可达
    PORT_NOT_READY = "port_not_ready"              # 端口尚未就绪（刚 running，sshd 未起好）
    AUTH_FAILED = "auth_failed"                    # 认证失败（密码错误/轮换）
    TIMEOUT = "timeout"                            # 连接超时
    UNKNOWN = "unknown"

    def __init__(self, reason, detail="", *, status=None):
        self.reason = reason
        self.status = status  # 失败时查到的实例状态（若有）
        super().__init__(f"SSH 不可达 [{reason}] status={status} {detail}".strip())
