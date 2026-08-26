"""佐糖 PicWish 高级变清晰客户端——ResizeStep 放大引擎（2026-08-26 新增）。

实测换装依据（350px 低清图，2026-08-26）：
- 输出 2048×2048（~5.8 倍），锐度 Laplacian 2→51（边缘真实重建非插值模糊）；
- 主体色保真 ±2 级（60/180/90→62/177/90）；
- 耗时 ~8s（提交-轮询-下载）。

协议与去水印（watermark.picwish）同款，仅 task path 不同：
POST /api/tasks/visual/scale-pro（multipart image_file，Header X-API-KEY）
→ GET /api/tasks/visual/scale-pro/{task_id} 轮询 state=1（实测 2026-08-26：
  state 0/2/4=进行中（2=Preparing 不是失败——与去水印端点语义不同）、
  负值=失败、1=完成；progress 0→100）
→ data.image（OSS 签名 URL，1h 有效）下载。
输入上限 4096×4096 / 30MB；key 复用 PT_WM_API_KEY（同一佐糖账号）。
任何失败返回 None（调用方降级裸插值兜底，不提示——low-res 已撤 2026-08-27）。
2026-08-26 重构：httpx.Client → core 全局 AsyncClient（http_sync 适配）。
"""

from __future__ import annotations

import logging
import time
import uuid

import cv2
import httpx
import numpy as np

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
        return bool(self._settings.wm_api_enabled and self._settings.wm_api_key)

    def upscale(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """变清晰放大（服务端决定倍率，实测小图出 2048 级）；失败 None。"""
        started_at = time.monotonic()
        deadline = started_at + self._settings.wm_api_timeout_seconds
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
        image_png = cv2.imencode(".png", image_bgr)[1].tobytes()  # 无损：变清晰输入不引入压缩损伤
        response = http_sync(self._http.post(
            f"{_BASE_URL}{_TASK_PATH}",
            headers={"X-API-KEY": self._settings.wm_api_key},
            files={"image_file": (f"sc_{uuid.uuid4().hex[:8]}.png", image_png, "image/png")},
        ))
        response.raise_for_status()
        task_id = response.json().get("data", {}).get("task_id")
        if not task_id:
            raise ValueError("scale-pro submit missing task_id")
        return str(task_id)

    def _poll(self, task_id: str) -> dict:
        response = http_sync(self._http.get(
            f"{_BASE_URL}{_TASK_PATH}/{task_id}",
            headers={"X-API-KEY": self._settings.wm_api_key},
        ))
        response.raise_for_status()
        return response.json().get("data", {})

    def _download(self, image_url: str) -> np.ndarray | None:
        result_bytes = http_sync(self._http.get(image_url)).content
        return cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
