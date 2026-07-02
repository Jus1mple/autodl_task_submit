"""AutoDL REST API 客户端 —— 稳健的 HTTP 地基。

关键设计：
- 统一超时；对网络抖动 / 429 / 5xx 做指数退避重试，但**只对幂等读接口**重试。
  create / power_on / power_off / release 等写接口绝不自动重试，避免重复开机/重复扣费。
- GET 接口参数走 query string（AutoDL 网关会丢弃 GET 的 body）；POST 走 JSON body。
- 响应统一 {code, data, msg, request_id}，code != Success 抛 APIError（保留 code/msg 供分流）。
"""
from __future__ import annotations

import threading
import time

import requests

from .config import Config
from .errors import APIError, RateLimited

# 这些 AutoDL 业务码代表"操作已是目标态"，调用方通常可容忍（如重复关机/已释放）
TOLERABLE_CODES = {"BadRequest"}

# gpu_spec_uuid 固定表（来自官方文档附录，无枚举接口）。
# label 用 AutoDL 控制台/库存的实际名称（括号里标芯片），避免与控制台叫法不一致引起误解：
# 例如 v-48g 在控制台叫「vGPU-48GB」，物理是 48G 显存的 4090（nvidia-smi 显示 RTX 4090）。
GPU_SPECS = [
    {"uuid": "v-48g", "label": "vGPU-48GB（4090·48G）", "category": "通用型"},
    {"uuid": "v-32g-p", "label": "vGPU-32GB（4080S·32G）", "category": "性能型"},
    {"uuid": "4090D", "label": "RTX 4090D", "category": "通用型"},
    {"uuid": "v-48g-350w", "label": "RTX 3090-48G", "category": "通用型"},
    {"uuid": "h800", "label": "H800-80G", "category": "通用型"},
    {"uuid": "pro6000-p", "label": "RTX PRO6000-96G", "category": "性能型"},
    {"uuid": "5090-p", "label": "RTX 5090-32G", "category": "性能型"},
]
GPU_SPEC_LABELS = {s["uuid"]: s["label"] for s in GPU_SPECS}

# region_sign 固定表（官方文档）
REGIONS = [
    {"sign": "westDC2", "name": "西北企业区"},
    {"sign": "westDC3", "name": "西北B区"},
    {"sign": "beijingDC1", "name": "北京A区"},
    {"sign": "beijingDC2", "name": "北京B区"},
    {"sign": "beijingDC3", "name": "北京C区"},
    {"sign": "beijingDC4", "name": "北京D区"},
    {"sign": "neimengDC1", "name": "内蒙A区"},
    {"sign": "neimengDC3", "name": "内蒙C区"},
    {"sign": "foshanDC1", "name": "佛山区"},
    {"sign": "chongqingDC1", "name": "重庆A区"},
    {"sign": "yangzhouDC1", "name": "扬州区"},
]


class AutoDLClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self._headers = {"Authorization": cfg.token, "Content-Type": "application/json"}
        # requests.Session 非线程安全；batch 调度器多线程共用一个 client，
        # 故每个线程惰性持有独立 Session。
        self._local = threading.local()

    @property
    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._headers)
            self._local.session = s
        return s

    # ---------------- 底层请求 ----------------
    def _request(self, method: str, path: str, *, params=None, body=None, idempotent=False):
        url = f"{self.base}{path}"
        timeout = (self.cfg.http.timeout_connect, self.cfg.http.timeout_read)
        attempts = (self.cfg.http.retries + 1) if idempotent else 1
        last_exc = None
        for i in range(attempts):
            try:
                resp = self._session.request(
                    method, url, params=params, json=body, timeout=timeout
                )
            except requests.RequestException as e:
                # 连接/读取层错误：幂等读才重试（写接口不重试，避免请求其实已送达却重复执行）
                last_exc = APIError(f"请求 {path} 网络错误: {e}")
                if i + 1 < attempts:
                    time.sleep(self.cfg.http.backoff * (2 ** i))
                    continue
                raise last_exc

            if resp.status_code == 429:
                if i + 1 < attempts:
                    time.sleep(self.cfg.http.backoff * (2 ** i))
                    continue
                raise RateLimited("被限流(429)", code="RateLimited", status_code=429)
            if resp.status_code >= 500 and idempotent and i + 1 < attempts:
                time.sleep(self.cfg.http.backoff * (2 ** i))
                continue
            if resp.status_code >= 400:
                raise APIError(f"{path} HTTP {resp.status_code}: {resp.text[:300]}",
                               status_code=resp.status_code)

            try:
                payload = resp.json()
            except ValueError:
                raise APIError(f"{path} 返回非 JSON: {resp.text[:300]}",
                               status_code=resp.status_code)
            return payload
        # 理论不可达
        raise last_exc or APIError(f"请求 {path} 失败")

    def _checked(self, payload, path):
        if payload.get("code") != "Success":
            raise APIError(
                f"{path} 失败: {payload.get('code')} {payload.get('msg')}",
                code=payload.get("code"), msg=payload.get("msg"),
                request_id=payload.get("request_id"),
            )
        return payload.get("data")

    def _get(self, path, *, idempotent=True, **params):
        return self._checked(self._request("GET", path, params=params, idempotent=idempotent), path)

    def _post(self, path, body=None, *, idempotent=False, raw=False):
        payload = self._request("POST", path, body=body or {}, idempotent=idempotent)
        if raw:
            return payload  # 调用方自行判断 code（用于可容忍的写操作）
        return self._checked(payload, path)

    # ---------------- 账户 / 资源（只读，可重试） ----------------
    def balance_yuan(self) -> float:
        return self._post("/api/v1/dev/wallet/balance", idempotent=True)["assets"] / 1000.0

    def list_instances(self) -> list:
        data = self._post("/api/v1/dev/instance/pro/list",
                          {"page_index": 1, "page_size": 100}, idempotent=True)
        return data.get("list", [])

    def gpu_stock(self, region_sign, cuda_v_from=None, cuda_v_to=None):
        body = {"region_sign": region_sign}
        if cuda_v_from is not None:
            body["cuda_v_from"] = cuda_v_from
        if cuda_v_to is not None:
            body["cuda_v_to"] = cuda_v_to
        return self._post("/api/v1/dev/machine/region/gpu_stock", body, idempotent=True)

    def status(self, instance_uuid):
        return self._get("/api/v1/dev/instance/pro/status", instance_uuid=instance_uuid)

    def status_or_none(self, instance_uuid):
        try:
            return self.status(instance_uuid)
        except APIError:
            return None

    def snapshot(self, instance_uuid):
        return self._get("/api/v1/dev/instance/pro/snapshot", instance_uuid=instance_uuid)

    def image_list(self):
        return self._post("/api/v1/dev/instance/pro/image/private/list",
                          {"page_index": 1, "page_size": 100}, idempotent=True).get("list", [])

    # ---------------- 写操作（不自动重试） ----------------
    def create(self, *, gpu_spec_uuid=None, req_gpu_amount=None, expand_disk_gb=None,
               data_center_list=None, image_uuid=None, cuda_v_from=None, instance_name=None):
        """创建实例，返回 instance_uuid。未显式给的参数回退到配置默认。"""
        c = self.cfg
        body = {
            "req_gpu_amount": int(req_gpu_amount if req_gpu_amount is not None else c.req_gpu_amount),
            "expand_system_disk_by_gb": int(expand_disk_gb if expand_disk_gb is not None else c.expand_disk_gb),
            "gpu_spec_uuid": gpu_spec_uuid or c.gpu_spec_uuid,
            "image_uuid": image_uuid or c.image_uuid,
            "cuda_v_from": int(cuda_v_from if cuda_v_from is not None else c.cuda_v_from),
            "instance_name": instance_name or c.instance_name,
            "start_command": "sleep infinity",
        }
        dcl = data_center_list if data_center_list is not None else c.data_center_list
        if dcl:
            body["data_center_list"] = list(dcl)
        return self._post("/api/v1/dev/instance/pro/create", body)

    def image_save(self, instance_uuid, image_name):
        """把实例存为私有镜像，返回 image_uuid。注意：AutoDL 无删除镜像 API，镜像会持续占存储费。"""
        data = self._post("/api/v1/dev/instance/pro/image/save",
                          {"instance_uuid": instance_uuid, "image_name": image_name})
        return data.get("image_uuid") if isinstance(data, dict) else data

    def power_on(self, instance_uuid, with_gpu=True):
        # 注意：无卡(CPU)模式的 payload 取值未在文档确证，默认带卡。
        payload = "gpu" if with_gpu else "cpu"
        return self._post("/api/v1/dev/instance/pro/power_on",
                          {"instance_uuid": instance_uuid, "payload": payload})

    def power_off(self, instance_uuid):
        return self._post("/api/v1/dev/instance/pro/power_off",
                          {"instance_uuid": instance_uuid}, raw=True)

    def release(self, instance_uuid):
        return self._post("/api/v1/dev/instance/pro/release",
                          {"instance_uuid": instance_uuid}, raw=True)

    # ---------------- 编排辅助 ----------------
    def wait_running(self, instance_uuid, max_tries=60, interval=5, log=print):
        """轮询直到 running，返回 snapshot。removing/removed 视为异常。"""
        for _ in range(max_tries):
            st = self.status(instance_uuid)
            if log:
                log(f"  状态: {st}")
            if st == "running":
                return self.snapshot(instance_uuid)
            if st in ("removing", "removed"):
                raise APIError(f"实例进入异常状态: {st}", code=st)
            time.sleep(interval)
        raise APIError("等待 running 超时")
