"""配置：从 YAML 文件 + 环境变量(.env) 加载，去掉源码里的硬编码常量。

Token 永远只从环境变量/.env 读，绝不写进 YAML（避免泄漏/误提交）。加载优先级
（高 → 低），核心原则：**项目本地 .env 覆盖一切，包括已导出的全局环境变量**——
在哪个项目目录里跑，就用哪个项目的 token：
  1. 当前目录及其父目录里的 .env（项目本地，最高）
  2. autodl.yaml 所在目录的 .env（--config 指到别处时跟着配置走）
  3. 已导出的环境变量（export AUTODL_TOKEN=...）
  4. ~/.autodl/.env（全局兜底，只补缺，任意目录都能用）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

import yaml
from dotenv import find_dotenv, load_dotenv

from .errors import ConfigError

DEFAULT_CONFIG_NAMES = ("autodl.yaml", "autodl.yml")


@dataclass
class HTTPConfig:
    timeout_connect: float = 10.0
    timeout_read: float = 30.0
    retries: int = 3        # 仅对幂等读接口生效；写接口（create/power_*/release）不自动重试
    backoff: float = 1.5    # 指数退避基数（秒）


@dataclass
class GitConfig:
    """实例上的项目仓库（autodl clone / sync 用）。"""
    repo: str = ""       # 远端仓库 URL（https/ssh）；私有 https 库配合环境变量 AUTODL_GIT_TOKEN
    branch: str = "main"
    dir: str = ""        # 实例上的项目目录；留空 = <ssh.remote_workdir>/repo


@dataclass
class EnvConfig:
    """实例上的 Python 环境准备（autodl setup / run --setup 用）。"""
    setup: str = ""        # 自定义安装命令（最高优先），在仓库目录下执行
    auto: bool = True      # 未配 setup 时自动探测：setup.sh > environment.yml > requirements.txt > pyproject.toml
    pip_index: str = "https://pypi.tuna.tsinghua.edu.cn/simple"  # 国内 pip 镜像源；留空用官方
    academic_turbo: bool = False  # 安装前 source /etc/network_turbo（github/HF 加速，对 pypi 未必快）


@dataclass
class SSHConfig:
    user: str = "root"
    remote_workdir: str = "/root/autodl-tmp"  # 数据盘，放代码/数据/日志
    connect_timeout: float = 20.0
    connect_retries: int = 5
    retry_interval: float = 5.0
    identity_file: str = ""     # 留空则自动探测 ~/.ssh/id_ed25519 等；没有则生成
    config_alias: str = "autodl"  # 写入 ~/.ssh/config 的 Host 别名


@dataclass
class Config:
    base_url: str = "https://api.autodl.com"
    image_uuid: str = "base-image-12be412037"
    gpu_spec_uuid: str = "v-48g"
    cuda_v_from: int = 111
    instance_name: str = "task-runner"
    expand_disk_gb: int = 10
    req_gpu_amount: int = 1
    data_center_list: list = field(default_factory=list)  # 空=让 AutoDL 自动调度
    regions: list = field(default_factory=lambda: ["westDC2", "westDC3", "beijingDC1"])
    stock_region: str = "westDC2"
    gpu_stock_name: str = "vGPU-48GB"  # 库存匹配用的 GPU 型号名（对应 gpu_spec_uuid=v-48g）
    min_balance_yuan: float = 10.0
    registry_path: str = ".autodl/registry.db"
    http: HTTPConfig = field(default_factory=HTTPConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    git: GitConfig = field(default_factory=GitConfig)
    env: EnvConfig = field(default_factory=EnvConfig)

    # 运行时填充（不来自 YAML）
    token: str = field(default="", repr=False)
    _base_dir: Path = field(default_factory=Path.cwd)

    @property
    def registry_abspath(self) -> Path:
        p = Path(self.registry_path)
        return p if p.is_absolute() else (self._base_dir / p)


def _merge_into(dc, data: dict):
    """把 dict 递归合并进 dataclass 实例（只认已声明的字段）。"""
    if not isinstance(data, dict):
        raise ConfigError(f"配置片段应为映射，实际为 {type(data).__name__}")
    valid = {f.name: f for f in fields(dc)}
    for key, val in data.items():
        if key not in valid:
            continue  # 忽略未知键，保持向前兼容
        cur = getattr(dc, key)
        if is_dataclass(cur) and isinstance(val, dict):
            _merge_into(cur, val)
        else:
            setattr(dc, key, val)


def find_config_file(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise ConfigError(f"指定的配置文件不存在: {p}")
        return p
    # 从 cwd 向上找
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        for name in DEFAULT_CONFIG_NAMES:
            cand = parent / name
            if cand.exists():
                return cand
    return None


def _load_env_files(config_path: Path | None):
    """按优先级加载 .env：项目本地覆盖全局环境变量（override=True），
    ~/.autodl/.env 只补缺。后加载且 override 的赢，所以按低→高的顺序加载。
    注意必须 usecwd=True：默认的 find_dotenv 从本模块（site-packages）位置向上找，
    pip 安装后会找不到项目里的 .env。"""
    load_dotenv(Path.home() / ".autodl" / ".env")  # 全局兜底：只补缺，不盖环境变量
    if config_path is not None:
        load_dotenv(config_path.parent / ".env", override=True)  # 配置文件旁
    found = find_dotenv(usecwd=True)  # cwd 及其父目录：项目本地，最高优先
    if found:
        load_dotenv(found, override=True)


def load_config(explicit_path: str | None = None) -> Config:
    cfg = Config()
    path = find_config_file(explicit_path)
    if path is not None:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _merge_into(cfg, data)
        cfg._base_dir = path.parent
    _load_env_files(path)
    cfg.token = os.getenv("AUTODL_TOKEN", "")
    return cfg


def require_token(cfg: Config) -> str:
    if not cfg.token:
        raise ConfigError(
            "未找到 AUTODL_TOKEN。获取：AutoDL 控制台 → 账号 → 设置 → 开发者 Token。"
            "配置任选其一：项目目录 .env 写 AUTODL_TOKEN=...（推荐）；"
            "或 export AUTODL_TOKEN=...；或写进 ~/.autodl/.env（全局）")
    return cfg.token
