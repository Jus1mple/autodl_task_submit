"""高层编排：复用/新建实例、库存驱动选区、个人记账与预算护栏。

把零散的生命周期逻辑收口到一处，供各 CLI 子命令调用。
每个开机/关机/释放入口都挂了 cost.* 钩子：共享账号下只记"我的"那份账。
"""
from __future__ import annotations

import time

from . import cost
from .api import AutoDLClient
from .cost import Ledger
from .errors import APIError, AutoDLError, BudgetExceeded
from .registry import Registry
from .ssh import SSHManager

# 可复用（开机即可用）的状态
_REUSABLE_DIRECT = {"running"}
_REUSABLE_AFTER_POWERON = {"shutdown", "power_off"}
_TRANSIENT_BOOTING = {"starting", "sys_volume_creating"}


def billing_class(status):
    """实例状态 → 计费口径（CLI 与 web-dashboard 分支共用的展示逻辑）。"""
    if status == "running":
        return "带卡计费"
    if status in ("shutdown", "power_off", "shutting_down"):
        return "仅磁盘计费"
    return status or "?"


class Context:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = AutoDLClient(cfg)
        self.reg = Registry(cfg.registry_abspath)
        self.ssh = SSHManager(cfg.ssh, self.api)
        self.ledger = Ledger(cfg.ledger_abspath)   # 个人账本（人级，跨项目）

    # ---------------- 记账辅助 ----------------
    def _list_item(self, uuid):
        """list 接口里该实例的条目（含平台 started_at 等）；拿不到返回 None。"""
        try:
            for it in self.api.list_instances():
                if (it.get("uuid") or it.get("instance_uuid")) == uuid:
                    return it
        except APIError:
            pass
        return None

    def _price_of(self, uuid):
        """开机前尽力取小时价（元）做预算预检；取不到返回 None（关机实例的 snapshot 未必可用）。"""
        try:
            return cost.price_milli(self.api.snapshot(uuid)) / cost.PRICE_UNIT or None
        except APIError:
            return None

    def _power_on_checked(self, uuid, log):
        """开机三步：预算预检 → power_on → 记账。"""
        cost.check_budget(self, price=self._price_of(uuid), new_instance=True, log=log)
        self.api.power_on(uuid)
        if log:
            log(f"开机中: {uuid}")
        snap = self.api.wait_running(uuid, log=log)
        self.reg.upsert_instance(uuid, status_cached="running")
        cost.on_power_on(self, uuid, snap, "power_on", log=log)
        return snap

    # ---------------- 选区 / 创建 ----------------
    def create_instance(self, region=None, log=print, **kw):
        """创建实例。指定区域被平台以 RequestParameterIsWrong 拒绝时（实测 neimengDC3 会被拒，
        库存接口的 region_sign 与 create 接受的取值不完全一致），回退为不指定区域自动调度。
        参数错误的 create 不会产生实例，所以这次回退不会重复开机。"""
        if region:
            try:
                return self.api.create(data_center_list=[region], **kw)
            except APIError as e:
                if e.code != "RequestParameterIsWrong":
                    raise
                if log:
                    log(f"  区域 {region} 被 create 拒绝（{e.code}），回退自动调度")
        return self.api.create(**kw)

    def select_region(self, log=print):
        """遍历偏好区域，返回第一个目标 GPU 有空闲的 region_sign；都没有则 None。"""
        target = self.cfg.gpu_stock_name
        for region in self.cfg.regions:
            try:
                stock = self.api.gpu_stock(region, cuda_v_from=self.cfg.cuda_v_from) or []
            except APIError as e:
                if log:
                    log(f"  [{region}] 库存查询失败: {e}")
                continue
            for entry in stock:
                for name, st in entry.items():
                    if target.lower() in name.lower() and st.get("idle_gpu_num", 0) > 0:
                        if log:
                            log(f"  选中区域 {region}：{name} 空闲 {st['idle_gpu_num']}")
                        return region
        return None

    # ---------------- 复用 / 新建 ----------------
    def ensure_instance(self, select_region=False, log=print):
        """复用台账里的活动实例（关机则开机），不可用则新建。返回 (instance_uuid, snapshot)。"""
        with self.reg.lock():
            uuid = self.reg.get_active()
            if uuid:
                st = self.api.status_or_none(uuid)
                if log:
                    log(f"复用实例 {uuid}，状态: {st}")
                if st == "shutting_down":
                    st = self._wait_until(uuid, _REUSABLE_AFTER_POWERON | {"shutdown"}, log)
                if st in _REUSABLE_DIRECT:
                    snap = self.api.snapshot(uuid)
                    self.reg.upsert_instance(uuid, status_cached=st)
                    cost.on_power_on(self, uuid, snap, "adopt", item=self._list_item(uuid))
                    return uuid, snap
                if st in _REUSABLE_AFTER_POWERON:
                    return uuid, self._power_on_checked(uuid, log)
                if st in _TRANSIENT_BOOTING:
                    snap = self.api.wait_running(uuid, log=log)
                    self.reg.upsert_instance(uuid, status_cached="running")
                    cost.on_power_on(self, uuid, snap, "adopt", log=log)
                    return uuid, snap
                # removed / None / 其它 → 记录失效
                if log:
                    log("  记录的实例已不可用，将新建。")
                self.reg.remove_instance(uuid)
                self.reg.set_active(None)

            # 新建（新计费）：先过预算/并发预检
            cost.check_budget(self, new_instance=True, log=log)
            region = self.select_region(log=log) if select_region else None
            if select_region and not region and log:
                log("  偏好区域均无空闲，回退自动调度创建。")
            uuid = self.create_instance(region, log=log)
            if log:
                log(f"实例已创建: {uuid}")
            self.reg.upsert_instance(uuid, name=cost.instance_name(self.cfg), status_cached="creating")
            self.reg.set_active(uuid)
            snap = self.api.wait_running(uuid, log=log)
            self.reg.upsert_instance(
                uuid, status_cached="running", region=snap.get("region_sign"),
                gpu_spec=snap.get("snapshot_gpu_alias_name"),
            )
            self._after_create(uuid, snap, "create", log)
            return uuid, snap

    def _after_create(self, uuid, snap, source, log):
        """新建实例就绪后：记账 + 单价上限后置检查（创建前拿不到价格）。超价立即释放。"""
        cost.on_power_on(self, uuid, snap, source, log=log)
        cap = float(getattr(self.cfg.budget, "max_price_per_hour", 0) or 0)
        p = cost.price_milli(snap) / cost.PRICE_UNIT
        if cap and p > cap:
            if log:
                log(f"  ⚠️ 新实例单价 ¥{p:.2f}/h 超过 budget.max_price_per_hour=¥{cap:.2f}，立即释放")
            self.finish_instance(uuid, "release", log=log)
            raise BudgetExceeded(f"新实例 {uuid} 单价 ¥{p:.2f}/h 超过上限 ¥{cap:.2f}，已释放")

    def use_instance(self, uuid, log=print):
        """登记一台已有实例为当前活动实例（不创建）。返回其当前状态。"""
        with self.reg.lock():
            st = self.api.status_or_none(uuid)
            self.reg.upsert_instance(uuid, status_cached=st or "unknown")
            self.reg.set_active(uuid)
        if log:
            log(f"已登记活动实例 {uuid}，当前状态: {st}")
        return st

    def ensure_specific(self, uuid, log=print):
        """确保指定的已有实例处于 running（关机则开机），返回 snapshot。绝不新建。"""
        st = self.api.status_or_none(uuid)
        if log:
            log(f"目标实例 {uuid} 状态: {st}")
        if st is None or st == "removed":
            raise APIError(f"实例 {uuid} 不存在或已释放", code=st)
        if st == "shutting_down":
            st = self._wait_until(uuid, _REUSABLE_AFTER_POWERON | {"shutdown"}, log)
        if st in _REUSABLE_DIRECT:
            snap = self.api.snapshot(uuid)
            cost.on_power_on(self, uuid, snap, "adopt", item=self._list_item(uuid))
            return snap
        if st in _REUSABLE_AFTER_POWERON:
            return self._power_on_checked(uuid, log)
        if st in _TRANSIENT_BOOTING:
            snap = self.api.wait_running(uuid, log=log)
            cost.on_power_on(self, uuid, snap, "adopt", log=log)
            return snap
        raise APIError(f"实例 {uuid} 状态异常无法使用: {st}", code=st)

    def _wait_until(self, uuid, target_states, log, max_tries=36, interval=5):
        for _ in range(max_tries):
            st = self.api.status_or_none(uuid)
            if log:
                log(f"  等待状态稳定: {st}")
            if st in target_states or st in (None, "removed"):
                return st
            time.sleep(interval)
        return self.api.status_or_none(uuid)

    # ---------------- 批量止损 ----------------
    def running_instances(self, scope="mine"):
        """账号里 running 的实例。scope=mine 只要我的（owner 前缀 / 账本 / 台账登记过）；all 全部。
        返回 (mine_or_all, skipped_others)。"""
        items = [it for it in self.api.list_instances() if it.get("status") == "running"]
        if scope == "all":
            return [it for it in items if (it.get("uuid") or it.get("instance_uuid"))], []
        mine, others = [], []
        for it in items:
            (mine if cost.is_mine(self, it) else others).append(it)
        return mine, others

    def stop_all_running(self, release=False, scope="mine", log=print):
        """关停 running 实例（release=True 则进一步释放）。**默认只关我的**（scope=mine）：
        共享账号里 stop_all 会误杀别人的训练，scope=all 必须显式指定。
        单台失败**不影响其余**（这是唯一的急停路径，绝不能因一台报错放弃其它）。
        返回 {"processed": [...], "failed": [...], "skipped_others": n}。"""
        items, others = self.running_instances(scope)
        running = [it.get("uuid") or it.get("instance_uuid") for it in items]
        if others and log:
            log(f"  跳过 {len(others)} 台别人的 running 实例（--all 才会碰）")
        failed = []
        for u in running:
            try:
                r = self.api.power_off(u)
                if log:
                    log(f"  关机 {u}: {r.get('code')} {r.get('msg')}")
                self.reg.upsert_instance(u, status_cached="shutdown")
                cost.on_power_off(self, u, log=log)
            except AutoDLError as e:
                failed.append(u)
                if log:
                    log(f"  ⚠️ 关机 {u} 失败: {e}（仍在计费，请稍后重试）")
        if release and running:
            time.sleep(15)
            for u in running:
                try:
                    r = self.api.release(u)
                    if log:
                        log(f"  释放 {u}: {r.get('code')} {r.get('msg')}")
                    self.reg.remove_instance(u)
                    cost.on_release(self, u, log=log)
                except AutoDLError as e:
                    if u not in failed:
                        failed.append(u)
                    if log:
                        log(f"  ⚠️ 释放 {u} 失败: {e}（磁盘仍计费）")
            if self.reg.get_active() in running:
                self.reg.set_active(None)
        if failed and log:
            log(f"⚠️ {len(failed)} 台未成功处理、仍在计费: {failed} —— 请手动 `autodl stop-all --release` 或去控制台处理")
        return {"processed": [u for u in running if u not in failed], "failed": failed,
                "skipped_others": len(others)}

    # ---------------- 环境固化为私有镜像 ----------------
    def snapshot_env(self, instance_uuid, image_name, wait=True, poll_interval=10, max_tries=180, log=print):
        """把实例存成私有镜像；wait=True 则轮询直到 status=finished。返回 image_uuid。
        注意：AutoDL 无删除镜像 API，镜像会持续占存储费，确认需要再用。"""
        image_uuid = self.api.image_save(instance_uuid, image_name)
        if log:
            log(f"已发起保存镜像: {image_uuid}（name={image_name}）")
        if not wait:
            return image_uuid
        for _ in range(max_tries):
            for img in self.api.image_list():
                if img.get("image_uuid") == image_uuid:
                    st = img.get("status")
                    if log:
                        log(f"  镜像状态: {st}")
                    if st == "finished":
                        return image_uuid
            time.sleep(poll_interval)
        if log:
            log("  保存超时（镜像可能仍在后台生成，可稍后用 image_list 查看）")
        return image_uuid

    # ---------------- 关机 + 记账 ----------------
    def power_off_with_cost(self, uuid, log=print):
        """关机并结算本段 session（单价 × 时长）。共享账号下不再用余额差分——那算不出我的份。"""
        resp = self.api.power_off(uuid)
        if log:
            log(f"  关机: {resp.get('code')} {resp.get('msg')}")
        self.reg.upsert_instance(uuid, status_cached="shutdown")
        cost.on_power_off(self, uuid, log=log)

    # ---------------- 统一收尾 ----------------
    def finish_instance(self, uuid, mode="power_off", log=print, release_retries=3):
        """收尾一台实例：keep（不动）/ power_off（关机保留复用）/ release（关机并彻底释放）。
        替代散落各处的 power_off + sleep(15) + release 盲等：release 前轮询等实例真正
        shutdown，release 的业务失败码也算失败并重试。返回 True=达成目标；
        False=失败（实例可能仍在计费，已显著告警，绝不静默）。"""
        if mode == "keep":
            return True
        try:
            self.power_off_with_cost(uuid, log=log)
        except AutoDLError as e:
            if log:
                log(f"  ⚠️ 关机 {uuid} 失败: {e}")
            if mode == "power_off":
                if log:
                    log(f"  ⚠️ 实例 {uuid} 可能仍在计费，请 `autodl stop-all` 或去控制台处理")
                return False
        if mode == "power_off":
            return True
        # release：等实例真正 shutdown（release 的前置条件），带重试
        for attempt in range(release_retries):
            st = self._wait_until(uuid, _REUSABLE_AFTER_POWERON, None, max_tries=10, interval=3)
            if st in (None, "removed"):
                break  # 实例已不存在，视为释放完成
            try:
                r = self.api.release(uuid)
            except AutoDLError as e:
                r = {"code": "Error", "msg": str(e)}
            if log:
                log(f"  释放 {uuid}: {r.get('code')} {r.get('msg') or ''}")
            if r.get("code") == "Success":
                break
            time.sleep(5)
        else:
            if log:
                log(f"  ⚠️⚠️ 实例 {uuid} 释放失败、仍在计费！"
                    f"请尽快 `autodl stop-all --release` 或去控制台处理。")
            return False
        self.reg.remove_instance(uuid)
        cost.on_release(self, uuid, log=log)
        if self.reg.get_active() == uuid:
            self.reg.set_active(None)
        return True
