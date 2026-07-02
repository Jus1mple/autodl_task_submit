"""旧版一体脚本（保留兼容入口）—— 现在是 autodl 包之上的薄封装。

用法不变：uv run python submit.py
流程：余额护栏 -> 复用/新建实例（记录在 .autodl/registry.db，跨工具共享）
      -> 跑下方 MY_TASK 冒烟脚本（实时回显、记入台账） -> 关机（保留实例以便复用）。

实例规格/区域/预算等配置改 autodl.yaml（参考 autodl.example.yaml），Token 放 .env。
日常使用请直接用 CLI：本脚本等价于
    uv run python -m autodl run --script <脚本> --down
"""
from autodl import tasks
from autodl.config import load_config, require_token
from autodl.core import Context

# True=任务后彻底释放（连磁盘费一起停，环境会随实例丢失）；False=只关机，保留磁盘环境复用
RELEASE_AFTER = False

# 在实例上执行的 bash 任务：先做一次链路 + 环境冒烟测试，替换成你的真实任务即可
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


def main():
    cfg = load_config()
    require_token(cfg)
    ctx = Context(cfg)

    print(f"账户余额: ¥{ctx.api.balance_yuan():.2f}")
    existing = ctx.api.list_instances()
    print(f"当前已有实例: {len(existing)} 个")
    for it in existing:
        uid = it.get("instance_uuid") or it.get("uuid") or "?"
        print(f"  - {uid}  status={it.get('status')}  region={it.get('region_sign')}")

    res = tasks.submit(
        ctx, mode="script_text", value=MY_TASK, name="smoke",
        teardown="release" if RELEASE_AFTER else "power_off",
        stream=lambda chunk: print(chunk, end="", flush=True),
    )
    print(f"退出码: {res['exit_code']}")
    return 0 if res["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
