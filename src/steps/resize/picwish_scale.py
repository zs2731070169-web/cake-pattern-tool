"""佐糖 PicWish 高级变清晰客户端——ResizeStep 放大引擎（2026-08-28 第十八次修订恢复）。

决策记录：石榴超分历版（直出/链式/高级版打底）用户目检均不符合要求，
2026-08-28 定案超分主力回佐糖 scale-pro（key 已充值）；客户端从 git 历史恢复
（第十一次修订曾删），配置键独立化 wm_api_key → picwish_api_key。

协议（官方文档 https://picwish.cn/scale-pro-api-doc，2026-08-28 抓取核验；
调用形态 = 显式异步 sync=0，用户指定）：
POST https://techsz.aoscdn.com/api/tasks/visual/scale-pro
  multipart: image_file + sync=0（异步：提交即返 task_id 不等待处理）
  Header: X-API-KEY
→ GET /api/tasks/visual/scale-pro/{task_id} 轮询（官方：1s 间隔、预算 180s）
  data.state: 1=成功（data.image = OSS URL 1h 有效）/ 负值=失败 /
  0/2/4=进行中（2026-08-26 实测 2=Preparing 非失败）
→ 下载 data.image。
输入上限 4096×4096 / 30MB。
任何失败返回 None（调用方记 failed 不交付——第十七次修订语义）。
传输层走 core 全局 AsyncClient（http_sync 适配，单循环线程）。
"""

from __future__ import annotations

import logging
import time
import uuid

import cv2
import httpx
import numpy as np

from src.core.api_throttle import provider_slot
from src.core.config import PatternToolSettings
from src.core.http import get_http_client, http_sync

module_logger = logging.getLogger("pattern_tool.picwish_scale")

_BASE_URL = "https://techsz.aoscdn.com"
_TASK_PATH = "/api/tasks/visual/scale-pro"
_POLL_INTERVAL_SECONDS = 2
SCALE_PRO_MAX_SIDE = 2048  # 实测输出上限（350px 输入出 2048；更大输入按比例上限内）


class PicwishScalePro:
    """佐糖高级变清晰（异步任务：提交-轮询-下载；永不抛出，失败 None）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.scale_enabled and self._settings.picwish_api_key)

    def upscale(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """变清晰放大（服务端决定倍率，实测小图出 2048 级）；失败 None。"""
        # 并发闸（第三十八次修订）：任务周期整体排队（提交-轮询-下载）——
        # 批内并行下 9 路并发提交实测 429；闸在时钟**之前**获取，排队时间
        # 不吃 picwish_timeout_seconds 轮询预算
        with provider_slot("picwish", self._settings.picwish_max_concurrent):
            started_at = time.monotonic()
            deadline = started_at + self._settings.picwish_timeout_seconds
            try:
                task_id = self._submit(image_bgr)
                module_logger.info("scale-pro task %s submitted", task_id)
                while time.monotonic() < deadline:
                    time.sleep(_POLL_INTERVAL_SECONDS)
                    data = self._poll(task_id)
                    state = data.get("state")
                    if state == 1:
                        image_url = data.get("image") or data.get("file")
                        if not image_url:
                            return None
                        module_logger.info(
                            "scale-pro task %s done in %.1fs", task_id, time.monotonic() - started_at
                        )
                        return self._download(image_url)
                    if state is not None and state < 0:  # 仅负值=失败；0/2/4=进行中
                        module_logger.warning("scale-pro task failed: %s", data.get("state_detail"))
                        return None
                    elapsed = time.monotonic() - started_at
                    if int(elapsed) % 10 < _POLL_INTERVAL_SECONDS:
                        module_logger.info(
                            "scale-pro task %s polling %.0fs (state=%s)", task_id, elapsed, state
                        )
                module_logger.warning(
                    "scale-pro task %s timeout after %.0fs", task_id, time.monotonic() - started_at
                )
                return None
            except (httpx.HTTPError, ValueError, KeyError) as scale_error:
                module_logger.warning(
                    "scale-pro api failed after %.1fs: %s",
                    time.monotonic() - started_at, scale_error,
                )
                return None

    # ---- 三步协议（同去水印骨架，仅 path 不同）----

    def _submit(self, image_bgr: np.ndarray) -> str:
        """异步提交（sync=0）：立即返回 task_id，处理结果由轮询获取——
        并发大/网络慢时成功率高于同步（官方文档异步调优点）。"""
        image_png = cv2.imencode(".png", image_bgr)[1].tobytes()  # 无损：变清晰输入不引入压缩损伤
        response = http_sync(self._http.post(
            f"{_BASE_URL}{_TASK_PATH}",
            headers={"X-API-KEY": self._settings.picwish_api_key},
            files={"image_file": (f"sc_{uuid.uuid4().hex[:8]}.png", image_png, "image/png")},
            data={"sync": "0"},
        ))
        response.raise_for_status()
        task_id = response.json().get("data", {}).get("task_id")
        if not task_id:
            raise ValueError("scale-pro submit missing task_id")
        return str(task_id)

    def _poll(self, task_id: str) -> dict:
        """轮询任务状态（官方口径 1s 间隔；预算由 picwish_timeout_seconds 控，默认 110s）。"""
        response = http_sync(self._http.get(
            f"{_BASE_URL}{_TASK_PATH}/{task_id}",
            headers={"X-API-KEY": self._settings.picwish_api_key},
        ))
        response.raise_for_status()
        return response.json().get("data", {})

    def _download(self, image_url: str) -> np.ndarray | None:
        result_bytes = http_sync(self._http.get(image_url)).content
        return cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
