import requests
import paramiko
import time
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 配置
# ============================================================
TOKEN = os.getenv("AUTODL_TOKEN")
if not TOKEN:
    raise SystemExit("未找到 AUTODL_TOKEN，请在 .env 文件中设置 AUTODL_TOKEN=...")

BASE_URL = "https://api.autodl.com"
HEADERS = {"Authorization": TOKEN, "Content-Type": "application/json"}

# --- 实例配置 ---
IMAGE_UUID = "base-image-12be412037"   # 也可换成你的私有镜像，如 "image-67b066536a"
GPU_SPEC_UUID = "v-48g"
CUDA_V_FROM = 111                      # 最低 CUDA 版本要求（111 = 11.1）
INSTANCE_NAME = "task-runner"
EXPAND_DISK_GB = 10
# 想固定区域就填，如 ["westDC2"]；填 None 让 AutoDL 自动调度（默认，最稳）
DATA_CENTER_LIST = None
# 仅用于"开机前看库存"的参考区域（westDC2 / westDC3 / beijingDC1 ...）
STOCK_REGION = "westDC2"

# --- 护栏 / 复用 ---
MIN_BALANCE_YUAN = 10.0                # 余额低于此值（元）则中止
RELEASE_AFTER = False                  # True=任务后彻底释放；False=只关机，保留磁盘环境以便复用
# 记录实例 uuid 的本地文件，下次运行自动复用
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".instance_uuid")


# ============================================================
# 底层请求封装
#   AutoDL 约定：GET 接口参数走 query string（body 经网关会被丢弃），
#   POST 接口参数走 JSON body。响应统一 {code, data, msg, request_id}。
# ============================================================
def _check(path, resp):
    if resp.get("code") != "Success":
        raise Exception(f"请求 {path} 失败: {resp}")
    return resp.get("data")


def _get(path, **params):
    resp = requests.get(f"{BASE_URL}{path}", params=params, headers=HEADERS).json()
    return _check(path, resp)


def _post(path, body=None, check=True):
    resp = requests.post(f"{BASE_URL}{path}", json=body or {}, headers=HEADERS).json()
    return _check(path, resp) if check else resp


# ============================================================
# 账户 / 资源查询
# ============================================================
def get_balance_yuan():
    """账户余额（元）。"""
    return _post("/api/v1/dev/wallet/balance")["assets"] / 1000.0


def list_instances():
    """当前全部实例（列表）。"""
    data = _post("/api/v1/dev/instance/pro/list", {"page_index": 1, "page_size": 100})
    return data.get("list", [])


def gpu_stock(region_sign, cuda_v_from=None, cuda_v_to=None):
    """查询某区域各 GPU 型号的空闲/总数。返回 [{型号: {idle_gpu_num, total_gpu_num}}, ...]"""
    body = {"region_sign": region_sign}
    if cuda_v_from is not None:
        body["cuda_v_from"] = cuda_v_from
    if cuda_v_to is not None:
        body["cuda_v_to"] = cuda_v_to
    return _post("/api/v1/dev/machine/region/gpu_stock", body)


# ============================================================
# 实例生命周期
# ============================================================
def create_instance():
    body = {
        "req_gpu_amount": 1,
        "expand_system_disk_by_gb": EXPAND_DISK_GB,
        "gpu_spec_uuid": GPU_SPEC_UUID,
        "image_uuid": IMAGE_UUID,
        "cuda_v_from": CUDA_V_FROM,
        "instance_name": INSTANCE_NAME,
        "start_command": "sleep infinity",
    }
    if DATA_CENTER_LIST:
        body["data_center_list"] = DATA_CENTER_LIST
    instance_uuid = _post("/api/v1/dev/instance/pro/create", body)
    print(f"实例已创建: {instance_uuid}")
    return instance_uuid


def get_status(instance_uuid):
    """查询状态；实例已不存在时返回 None。"""
    try:
        return _get("/api/v1/dev/instance/pro/status", instance_uuid=instance_uuid)
    except Exception:
        return None


def get_snapshot(instance_uuid):
    return _get("/api/v1/dev/instance/pro/snapshot", instance_uuid=instance_uuid)


def power_on(instance_uuid):
    # payload="gpu" 表示带卡开机（文档约定）
    _post("/api/v1/dev/instance/pro/power_on",
          {"instance_uuid": instance_uuid, "payload": "gpu"})
    print(f"开机中: {instance_uuid}")


def power_off(instance_uuid):
    resp = _post("/api/v1/dev/instance/pro/power_off",
                 {"instance_uuid": instance_uuid}, check=False)
    print(f"  关机: {resp.get('code')} {resp.get('msg')}")


def release(instance_uuid):
    resp = _post("/api/v1/dev/instance/pro/release",
                 {"instance_uuid": instance_uuid}, check=False)
    print(f"  释放: {resp.get('code')} {resp.get('msg')}")


def wait_running(instance_uuid, max_tries=60, interval=5):
    """轮询直到 running，返回 SSH 快照信息。"""
    for _ in range(max_tries):
        status = _get("/api/v1/dev/instance/pro/status", instance_uuid=instance_uuid)
        print(f"  状态: {status}")
        if status == "running":
            return get_snapshot(instance_uuid)
        if status in ("removing", "removed"):
            raise Exception(f"实例进入异常状态: {status}")
        time.sleep(interval)
    raise Exception("等待 running 超时")


# --- 复用：本地记录的实例 uuid ---
def _load_saved_uuid():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return f.read().strip() or None
    return None


def _save_uuid(instance_uuid):
    with open(STATE_FILE, "w") as f:
        f.write(instance_uuid)


def _clear_uuid():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def ensure_instance():
    """复用本地记录的实例（已关机则开机），不可用则新建。返回 (instance_uuid, snapshot)。"""
    instance_uuid = _load_saved_uuid()
    if instance_uuid:
        status = get_status(instance_uuid)
        print(f"复用已记录实例 {instance_uuid}，当前状态: {status}")

        # 关机中是个短暂的过渡态，等它彻底进入 shutdown 再决定开机
        if status == "shutting_down":
            for _ in range(36):
                time.sleep(5)
                status = get_status(instance_uuid)
                print(f"  等待关机完成: {status}")
                if status in ("shutdown", "power_off"):
                    break

        if status == "running":
            return instance_uuid, get_snapshot(instance_uuid)
        if status in ("shutdown", "power_off"):
            power_on(instance_uuid)
            return instance_uuid, wait_running(instance_uuid)
        if status in ("starting", "sys_volume_creating"):
            return instance_uuid, wait_running(instance_uuid)
        # removed / None / 其它 → 记录失效，新建
        print("  记录的实例已不可用，将新建。")
        _clear_uuid()

    instance_uuid = create_instance()
    _save_uuid(instance_uuid)
    return instance_uuid, wait_running(instance_uuid)


# ============================================================
# 通过 SSH 执行任务
# ============================================================
def run_task_via_ssh(snapshot, task_script: str) -> str:
    """
    通过 SSH 在实例上执行任务，返回标准输出结果。

    task_script: 你想在服务器上执行的 bash 脚本（字符串形式）。
                 如需用 python，可在脚本里自行激活 conda 后调用。
    """
    host = snapshot["proxy_host"]
    port = snapshot["ssh_port"]
    password = snapshot["root_password"]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # 刚 running 时 SSH 可能还没就绪，重试几次
    last_err = None
    for attempt in range(5):
        try:
            client.connect(hostname=host, port=port, username="root",
                           password=password, timeout=20)
            break
        except Exception as e:
            last_err = e
            print(f"  SSH 重试 ({attempt + 1}/5): {e}")
            time.sleep(5)
    else:
        raise Exception(f"SSH 连接失败: {last_err}")
    print(f"SSH 已连接: {host}:{port}")

    # 上传 bash 任务脚本
    sftp = client.open_sftp()
    with sftp.open("/root/task.sh", "w") as f:
        f.write(task_script)
    sftp.close()

    # 用 bash 执行，捕获输出
    _, stdout, stderr = client.exec_command("bash /root/task.sh")
    output = stdout.read().decode("utf-8")
    error = stderr.read().decode("utf-8")
    exit_code = stdout.channel.recv_exit_status()

    client.close()

    if error:
        print(f"[stderr]\n{error}")
    if exit_code != 0:
        print(f"[远程退出码] {exit_code}")
    return output


# ============================================================
# 使用示例：先用 bash 脚本做一次链路 + 环境冒烟测试
# ============================================================
MY_TASK = r"""#!/bin/bash
set -u
echo "===== 主机信息 ====="
hostname
uname -a
echo

echo "===== GPU (nvidia-smi) ====="
nvidia-smi || echo "nvidia-smi 不可用"
echo

echo "===== Python / conda ====="
for c in /root/miniconda3 /opt/conda /root/anaconda3 /root/miniconda; do
  if [ -f "$c/etc/profile.d/conda.sh" ]; then
    . "$c/etc/profile.d/conda.sh"
    conda activate base 2>/dev/null || true
    break
  fi
done
if command -v python >/dev/null; then python --version; else echo "python 不可用"; fi
python -c "import torch; print('torch', torch.__version__, 'cuda_available', torch.cuda.is_available())" 2>/dev/null \
  || echo "torch 未安装（仅验证流程时可忽略）"
echo

echo "===== 完成 ====="
"""


if __name__ == "__main__":
    # 1) 余额护栏
    bal = get_balance_yuan()
    print(f"账户余额: ¥{bal:.2f}")
    if bal < MIN_BALANCE_YUAN:
        raise SystemExit(f"余额低于 ¥{MIN_BALANCE_YUAN:.2f}，已中止。")

    # 2) 启动前清查现有实例（避免残留计费）
    existing = list_instances()
    print(f"当前已有实例: {len(existing)} 个")
    for it in existing:
        uid = it.get("instance_uuid") or it.get("uuid") or "?"
        print(f"  - {uid}  status={it.get('status')}  region={it.get('region_sign')}")

    # 3) 开机前看库存（参考信息，不阻断）
    try:
        stock = gpu_stock(STOCK_REGION, cuda_v_from=CUDA_V_FROM)
        line = ", ".join(
            f"{name}:{st.get('idle_gpu_num')}/{st.get('total_gpu_num')}"
            for d in (stock or []) for name, st in d.items()
        ) or "无数据"
        print(f"[{STOCK_REGION}] GPU库存(空闲/总): {line}")
    except Exception as e:
        print(f"库存查询跳过: {e}")

    # 4) 复用或新建实例
    instance_uuid, snapshot = ensure_instance()

    try:
        result = run_task_via_ssh(snapshot, MY_TASK)
        print("\n======== 任务输出 ========")
        print(result)
        print("==========================")
    finally:
        if RELEASE_AFTER:
            power_off(instance_uuid)
            time.sleep(15)
            release(instance_uuid)
            _clear_uuid()
            print(f"实例 {instance_uuid} 已关机并释放")
        else:
            power_off(instance_uuid)
            print(f"实例 {instance_uuid} 已关机（保留以便下次复用；彻底释放请设 RELEASE_AFTER=True）")
