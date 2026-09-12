"""个人费用账本与预算 —— 共享账号下只算"我自己"的那份。

为什么不用余额差分：账号被很多人共用，余额的变化混着所有人的实例/存储/弹性部署，
差分算不出"我花了多少"。AutoDL 也没有账单/消费明细 API。

口径（与官方计费公式一致：按秒计，时长 = 关机时间 − 开机时间）：
- 一条 **session** = 我让某台实例处于 running 的一段时间，费用 = 小时价 × 秒数 / 3600。
  小时价来自 snapshot 的 `payg_price`（折后按量价，单位 元×1000/小时，与钱包 assets 同一族）。
- 关机未释放的实例，数据盘按 ¥0.01/GB/天 继续计费（官方口径，次日结算），单独记为磁盘费。
- run 的费用 = 覆盖它的 session 单价 × run 时长；session 里 run 之间的空档就是"闲置开销"。

"我的"边界：`owner` 配置。新建实例名带 `<owner>/` 前缀；对账时按前缀认领，即使换电脑、
台账丢了也能重建。止损/守护默认只碰我的实例，绝不动别人的。

账本是**人级**的（默认 ~/.autodl/ledger.db，跨项目共享），预算按人算才有意义；
项目级 registry（runs/metrics）保持不变。

限制体系四层，各管一件事：
1. 单次 run 时限：`run --max-hours` → 实例侧 `timeout` 包住命令，本地进程死了也生效。
2. 会话保险丝：`budget.max_session_hours` → 开机时在实例里挂 `sleep N; shutdown`
   （AutoDL 文档：实例内执行 shutdown 即关机），笔记本合盖/断网也能自关。
3. 额度：`budget.daily/weekly/monthly_yuan` → 开机/创建/提交前预检 + budget-guard 守护。
4. 资源：`max_concurrent` / `max_price_per_hour` → 创建/开机前拦。

精度边界：这是"归属估算"不是发票——单价按开机时快照、代金券看不到、别人用我的实例
也会算到我头上（所以要贴 owner 前缀）。月底拿控制台账单按我的实例 uuid 筛一次对账。
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .errors import APIError, AutoDLError, BudgetExceeded, SSHUnavailable
from .ssh import _shq

PRICE_UNIT = 1000.0               # payg_price → 元/小时
DISK_YUAN_PER_GB_DAY = 0.01       # 官方：付费数据盘 ¥0.01/GB/日，开关机都算
FUSE_PID = "/root/.autodl_fuse.pid"
_BOOTING = ("creating", "starting", "sys_volume_creating")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_uuid TEXT NOT NULL,
    owner         TEXT,
    project       TEXT,
    gpu_spec      TEXT,
    req_gpu       INTEGER,
    region        TEXT,
    price_milli   INTEGER,      -- 元*1000 / 小时（snapshot.payg_price）
    started_at    REAL NOT NULL,
    ended_at      REAL,         -- NULL = 仍在 running
    source        TEXT,         -- create | power_on | adopt(对账发现) | batch
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_inst ON sessions(instance_uuid);
CREATE TABLE IF NOT EXISTS owned (
    instance_uuid TEXT PRIMARY KEY,
    owner         TEXT,
    name          TEXT,
    disk_gb       REAL,         -- 付费数据盘 GB（磁盘费估算用）
    created_at    REAL,
    released_at   REAL,         -- NULL = 仍存在
    last_seen     REAL
);
"""


class Ledger:
    """个人账本（SQLite，WAL）。所有金额计算都在 Python 里做，SQL 只存事实。"""

    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    # ---------------- owned ----------------
    def adopt(self, instance_uuid, owner="", name="", disk_gb=None, created_at=None):
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT instance_uuid FROM owned WHERE instance_uuid=?",
                            (instance_uuid,)).fetchone()
            if row is None:
                c.execute("INSERT INTO owned(instance_uuid, owner, name, disk_gb, created_at, last_seen) "
                          "VALUES(?,?,?,?,?,?)",
                          (instance_uuid, owner, name, disk_gb, created_at or now, now))
            else:
                c.execute("UPDATE owned SET owner=COALESCE(NULLIF(?,''), owner), "
                          "name=COALESCE(NULLIF(?,''), name), disk_gb=COALESCE(?, disk_gb), "
                          "created_at=COALESCE(?, created_at), released_at=NULL, last_seen=? "
                          "WHERE instance_uuid=?",
                          (owner, name, disk_gb, created_at, now, instance_uuid))

    def mark_released(self, instance_uuid, at=None):
        with self._conn() as c:
            c.execute("UPDATE owned SET released_at=COALESCE(released_at, ?) WHERE instance_uuid=?",
                      (at or time.time(), instance_uuid))

    def owned(self, include_released=False):
        with self._conn() as c:
            q = "SELECT * FROM owned" + ("" if include_released else " WHERE released_at IS NULL")
            return [dict(r) for r in c.execute(q + " ORDER BY created_at")]

    def is_owned(self, instance_uuid):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM owned WHERE instance_uuid=? AND released_at IS NULL",
                             (instance_uuid,)).fetchone() is not None

    # ---------------- sessions ----------------
    def open_session(self, instance_uuid, price_milli, *, owner="", project="", gpu_spec="",
                     req_gpu=None, region="", source="", started_at=None, note=""):
        """开一条 session；该实例已有未关闭的 session 则不重复开（返回已有 id）。"""
        with self._conn() as c:
            row = c.execute("SELECT id FROM sessions WHERE instance_uuid=? AND ended_at IS NULL",
                            (instance_uuid,)).fetchone()
            if row:
                if price_milli:  # 之前没拿到价格、现在拿到了 → 补上
                    c.execute("UPDATE sessions SET price_milli=COALESCE(NULLIF(price_milli,0), ?) WHERE id=?",
                              (int(price_milli), row["id"]))
                return row["id"]
            cur = c.execute(
                "INSERT INTO sessions(instance_uuid, owner, project, gpu_spec, req_gpu, region, "
                "price_milli, started_at, source, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (instance_uuid, owner, project, gpu_spec, req_gpu, region,
                 int(price_milli or 0), float(started_at or time.time()), source, note))
            return cur.lastrowid

    def close_session(self, instance_uuid, ended_at=None):
        """关闭该实例的未结 session，返回该 session（dict，含 cost_yuan）；没有则 None。"""
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE instance_uuid=? AND ended_at IS NULL "
                            "ORDER BY started_at DESC LIMIT 1", (instance_uuid,)).fetchone()
            if not row:
                return None
            end = float(ended_at or time.time())
            if end < row["started_at"]:
                end = row["started_at"]
            c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (end, row["id"]))
            s = dict(row)
            s["ended_at"] = end
            s["cost_yuan"] = session_cost(s)
            return s

    def open_sessions(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sessions WHERE ended_at IS NULL")]

    def sessions(self, since=None, until=None, instance_uuid=None):
        """与 [since, until] 有交集的 session（until=None → 现在）。"""
        q, args = "SELECT * FROM sessions WHERE 1=1", []
        if since is not None:
            q += " AND (ended_at IS NULL OR ended_at >= ?)"
            args.append(float(since))
        if until is not None:
            q += " AND started_at <= ?"
            args.append(float(until))
        if instance_uuid:
            q += " AND instance_uuid=?"
            args.append(instance_uuid)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY started_at", args)]


# ---------------- 金额计算 ----------------
def session_cost(s, since=None, until=None):
    """session 落在 [since, until] 内的费用（元）。未结 session 算到 until（默认现在）。"""
    start = float(s["started_at"])
    end = float(s["ended_at"]) if s.get("ended_at") else (until or time.time())
    if until is not None:
        end = min(end, until)
    if since is not None:
        start = max(start, since)
    secs = max(0.0, end - start)
    return secs / 3600.0 * (s.get("price_milli") or 0) / PRICE_UNIT


def session_hours(s, until=None):
    end = float(s["ended_at"]) if s.get("ended_at") else (until or time.time())
    return max(0.0, end - float(s["started_at"])) / 3600.0


def disk_cost(owned_rows, since=None, until=None):
    """关机不释放期间的磁盘费估算：GB × 天 × 0.01（开机期间也收，官方口径"无论是否开机"）。"""
    until = until or time.time()
    total = 0.0
    for o in owned_rows:
        gb = o.get("disk_gb") or 0
        if not gb:
            continue
        start = float(o.get("created_at") or until)
        end = float(o.get("released_at") or until)
        if since is not None:
            start = max(start, since)
        end = min(end, until)
        days = max(0.0, end - start) / 86400.0
        total += gb * days * DISK_YUAN_PER_GB_DAY
    return total


def window_start(kind, now=None):
    """daily / weekly / monthly 窗口起点（本地时间）。"""
    now = datetime.fromtimestamp(now or time.time())
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return day0.timestamp()
    if kind == "weekly":
        return (day0 - timedelta(days=day0.weekday())).timestamp()
    if kind == "monthly":
        return day0.replace(day=1).timestamp()
    raise ValueError(kind)


def parse_since(text, now=None):
    """'7d' / '24h' / '2026-09-01' / 'today' / 'week' / 'month' → 时间戳。"""
    now = now or time.time()
    t = (text or "").strip().lower()
    if not t or t == "month":
        return window_start("monthly", now)
    if t == "today":
        return window_start("daily", now)
    if t == "week":
        return window_start("weekly", now)
    m = re.fullmatch(r"(\d+)([dhw])", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - n * {"d": 86400, "h": 3600, "w": 7 * 86400}[u]
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        raise ValueError(f"无法解析时间: {text!r}（支持 7d / 24h / 2w / today / week / month / YYYY-MM-DD）")


# ---------------- AutoDL 时间字段 ----------------
_FRAC = re.compile(r"(\.\d{6})\d+")


def parse_ts(v):
    """list 接口的时间：'2025-12-15T17:31:05+08:00' 或 {'Time': ..., 'Valid': bool}。无效 → None。"""
    if isinstance(v, dict):
        if not v.get("Valid"):
            return None
        v = v.get("Time")
    if not v or not isinstance(v, str) or v.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(_FRAC.sub(r"\1", v.replace("Z", "+00:00"))).timestamp()
    except ValueError:
        return None


def price_milli(snap):
    try:
        return int(snap.get("payg_price") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


# ---------------- "我的"边界 ----------------
def owner_prefix(cfg):
    o = (getattr(cfg, "owner", "") or "").strip()
    return f"{o}/" if o else ""


def instance_name(cfg, base=None):
    """新建实例的名字：<owner>/<base>，让控制台里一眼看出是谁的。"""
    return owner_prefix(cfg) + (base or cfg.instance_name)


def _ledger(ctx):
    return getattr(ctx, "ledger", None)


def _uuid(item):
    return item.get("uuid") or item.get("instance_uuid")


def is_mine(ctx, item):
    """实例是否归我：名字带 owner 前缀，或账本/台账登记过。"""
    uuid = _uuid(item) if isinstance(item, dict) else item
    name = item.get("name") or "" if isinstance(item, dict) else ""
    pre = owner_prefix(ctx.cfg)
    if pre and name.startswith(pre):
        return True
    led = _ledger(ctx)
    if led is not None and led.is_owned(uuid):
        return True
    reg = getattr(ctx, "reg", None)
    try:
        return bool(reg and reg.get_instance(uuid))
    except Exception:  # noqa: BLE001 - 桩/损坏台账都不该影响判定
        return False


def my_instances(ctx, items=None):
    items = ctx.api.list_instances() if items is None else items
    return [it for it in items if is_mine(ctx, it)]


# ---------------- 生命周期钩子（core / scheduler 调用） ----------------
def on_power_on(ctx, uuid, snap, source="power_on", item=None, log=None):
    """实例进入 running：登记归属 + 开 session + 挂保险丝。绝不抛（记账失败不该阻断任务）。"""
    led = _ledger(ctx)
    if led is None:
        return None
    try:
        started = parse_ts((item or {}).get("started_at")) if item else None
        led.adopt(uuid, owner=ctx.cfg.owner, name=(item or {}).get("name") or "",
                  disk_gb=float(ctx.cfg.expand_disk_gb) if source == "create" else None,
                  created_at=parse_ts((item or {}).get("created_at")) if item else None)
        sid = led.open_session(
            uuid, price_milli(snap), owner=ctx.cfg.owner, project=ctx.cfg.project_name,
            gpu_spec=(snap or {}).get("snapshot_gpu_alias_name") or "",
            req_gpu=(item or {}).get("req_gpu_amount"), region=(snap or {}).get("region_sign") or "",
            source=source, started_at=started)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  ⚠️ 记账失败（不影响任务）: {e}")
        return None
    hours = float(getattr(ctx.cfg.budget, "max_session_hours", 0) or 0)
    if hours > 0 and snap:
        arm_fuse(ctx, snap, uuid, hours, log=log)
    if log:
        p = price_milli(snap) / PRICE_UNIT
        log(f"  记账: session 开始，单价 ¥{p:.2f}/h" + ("" if p else "（未取到价格，稍后对账补）"))
    return sid


def on_power_off(ctx, uuid, ended_at=None, log=None):
    led = _ledger(ctx)
    if led is None:
        return None
    try:
        s = led.close_session(uuid, ended_at)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  ⚠️ 记账失败: {e}")
        return None
    if s and log:
        log(f"  记账: 本段 {session_hours(s):.2f}h × ¥{(s.get('price_milli') or 0) / PRICE_UNIT:.2f}/h "
            f"≈ ¥{s['cost_yuan']:.2f}")
    return s


def on_release(ctx, uuid, log=None):
    led = _ledger(ctx)
    if led is None:
        return
    try:
        led.close_session(uuid)
        led.mark_released(uuid)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  ⚠️ 记账失败: {e}")


def arm_fuse(ctx, snap, uuid, hours, log=None):
    """实例侧保险丝：sleep N 后执行 shutdown（AutoDL 文档：实例内 shutdown 即关机）。
    本地进程挂了/网断了也生效。重复开机会先撤掉旧定时器。失败只告警。"""
    secs = int(hours * 3600)
    cmd = (f"[ -f {FUSE_PID} ] && kill $(cat {FUSE_PID}) 2>/dev/null; "
           f"nohup setsid sh -c 'sleep {secs}; shutdown' >/dev/null 2>&1 & echo $! > {FUSE_PID}")
    try:
        ctx.ssh.run(snap, cmd, uuid)
        if log:
            log(f"  保险丝: {hours:g}h 后实例自动关机（budget.max_session_hours）")
        return True
    except (SSHUnavailable, AutoDLError, OSError) as e:
        if log:
            log(f"  ⚠️ 保险丝未挂上（{e}），请留意手动关机")
        return False


def disarm_fuse(ctx, snap, uuid):
    try:
        ctx.ssh.run(snap, f"[ -f {FUSE_PID} ] && kill $(cat {FUSE_PID}) 2>/dev/null; rm -f {FUSE_PID}", uuid)
        return True
    except (SSHUnavailable, AutoDLError, OSError):
        return False


# ---------------- 对账 ----------------
def reconcile(ctx, items=None, log=None):
    """用账号实例列表修正账本：我的实例 running 却没开 session → 补开（起点用平台 started_at）；
    不 running 却有未结 session → 用平台 stopped_at 关掉；账号里已不存在 → 结清并标记释放。
    返回 {opened, closed, released, mine}。"""
    led = _ledger(ctx)
    if led is None:
        return {"opened": [], "closed": [], "released": [], "mine": []}
    items = ctx.api.list_instances() if items is None else items
    by_uuid = {_uuid(it): it for it in items if _uuid(it)}
    out = {"opened": [], "closed": [], "released": [], "mine": []}
    open_by = {s["instance_uuid"]: s for s in led.open_sessions()}
    for uuid, it in by_uuid.items():
        if not is_mine(ctx, it):
            continue
        out["mine"].append(uuid)
        st = it.get("status")
        led.adopt(uuid, owner=ctx.cfg.owner, name=it.get("name") or "",
                  created_at=parse_ts(it.get("created_at")))
        if st == "running":
            if uuid not in open_by or not open_by[uuid].get("price_milli"):
                snap = None
                try:
                    snap = ctx.api.snapshot(uuid)
                except APIError:
                    pass
                led.open_session(uuid, price_milli(snap or {}), owner=ctx.cfg.owner,
                                 project=ctx.cfg.project_name,
                                 gpu_spec=(snap or {}).get("snapshot_gpu_alias_name") or "",
                                 req_gpu=it.get("req_gpu_amount"), region=it.get("region_sign") or "",
                                 source="adopt", started_at=parse_ts(it.get("started_at")))
                if uuid not in open_by:
                    out["opened"].append(uuid)
        elif uuid in open_by and st not in _BOOTING:
            # 创建中/启动中的实例（batch 刚 create 就占了坑）不算关机，等它 running
            led.close_session(uuid, parse_ts(it.get("stopped_at")) or parse_ts(it.get("status_at")))
            out["closed"].append(uuid)
    for o in led.owned():
        if o["instance_uuid"] not in by_uuid:
            led.close_session(o["instance_uuid"])
            led.mark_released(o["instance_uuid"])
            out["released"].append(o["instance_uuid"])
    if log and (out["opened"] or out["closed"] or out["released"]):
        log(f"  对账: 补开 {len(out['opened'])} / 补关 {len(out['closed'])} / 已释放 {len(out['released'])}")
    return out


# ---------------- 用量与预算 ----------------
def usage(ctx, now=None):
    """我的用量快照：今日/本周/本月花费、当前烧钱速率、running 台数。"""
    led = _ledger(ctx)
    now = now or time.time()
    out = {"today": 0.0, "week": 0.0, "month": 0.0, "disk_month": 0.0,
           "burn_rate": 0.0, "running": 0, "sessions_open": []}
    if led is None:
        return out
    for kind, key in (("daily", "today"), ("weekly", "week"), ("monthly", "month")):
        since = window_start(kind, now)
        out[key] = sum(session_cost(s, since, now) for s in led.sessions(since, now))
    out["disk_month"] = disk_cost(led.owned(include_released=True), window_start("monthly", now), now)
    opens = led.open_sessions()
    out["sessions_open"] = [{"instance_uuid": s["instance_uuid"], "hours": session_hours(s, now),
                             "price": (s.get("price_milli") or 0) / PRICE_UNIT,
                             "cost": session_cost(s, None, now)} for s in opens]
    out["running"] = len(opens)
    out["burn_rate"] = sum(x["price"] for x in out["sessions_open"])
    return out


def check_budget(ctx, price=None, new_instance=False, reconcile_first=True, log=None):
    """预检：任一上限触顶即抛 BudgetExceeded。price=拟开机实例小时价（元），未知给 None。
    new_instance=True 表示会新增一台 running（创建/开机），用于并发上限。"""
    b = getattr(ctx.cfg, "budget", None)
    led = _ledger(ctx)
    if b is None or led is None:
        return None
    if reconcile_first:
        try:
            reconcile(ctx, log=log)
        except APIError as e:
            if log:
                log(f"  ⚠️ 对账失败（{e}），按本地账本预检")
    u = usage(ctx)
    for key, cap, label in (("today", b.daily_yuan, "今日"), ("week", b.weekly_yuan, "本周"),
                            ("month", b.monthly_yuan, "本月")):
        if cap and u[key] >= cap:
            raise BudgetExceeded(f"{label}我的花费 ¥{u[key]:.2f} 已达上限 ¥{cap:.2f}（budget），拒绝开机/提交。"
                                 f"查看: autodl cost；调整: autodl.yaml budget.*")
    if b.max_concurrent and new_instance and u["running"] + 1 > b.max_concurrent:
        raise BudgetExceeded(f"我已有 {u['running']} 台 running，再开会超过 budget.max_concurrent={b.max_concurrent}")
    if b.max_price_per_hour and price is not None and price > b.max_price_per_hour:
        raise BudgetExceeded(f"实例单价 ¥{price:.2f}/h 超过 budget.max_price_per_hour=¥{b.max_price_per_hour:.2f}")
    return u


def run_costs(ctx, runs, now=None):
    """给 registry 的 run 记录配上费用：找覆盖 run 起点的 session，费用 = 单价 × run 时长。"""
    led = _ledger(ctx)
    now = now or time.time()
    out = []
    for r in runs:
        d = dict(r)
        d["cost_yuan"], d["hours"], d["price"] = None, None, None
        st, en = r.get("started_at"), r.get("ended_at") or (now if r.get("status") == "running" else None)
        if led is not None and st and en:
            d["hours"] = max(0.0, en - st) / 3600.0
            ss = [s for s in led.sessions(st, en, instance_uuid=r.get("instance_uuid"))
                  if s["started_at"] <= st]
            if ss:
                p = (ss[-1].get("price_milli") or 0) / PRICE_UNIT
                d["price"], d["cost_yuan"] = p, d["hours"] * p
        out.append(d)
    return out


def report(ctx, since, until=None, by="day"):
    """费用报表。by = day | instance | session。返回 {rows, total, disk, since, until}。"""
    led = _ledger(ctx)
    until = until or time.time()
    rows = []
    total = 0.0
    if led is None:
        return {"rows": rows, "total": 0.0, "disk": 0.0, "since": since, "until": until}
    sess = led.sessions(since, until)
    if by == "session":
        for s in sess:
            c = session_cost(s, since, until)
            total += c
            rows.append({"instance_uuid": s["instance_uuid"], "gpu_spec": s.get("gpu_spec"),
                         "price": (s.get("price_milli") or 0) / PRICE_UNIT,
                         "started_at": s["started_at"], "ended_at": s.get("ended_at"),
                         "hours": session_hours(s, until), "cost": c, "source": s.get("source"),
                         "project": s.get("project")})
    elif by == "instance":
        agg = {}
        for s in sess:
            c = session_cost(s, since, until)
            total += c
            a = agg.setdefault(s["instance_uuid"], {"instance_uuid": s["instance_uuid"], "hours": 0.0,
                                                   "cost": 0.0, "sessions": 0, "gpu_spec": s.get("gpu_spec")})
            start = max(float(s["started_at"]), since)
            end = min(float(s["ended_at"]) if s.get("ended_at") else until, until)
            a["hours"] += max(0.0, end - start) / 3600.0
            a["cost"] += c
            a["sessions"] += 1
        rows = sorted(agg.values(), key=lambda x: -x["cost"])
    else:  # day
        agg = {}
        day = datetime.fromtimestamp(since).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() <= until:
            d0, d1 = day.timestamp(), (day + timedelta(days=1)).timestamp()
            c = sum(session_cost(s, max(d0, since), min(d1, until)) for s in sess)
            h = sum(max(0.0, min(float(s["ended_at"]) if s.get("ended_at") else until, d1, until)
                        - max(float(s["started_at"]), d0, since)) for s in sess) / 3600.0
            if c or h:
                agg[day.strftime("%Y-%m-%d")] = {"day": day.strftime("%Y-%m-%d"), "hours": h, "cost": c}
            total += c
            day += timedelta(days=1)
        rows = list(agg.values())
    disk = disk_cost(led.owned(include_released=True), since, until)
    return {"rows": rows, "total": total, "disk": disk, "since": since, "until": until}
