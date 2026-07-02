"""autodl —— AutoDL GPU 云的 Python 库 + 命令行工具。

用法对标 huggingface-hub：pip 装完即有 `autodl` 命令，跑任务、拿数据（--json）；
也可以当库 import，所有 CLI 能力都有对应的 Python API。

命令行::

    pip install autodl-task-submit
    echo 'AUTODL_TOKEN=...' > .env
    autodl run --remote-script ~/proj/run.sh --down --json   # stdout 只有 JSON

Python::

    from autodl import connect, submit

    ctx = connect()                       # 读 autodl.yaml + .env 的 AUTODL_TOKEN
    res = submit(ctx, mode="remote_script", value="~/proj/run.sh",
                 teardown="power_off")    # 起/复用实例 -> 跑 -> 抓指标 -> 关机
    print(res["exit_code"], ctx.reg.get_metrics(res["run_id"]))

可视化大盘不在核心包里，在 `web-dashboard` 分支单独维护。
"""

from .config import Config, load_config, require_token
from .core import Context
from .errors import (APIError, AutoDLError, ConfigError, InsufficientBalance,
                     RateLimited, SSHUnavailable)
from .tasks import submit

__version__ = "1.7.0"

__all__ = [
    "connect", "submit", "Context", "Config", "load_config", "require_token",
    "AutoDLError", "APIError", "ConfigError", "InsufficientBalance",
    "RateLimited", "SSHUnavailable", "__version__",
]


def connect(config_path=None):
    """一步拿到可用的客户端 Context：加载配置（autodl.yaml + .env）并校验 token。"""
    cfg = load_config(config_path)
    require_token(cfg)
    return Context(cfg)
