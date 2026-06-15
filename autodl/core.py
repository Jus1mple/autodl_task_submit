"""高层编排：复用/新建实例、库存驱动选区、余额差分计费。

把零散的生命周期逻辑收口到一处，供各 CLI 子命令调用。
"""
from __future__ import annotations

import time

from .api import AutoDLClient
from .errors import APIError, AutoDLError
from .registry import Registry
from .ssh import SSHManager

# 可复用（开机即可用）的状态
_REUSABLE_DIRECT = {"running"}
_REUSABLE_AFTER_POWERON = {"shutdown", "power_off"}
_TRANSIENT_BOOTING = {"starting", "sys_volume_creating"}


class Context:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = AutoDLClient(cfg)
        self.reg = Registry(cfg.registry_abspath)
        self.ssh = SSHManager(cfg.ssh, self.api)

    # ---------------- 选区 ----------------
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
                    return uuid, snap
                if st in _REUSABLE_AFTER_POWERON:
                    self._record_balance(uuid)
                    self.api.power_on(uuid)
                    if log:
                        log(f"开机中: {uuid}")
                    snap = self.api.wait_running(uuid, log=log)
                    self.reg.upsert_instance(uuid, status_cached="running")
                    return uuid, snap
                if st in _TRANSIENT_BOOTING:
                    snap = self.api.wait_running(uuid, log=log)
                    self.reg.upsert_instance(uuid, status_cached="running")
                    return uuid, snap
                # removed / None / 其它 → 记录失效
                if log:
                    log("  记录的实例已不可用，将新建。")
                self.reg.remove_instance(uuid)
                self.reg.set_active(None)

            # 新建
            if select_region:
                region = self.select_region(log=log)
                if region:
                    uuid = self.api.create_in_region([region])
                else:
                    if log:
                        log("  偏好区域均无空闲，回退自动调度创建。")
                    uuid = self.api.create()
            else:
                uuid = self.api.create()
            if log:
                log(f"实例已创建: {uuid}")
            self.reg.upsert_instance(uuid, name=self.cfg.instance_name, status_cached="creating")
            self.reg.set_active(uuid)
            self._record_balance(uuid)
            snap = self.api.wait_running(uuid, log=log)
            self.reg.upsert_instance(
                uuid, status_cached="running", region=snap.get("region_sign"),
                gpu_spec=snap.get("snapshot_gpu_alias_name"),
            )
            return uuid, snap

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
            return self.api.snapshot(uuid)
        if st in _REUSABLE_AFTER_POWERON:
            self._record_balance(uuid)
            self.api.power_on(uuid)
            if log:
                log(f"开机中: {uuid}")
            snap = self.api.wait_running(uuid, log=log)
            self.reg.upsert_instance(uuid, status_cached="running")
            return snap
        if st in _TRANSIENT_BOOTING:
            return self.api.wait_running(uuid, log=log)
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
    def stop_all_running(self, release=False, log=print):
        """关停所有 running 实例（release=True 则进一步释放）。
        单台失败**不影响其余**（这是唯一的急停路径，绝不能因一台报错放弃其它）。
        返回 {"processed": [...], "failed": [...]}。"""
        running = [it.get("instance_uuid") or it.get("uuid")
                   for it in self.api.list_instances() if it.get("status") == "running"]
        running = [u for u in running if u]
        failed = []
        for u in running:
            try:
                r = self.api.power_off(u)
                if log:
                    log(f"  关机 {u}: {r.get('code')} {r.get('msg')}")
                self.reg.upsert_instance(u, status_cached="shutdown")
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
                except AutoDLError as e:
                    if u not in failed:
                        failed.append(u)
                    if log:
                        log(f"  ⚠️ 释放 {u} 失败: {e}（磁盘仍计费）")
            if self.reg.get_active() in running:
                self.reg.set_active(None)
        if failed and log:
            log(f"⚠️ {len(failed)} 台未成功处理、仍在计费: {failed} —— 请手动 `autodl stop-all --release` 或去控制台处理")
        return {"processed": [u for u in running if u not in failed], "failed": failed}

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

    # ---------------- 余额差分计费 ----------------
    def _record_balance(self, uuid):
        try:
            self.reg.upsert_instance(uuid, last_balance=self.api.balance_yuan())
        except APIError:
            pass

    def power_off_with_cost(self, uuid, log=print):
        """关机并用余额差分粗估本段花费（仅在单实例独占计费时才准）。"""
        inst = self.reg.get_instance(uuid)
        before = inst.get("last_balance") if inst else None
        resp = self.api.power_off(uuid)
        if log:
            log(f"  关机: {resp.get('code')} {resp.get('msg')}")
        self.reg.upsert_instance(uuid, status_cached="shutdown")
        try:
            after = self.api.balance_yuan()
            if before is not None:
                spent = before - after
                if log:
                    log(f"  本段约花费 ¥{spent:.2f}（余额差分粗估，多实例并发时不准）")
        except APIError:
            pass
