"""极简后台任务表：把"创建/开机实例"等耗时操作放到线程里跑，前端轮询状态。"""
from __future__ import annotations

import threading


class JobStore:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()
        self._n = 0

    def submit(self, fn):
        """fn 接收一个 log(msg) 回调；返回值存入 result。立即返回 job_id。"""
        with self._lock:
            self._n += 1
            jid = f"job-{self._n}"
            self._jobs[jid] = {"id": jid, "status": "running", "result": None, "error": None, "log": []}

        def runner():
            try:
                res = fn(self._logger(jid))
                self._update(jid, status="done", result=res)
            except Exception as e:  # noqa: BLE001 - 把任何失败回报给前端
                self._update(jid, status="failed", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=runner, daemon=True).start()
        return jid

    def _logger(self, jid):
        def log(msg):
            with self._lock:
                if jid in self._jobs:
                    self._jobs[jid]["log"].append(str(msg))
        return log

    def _update(self, jid, **kw):
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid].update(kw)

    def get(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            return dict(j) if j else None
