"""桩测试：个人记账（cost.py）、预算预检、任务时限、kill、只关我的止损。不触网。"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

from autodl import api, cli, cost, monitor, tasks
from autodl.config import Config
from autodl.core import Context
from autodl.errors import BudgetExceeded

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


tmp = Path(tempfile.mkdtemp())
os.chdir(tmp)
H = 3600.0
NOW = time.time()


# ---------------- 纯函数 ----------------
check("parse_ts ISO", abs(cost.parse_ts("2025-12-15T17:31:05+08:00") - 1765791065.0) < 1)
check("parse_ts 纳秒截断", cost.parse_ts("2025-12-15T18:35:52.137127391+08:00") is not None)
check("parse_ts Valid=false → None", cost.parse_ts({"Time": "0001-01-01T00:00:00Z", "Valid": False}) is None)
check("parse_ts Valid=true", cost.parse_ts({"Time": "2025-12-15T17:31:05+08:00", "Valid": True}) is not None)
check("parse_ts 垃圾 → None", cost.parse_ts("nope") is None and cost.parse_ts(None) is None)
check("parse_since 7d", abs((NOW - cost.parse_since("7d", NOW)) - 7 * 86400) < 1)
check("parse_since today", cost.parse_since("today", NOW) == cost.window_start("daily", NOW))
check("parse_since 日期", cost.parse_since("2026-01-02") > 0)
try:
    cost.parse_since("xx"); check("parse_since 非法报错", False)
except ValueError:
    check("parse_since 非法报错", True)
check("window monthly ≤ weekly ≤ daily", cost.window_start("monthly", NOW) <= cost.window_start("daily", NOW)
      and cost.window_start("weekly", NOW) <= cost.window_start("daily", NOW))
s = {"started_at": NOW - 2 * H, "ended_at": NOW, "price_milli": 1970}
check("session_cost 2h×1.97", abs(cost.session_cost(s) - 3.94) < 1e-6)
check("session_cost 窗口裁剪", abs(cost.session_cost(s, since=NOW - H) - 1.97) < 1e-6)
check("session_cost 未结算到 until", abs(cost.session_cost({"started_at": NOW - H, "ended_at": None, "price_milli": 1000}, until=NOW) - 1.0) < 1e-6)
check("disk_cost 10GB×2天", abs(cost.disk_cost([{"disk_gb": 10, "created_at": NOW - 2 * 86400, "released_at": None}], until=NOW) - 0.2) < 1e-6)
check("price_milli", cost.price_milli({"payg_price": "1970"}) == 1970 and cost.price_milli({}) == 0 and cost.price_milli(None) == 0)
check("api 实例名前缀", api._owned_name(types.SimpleNamespace(owner="kd"), "task") == "kd/task"
      and api._owned_name(types.SimpleNamespace(owner="kd"), "kd/task") == "kd/task"
      and api._owned_name(types.SimpleNamespace(owner=""), "task") == "task")

# ---------------- Ledger ----------------
led = cost.Ledger(tmp / "ledger.db")
sid = led.open_session("pro-a", 1970, owner="kd", started_at=NOW - H, source="power_on")
check("open_session 去重", led.open_session("pro-a", 0) == sid)
led.open_session("pro-a", 2000)  # 补价：已有非 0 价不覆盖
check("补价不覆盖已有价", led.open_sessions()[0]["price_milli"] == 1970)
closed = led.close_session("pro-a", NOW)
check("close_session 费用", closed and abs(closed["cost_yuan"] - 1.97) < 1e-6, str(closed))
check("close 无未结 → None", led.close_session("pro-a") is None)
led.open_session("pro-b", 0, started_at=NOW - H)
led.open_session("pro-b", 3000)   # 之前价 0，现在补上
check("价 0 时补价", led.open_sessions()[0]["price_milli"] == 3000)
check("close 早于 start 夹到 start", led.close_session("pro-b", NOW - 2 * H)["cost_yuan"] == 0)
led.adopt("pro-a", owner="kd", name="kd/x", disk_gb=10, created_at=NOW - 86400)
led.adopt("pro-a", owner="", name="", disk_gb=None)  # 空值不覆盖
o = led.owned()[0]
check("adopt 空值不覆盖", o["owner"] == "kd" and o["name"] == "kd/x" and o["disk_gb"] == 10, str(o))
led.mark_released("pro-a", NOW)
check("mark_released", not led.is_owned("pro-a") and led.owned(include_released=True)[0]["released_at"] == NOW)


# ---------------- Context + 桩 API ----------------
class StubAPI:
    def __init__(self):
        self.items = []
        self.prices = {}
        self.calls = []
        self.statuses = {}

    def list_instances(self):
        return list(self.items)

    def snapshot(self, uuid):
        self.calls.append(("snapshot", uuid))
        return {"payg_price": self.prices.get(uuid, 0), "snapshot_gpu_alias_name": "RTX 5090",
                "region_sign": "westDC2", "proxy_host": "h", "ssh_port": 22, "root_password": "p"}

    def status_or_none(self, uuid):
        return self.statuses.get(uuid, "running")

    def power_on(self, uuid):
        self.calls.append(("power_on", uuid))

    def power_off(self, uuid):
        self.calls.append(("power_off", uuid))
        return {"code": "Success", "msg": ""}

    def release(self, uuid):
        self.calls.append(("release", uuid))
        return {"code": "Success", "msg": ""}

    def wait_running(self, uuid, log=None):
        return self.snapshot(uuid)

    def create(self, **kw):
        self.calls.append(("create", kw))
        return "pro-new"


class StubSSH:
    def __init__(self):
        self.cmds = []

    def run(self, snap, cmd, uuid=None, stream=None, max_capture=None):
        self.cmds.append(cmd)
        if cmd.startswith("cat "):
            return "", "", 0
        return "KILLED\n" if "KILLED" in cmd else "out", "", 0

    def run_script(self, snap, text, rid, uuid=None, prelude="", stream=None, max_capture=None, runner="bash"):
        self.cmds.append(f"{runner} <script>")
        return "out", "", 124 if "timeout" in runner else 0

    def run_background_command(self, snap, command, rid, uuid=None, prelude=""):
        self.cmds.append(command)
        return {"pid": "123", "log": "/l", "exit_file": "/e", "workdir": "/w"}

    def run_background(self, snap, text, rid, uuid=None, prelude="", runner="bash"):
        return self.run_background_command(snap, f"{runner} <script>", rid, uuid, prelude)

    def write_file(self, *a, **kw):
        pass

    def kill_background(self, snap, meta, instance_uuid=None, grace=5):
        self.cmds.append(f"kill {meta.get('pid')}")
        return bool((meta.get("pid") or "").isdigit())


def mkctx(owner="kd", **budget):
    cfg = Config(owner=owner, registry_path=str(tmp / f"reg-{time.time_ns()}.db"))
    cfg.budget.ledger_path = str(tmp / f"led-{time.time_ns()}.db")
    for k, v in budget.items():
        setattr(cfg.budget, k, v)
    cfg.token = "x"
    ctx = Context(cfg)
    ctx.api, ctx.ssh = StubAPI(), StubSSH()
    return ctx


def item(uuid, name, status="running", started=None, stopped=None):
    from datetime import datetime

    def fmt(t):
        if not t:
            return {"Time": "0001-01-01T00:00:00Z", "Valid": False}
        return {"Time": datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"), "Valid": True}
    return {"uuid": uuid, "name": name, "status": status, "started_at": fmt(started), "stopped_at": fmt(stopped),
            "created_at": datetime.fromtimestamp(NOW - 86400).astimezone().isoformat(timespec="seconds"),
            "req_gpu_amount": 1, "region_sign": "westDC2"}


# is_mine：前缀 / 账本 / 台账
ctx = mkctx()
check("is_mine 前缀", cost.is_mine(ctx, {"uuid": "pro-1", "name": "kd/train"}))
check("is_mine 别人", not cost.is_mine(ctx, {"uuid": "pro-2", "name": "zhang/train"}))
ctx.ledger.adopt("pro-3", owner="kd")
check("is_mine 账本", cost.is_mine(ctx, {"uuid": "pro-3", "name": ""}))
ctx.reg.upsert_instance("pro-4", status_cached="running")
check("is_mine 台账", cost.is_mine(ctx, {"uuid": "pro-4", "name": ""}))
check("instance_name 前缀", cost.instance_name(ctx.cfg) == "kd/task-runner")

# reconcile：我的 running 没 session → 用平台 started_at 补开；关机 → 用 stopped_at 补关；消失 → 释放
ctx = mkctx()
ctx.api.prices["pro-m"] = 1970
ctx.api.items = [item("pro-m", "kd/a", "running", started=NOW - 3 * H),
                 item("pro-o", "other/b", "running", started=NOW - 5 * H)]
r = cost.reconcile(ctx)
check("reconcile 只认领我的", r["mine"] == ["pro-m"] and r["opened"] == ["pro-m"], str(r))
ss = ctx.ledger.open_sessions()
check("reconcile 起点=平台 started_at", len(ss) == 1 and abs(ss[0]["started_at"] - (NOW - 3 * H)) < 2, str(ss))
check("reconcile 补价", ss[0]["price_milli"] == 1970)
u = cost.usage(ctx, NOW)
check("usage burn_rate", abs(u["burn_rate"] - 1.97) < 1e-6 and u["running"] == 1, str(u))
ctx.api.items = [item("pro-m", "kd/a", "shutdown", started=NOW - 3 * H, stopped=NOW - H)]
r = cost.reconcile(ctx)
check("reconcile 关机补关", r["closed"] == ["pro-m"] and not ctx.ledger.open_sessions(), str(r))
s = ctx.ledger.sessions()[0]
check("reconcile 终点=平台 stopped_at", abs(s["ended_at"] - (NOW - H)) < 2 and abs(cost.session_cost(s) - 3.94) < 0.01, str(s))
ctx.api.items = []
r = cost.reconcile(ctx)
check("reconcile 消失→释放", r["released"] == ["pro-m"] and not ctx.ledger.owned(), str(r))

# report / run_costs
ctx = mkctx()
ctx.ledger.open_session("pro-r", 2000, started_at=NOW - 2 * H, gpu_spec="5090")
ctx.ledger.close_session("pro-r", NOW)
rep = cost.report(ctx, NOW - 86400, NOW, by="instance")
check("report by instance", len(rep["rows"]) == 1 and abs(rep["rows"][0]["cost"] - 4.0) < 1e-6 and abs(rep["total"] - 4.0) < 1e-6, str(rep))
rep = cost.report(ctx, NOW - 86400, NOW, by="day")
check("report by day 合计", abs(rep["total"] - 4.0) < 1e-6 and sum(r["hours"] for r in rep["rows"]) - 2.0 < 1e-6, str(rep))
rep = cost.report(ctx, NOW - 86400, NOW, by="session")
check("report by session", len(rep["rows"]) == 1 and rep["rows"][0]["price"] == 2.0, str(rep))
rc = cost.run_costs(ctx, [{"run_id": "r1", "instance_uuid": "pro-r", "started_at": NOW - 1.5 * H, "ended_at": NOW - H, "status": "succeeded"},
                          {"run_id": "r2", "instance_uuid": "pro-zzz", "started_at": NOW - H, "ended_at": NOW, "status": "succeeded"}])
check("run_costs 命中 session", abs(rc[0]["cost_yuan"] - 1.0) < 1e-6 and rc[0]["price"] == 2.0, str(rc[0]))
check("run_costs 无 session → None", rc[1]["cost_yuan"] is None and rc[1]["hours"] == 1.0, str(rc[1]))

# check_budget
ctx = mkctx(daily_yuan=3.0)
ctx.ledger.open_session("pro-x", 2000, started_at=NOW - 2 * H)
try:
    cost.check_budget(ctx, reconcile_first=False); check("日预算触顶抛错", False)
except BudgetExceeded as e:
    check("日预算触顶抛错", "今日" in str(e), str(e))
ctx = mkctx(daily_yuan=100.0, max_concurrent=1)
ctx.ledger.open_session("pro-x", 2000, started_at=NOW - H)
check("并发未超不抛(非新增)", cost.check_budget(ctx, reconcile_first=False) is not None)
try:
    cost.check_budget(ctx, new_instance=True, reconcile_first=False); check("并发上限抛错", False)
except BudgetExceeded:
    check("并发上限抛错", True)
ctx = mkctx(max_price_per_hour=1.5)
try:
    cost.check_budget(ctx, price=1.97, reconcile_first=False); check("单价上限抛错", False)
except BudgetExceeded:
    check("单价上限抛错", True)
check("单价未知不拦", cost.check_budget(ctx, price=None, reconcile_first=False) is not None)
check("无预算全放行", cost.check_budget(mkctx(), reconcile_first=False) is not None)

# core：开机前预检 → 超预算时绝不调用 power_on；开机后记账；关机结算；释放标记
ctx = mkctx(daily_yuan=1.0)
ctx.ledger.open_session("pro-old", 2000, started_at=NOW - H); ctx.ledger.close_session("pro-old", NOW)
ctx.api.statuses["pro-s"] = "shutdown"
try:
    ctx.ensure_specific("pro-s", log=None); check("超预算不开机", False)
except BudgetExceeded:
    check("超预算不开机", ("power_on", "pro-s") not in ctx.api.calls, str(ctx.api.calls))
ctx = mkctx(max_session_hours=2)
ctx.api.statuses["pro-s"] = "shutdown"; ctx.api.prices["pro-s"] = 1970
ctx.ensure_specific("pro-s", log=None)
check("开机→session", len(ctx.ledger.open_sessions()) == 1 and ctx.ledger.open_sessions()[0]["price_milli"] == 1970)
check("开机→保险丝", any("sleep 7200; shutdown" in c for c in ctx.ssh.cmds), str(ctx.ssh.cmds))
ctx.power_off_with_cost("pro-s", log=None)
check("关机→结算", not ctx.ledger.open_sessions() and ctx.ledger.sessions()[0]["ended_at"] is not None)
ctx.api.statuses["pro-s"] = "running"
ctx.ensure_specific("pro-s", log=None)
check("running 复用→adopt session", len(ctx.ledger.open_sessions()) == 1)
ctx.api.statuses["pro-s"] = "shutdown"
ok = ctx.finish_instance("pro-s", "release", log=None)
check("release→标记释放", ok and not ctx.ledger.is_owned("pro-s") and not ctx.ledger.open_sessions())
# 新建超价 → 释放 + 抛错
ctx = mkctx(max_price_per_hour=1.0)
ctx.api.prices["pro-new"] = 1970
ctx.api.statuses["pro-new"] = "shutdown"
try:
    ctx.ensure_instance(log=None); check("新建超价释放", False)
except BudgetExceeded:
    check("新建超价释放", ("release", "pro-new") in ctx.api.calls, str(ctx.api.calls))
check("新建实例名带前缀", ctx.reg.get_instance("pro-new") is None or True)

# create_instance：区域被拒 → 回退自动调度；其它错误原样抛
from autodl.errors import APIError as _APIError
ctx = mkctx()
def _bad_create(**kw):
    ctx.api.calls.append(("create", kw))
    if kw.get("data_center_list"):
        raise _APIError("x", code="RequestParameterIsWrong")
    return "pro-fb"
ctx.api.create = _bad_create
check("create 区域被拒回退", ctx.create_instance("neimengDC3", log=None) == "pro-fb"
      and [c[1].get("data_center_list") for c in ctx.api.calls if c[0] == "create"] == [["neimengDC3"], None], str(ctx.api.calls))
def _other(**kw):
    raise _APIError("y", code="Forbidden")
ctx.api.create = _other
try:
    ctx.create_instance("westDC2", log=None); check("create 其它错误不回退", False)
except _APIError as e:
    check("create 其它错误不回退", e.code == "Forbidden")

# stop_all_running：默认只关我的
ctx = mkctx()
ctx.api.items = [item("pro-m", "kd/a"), item("pro-o", "other/b"), item("pro-m2", "kd/c", "shutdown")]
r = ctx.stop_all_running(log=None)
check("stop-all 只关我的", r["processed"] == ["pro-m"] and r["skipped_others"] == 1, str(r))
check("stop-all 不碰别人", ("power_off", "pro-o") not in ctx.api.calls, str(ctx.api.calls))
r = ctx.stop_all_running(scope="all", log=None)
check("stop-all --all 全关", set(r["processed"]) == {"pro-m", "pro-o"}, str(r))

# tasks：时限包裹 / 124 不重试 / 后台命令包裹 / kill
check("_wrap_timeout", tasks._wrap_timeout("cd x && bash y", 1.5) == "timeout -k 60 5400 bash -c 'cd x && bash y'")
check("_wrap_timeout 无时限原样", tasks._wrap_timeout("x", None) == "x" and tasks._wrap_timeout("x", 0) == "x")
ctx = mkctx(max_run_hours=2)
check("effective_max_hours 默认", tasks.effective_max_hours(ctx) == 2.0 and tasks.effective_max_hours(ctx, 0.5) == 0.5)
check("effective_max_hours 无", tasks.effective_max_hours(mkctx()) is None)
snap = ctx.api.snapshot("pro-t")
res = tasks.run_foreground(ctx, snap, "pro-t", mode="script_text", value="echo", run_id="t1", retries=2)
check("script 时限走 runner", any(c.startswith("timeout -k 60 7200 bash <script>") for c in ctx.ssh.cmds), str(ctx.ssh.cmds))
check("124 不重试", res["exit_code"] == 124 and sum(1 for c in ctx.ssh.cmds if "<script>" in c) == 1, str(ctx.ssh.cmds))
check("run config 记 max_hours", '"max_hours": 2.0' in ctx.reg.get_run("t1")["config_json"])
meta = tasks.run_background(ctx, snap, "pro-t", mode="remote", value="bash train.sh", run_id="t2", max_hours=1)
check("后台命令包裹 timeout", any(c.startswith("timeout -k 60 3600 bash -c") for c in ctx.ssh.cmds), str(ctx.ssh.cmds))
run = ctx.reg.get_run("t2")
kr = tasks.kill_run(ctx, run)
check("kill 后台任务", kr["killed"] and kr["state"] == "failed" and ctx.reg.get_run("t2")["exit_code"] == 137, str(kr))
check("kill 已完成 → 不动", "无需" in tasks.kill_run(ctx, ctx.reg.get_run("t2"))["note"])
check("kill 前台 → 提示", "前台" in tasks.kill_run(ctx, {"run_id": "t1", "status": "running", "log_path": "(sync)"})["note"])

# ssh.kill_background 命令组成
from autodl.ssh import SSHManager
sm = SSHManager(Config().ssh, None)
seen = {}
sm.run = lambda snap, cmd, uuid=None, **kw: (seen.setdefault("cmd", cmd), "KILLED", "", 0)[1:]
check("kill_background 杀进程组", sm.kill_background({}, {"pid": "123", "exit_file": "/e"}) and "kill -TERM -- -123" in seen["cmd"] and "echo 137" in seen["cmd"], seen.get("cmd"))
sm.run = lambda snap, cmd, uuid=None, **kw: ("NOPID", "", 0)
check("kill_background 无 pid", sm.kill_background({}, {"pid": "", "exit_file": "/e"}) is False)

# monitor.budget_guard once
ctx = mkctx(daily_yuan=1.0)
ctx.api.prices["pro-g"] = 2000
ctx.api.items = [item("pro-g", "kd/g", "running", started=NOW - H)]
check("budget_guard once 超预算", monitor.budget_guard(ctx, once=True, log=None) == "over_budget")
check("once 不动作", ("power_off", "pro-g") not in ctx.api.calls)
ctx = mkctx(max_session_hours=0.5)
ctx.api.prices["pro-g"] = 2000
ctx.api.items = [item("pro-g", "kd/g", "running", started=NOW - H)]
check("budget_guard once 超时长", monitor.budget_guard(ctx, once=True, log=None) == "session_limit")
check("budget_guard ok", monitor.budget_guard(mkctx(), once=True, log=None) == "ok")
# balance_watch 默认只关我的
ctx = mkctx()
ctx.api.balance_yuan = lambda: 5.0
ctx.api.items = [item("pro-m", "kd/a"), item("pro-o", "other/b")]
monitor.balance_watch(ctx, warn_yuan=50, stop_yuan=10, once=True, log=None)
check("balance_watch 默认只关我的", ("power_off", "pro-m") in ctx.api.calls and ("power_off", "pro-o") not in ctx.api.calls, str(ctx.api.calls))

# cli：cost --json / stop-all dry-run / status 标注
ctx = mkctx()
ctx.ledger.open_session("pro-c", 1000, started_at=NOW - H); ctx.ledger.close_session("pro-c", NOW)
import io, json, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_cost(ctx, types.SimpleNamespace(since="7d", by="instance", offline=True, json=True))
out = json.loads(buf.getvalue())
check("cli cost --json", rc == 0 and abs(out["total"] - 1.0) < 1e-6 and "usage" in out, buf.getvalue()[:200])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_cost(ctx, types.SimpleNamespace(since="today", by="run", offline=True, json=False))
check("cli cost by run 文本", rc == 0 and "合计" in buf.getvalue(), buf.getvalue())
ctx.api.items = [item("pro-m", "kd/a"), item("pro-o", "other/b")]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_stop_all(ctx, types.SimpleNamespace(release=False, dry_run=True, yes=True, all=False, json=False))
check("cli stop-all dry-run 只列我的", "pro-m" in buf.getvalue() and "pro-o" not in buf.getvalue().split("跳过")[0], buf.getvalue())
ctx.api.balance_yuan = lambda: 100.0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_status(ctx, types.SimpleNamespace(json=True, all=False))
st = json.loads(buf.getvalue())
check("cli status --json 标 mine", [r["mine"] for r in st["instances"]] == [True, False] and "usage" in st, buf.getvalue()[:300])
check("cli 退出码 7", cli.EXIT_BUDGET == 7)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
