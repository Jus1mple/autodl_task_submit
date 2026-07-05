"""跑 tests/suites/ 下的四套桩测试。

套件是线性脚本风格（内部自带 check() 断言与非零退出码），且会改 os.environ / HOME /
cwd，所以用**子进程**隔离执行而不是 import——pytest 只负责汇总结果。
套件不触网、不花钱：SSH 桩用本地 bash 真执行生成的脚本（git/glob/hash 行为都是真实的）。
"""
import subprocess
import sys
from pathlib import Path

import pytest

SUITES = sorted((Path(__file__).parent / "suites").glob("suite_*.py"))


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.stem)
def test_suite(suite):
    r = subprocess.run([sys.executable, str(suite)], capture_output=True, text=True, timeout=600)
    tail = "\n".join(r.stdout.splitlines()[-25:])
    assert r.returncode == 0, f"{suite.name} 失败：\n{tail}\n{r.stderr[-1500:]}"
