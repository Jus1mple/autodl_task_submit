"""带锁的 SQLite 实例/任务台账 —— 多实例与多守护进程的"真相源"。

替代原来单值明文的 .instance_uuid：
- instances 表：记录每台实例的 uuid/名称/区域/状态缓存/标签/是否受保护/开关机余额打点。
- runs 表：记录每个后台任务的 run_id/实例/脚本/状态/日志/pid/退出码/起止时间。
并发安全：跨进程临界区用 fcntl.flock 文件锁；SQLite 自身设 busy_timeout 兜底。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl  # POSIX（macOS/Linux 可用）
except ImportError:  # pragma: no cover - Windows 回退（无跨进程锁）
    fcntl = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_uuid TEXT PRIMARY KEY,
    name          TEXT,
    region        TEXT,
    gpu_spec      TEXT,
    status_cached TEXT,
    protected     INTEGER DEFAULT 0,
    tags          TEXT DEFAULT '',
    last_balance  REAL,
    created_at    REAL,
    updated_at    REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    instance_uuid TEXT,
    config_json   TEXT,
    status        TEXT,
    log_path      TEXT,
    exit_file     TEXT,
    pid           TEXT,
    exit_code     INTEGER,
    started_at    REAL,
    ended_at      REAL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Registry:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lockfile = str(self.path) + ".lock"
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def lock(self):
        """跨进程临界区锁（如开机/创建/治理动作前后），避免多守护进程互相打架。"""
        if fcntl is None:
            yield
            return
        fd = os.open(self._lockfile, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---------------- instances ----------------
    def upsert_instance(self, instance_uuid, **fields):
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT instance_uuid FROM instances WHERE instance_uuid=?",
                            (instance_uuid,)).fetchone()
            if row is None:
                c.execute("INSERT INTO instances(instance_uuid, created_at, updated_at) VALUES(?,?,?)",
                          (instance_uuid, now, now))
            cols, vals = [], []
            for k, v in fields.items():
                cols.append(f"{k}=?")
                vals.append(v)
            cols.append("updated_at=?")
            vals.append(now)
            vals.append(instance_uuid)
            c.execute(f"UPDATE instances SET {', '.join(cols)} WHERE instance_uuid=?", vals)

    def get_instance(self, instance_uuid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM instances WHERE instance_uuid=?", (instance_uuid,)).fetchone()
            return dict(r) if r else None

    def list_instances(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM instances ORDER BY created_at")]

    def remove_instance(self, instance_uuid):
        with self._conn() as c:
            c.execute("DELETE FROM instances WHERE instance_uuid=?", (instance_uuid,))

    def set_protected(self, instance_uuid, protected: bool):
        self.upsert_instance(instance_uuid, protected=1 if protected else 0)

    # ---------------- 当前活动实例（替代 .instance_uuid） ----------------
    def get_active(self):
        with self._conn() as c:
            r = c.execute("SELECT v FROM meta WHERE k='active_instance'").fetchone()
            return r["v"] if r else None

    def set_active(self, instance_uuid):
        with self._conn() as c:
            if instance_uuid is None:
                c.execute("DELETE FROM meta WHERE k='active_instance'")
            else:
                c.execute("INSERT INTO meta(k,v) VALUES('active_instance',?) "
                          "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (instance_uuid,))

    # ---------------- runs ----------------
    def record_run(self, run_id, instance_uuid, config, log_path, exit_file, pid):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs(run_id, instance_uuid, config_json, status, "
                "log_path, exit_file, pid, started_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, instance_uuid, json.dumps(config, ensure_ascii=False),
                 "running", log_path, exit_file, pid, time.time()),
            )

    def finish_run(self, run_id, exit_code):
        with self._conn() as c:
            c.execute("UPDATE runs SET status=?, exit_code=?, ended_at=? WHERE run_id=?",
                      ("succeeded" if exit_code == 0 else "failed", exit_code, time.time(), run_id))

    def list_runs(self, instance_uuid=None):
        with self._conn() as c:
            if instance_uuid:
                rows = c.execute("SELECT * FROM runs WHERE instance_uuid=? ORDER BY started_at",
                                 (instance_uuid,))
            else:
                rows = c.execute("SELECT * FROM runs ORDER BY started_at")
            return [dict(r) for r in rows]

    def get_run(self, run_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None
