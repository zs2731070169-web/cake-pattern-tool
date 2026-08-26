"""佐糖 PicWish 高级版全屏自动去水印客户端——去水印修复主力（2026-08-26 回退恢复）。

2026-08-26 当日曾换装 qwen-image-2.0 生成式（单图对比改动面 5.6% vs 44.5% 占优），
但多图实测整体效果不理想，用户定案回退佐糖高级版（10 算粒/次，全屏生成式
重建，2026-08-25 首轮实测：满图斜排浅字带清除、主体完整）。

协议（2026-08-25 实测打通，高级版）：
1. POST /api/tasks/visual/advanced/watermark-remove
   multipart 字段 image_file（图片字节），Header X-API-KEY → {data: {task_id}}；
2. GET /api/tasks/visual/advanced/watermark-remove/{task_id}
   → {data: {state, image_url}}；state=1 完成，image_url 为 OSS 签名 URL（1h 有效）；
3. 下载 image_url 字节流（输出可能放大——缩回原幅）。

2026-08-26 重构：httpx.Client → core 全局 AsyncClient（连接池共享、统一超时），
调用经 http_sync 适配线程上下文——步骤代码保持同步面。
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

module_logger = logging.getLogger("pattern_tool.watermark_picwish")

_BASE_URL = "https://techsz.aoscdn.com"
_TASK_PATH = "/api/tasks/visual/advanced/watermark-remove"
_POLL_INTERVAL_SECONDS = 2


class PicwishWatermarkRemover:
    """佐糖高级版全屏去水印（异步任务：提交-轮询-下载；线程安全由连接池保证）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.wm_api_enabled and self._settings.wm_api_key)

    def remove_watermark(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """全屏自动去水印；任何失败/超时返回 None（不抛出，由调用方降级）。

        同步等待至完成（2026-08-25 口径：必须等整个管线跑完才回显——不做
        提前降级回显）；等待上限 = wm_api_timeout_seconds，到限返回 None。
        """
        started_at = time.monotonic()
        deadline_timestamp = started_at + self._settings.wm_api_timeout_seconds
        try:
            task_id = self._submit(image_bgr)
            module_logger.info("picwish task %s submitted", task_id)
            while time.monotonic() < deadline_timestamp:
                time.sleep(_POLL_INTERVAL_SECONDS)
                poll_data = self._poll(task_id)
                state = poll_data.get("state")
                if state == 1:
                    result_url = poll_data.get("image_url") or poll_data.get("file")
                    if not result_url:
                        return None
                    module_logger.info(
                        "picwish task %s done in %.1fs", task_id, time.monotonic() - started_at
                    )
                    return self._download(result_url, image_bgr.shape[:2])
                if state in (-1, 2):
                    module_logger.warning("picwish task failed: %s", poll_data.get("state_detail"))
                    return None
                # 其余 state（0/4 进行中）继续轮询；每 10s 一条进度打点
                elapsed = time.monotonic() - started_at
                if int(elapsed) % 10 < _POLL_INTERVAL_SECONDS:
                    module_logger.info(
                        "picwish task %s polling %.0fs (state=%s)", task_id, elapsed, state
                    )
            module_logger.warning(
                "picwish task %s timeout after %.0fs", task_id, time.monotonic() - started_at
            )
            return None  # 到限（管线超时兜底 failed；同步口径）
        except (httpx.HTTPError, ValueError, KeyError) as picwish_error:
            module_logger.warning(
                "picwish watermark api failed after %.1fs: %s",
                time.monotonic() - started_at, picwish_error,
            )
            return None

    # ---- 三步协议 ----

    def _submit(self, image_bgr: np.ndarray) -> str:
        """multipart 上传创建任务（字段 image_file；文件名随机后缀防 OSS 禁覆盖）。"""
        image_jpg = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()
        submit_response = http_sync(self._http.post(
            f"{_BASE_URL}{_TASK_PATH}",
            headers={"X-API-KEY": self._settings.wm_api_key},
            files={"image_file": (f"wm_{uuid.uuid4().hex[:8]}.jpg", image_jpg, "image/jpeg")},
        ))
        submit_response.raise_for_status()
        task_id = submit_response.json().get("data", {}).get("task_id")
        if not task_id:
            raise ValueError("picwish submit missing task_id")
        return str(task_id)

    def _poll(self, task_id: str) -> dict:
        """轮询任务（data.state：1 完成 / -1、2 失败 / 0、4 进行中）。"""
        poll_response = http_sync(self._http.get(
            f"{_BASE_URL}{_TASK_PATH}/{task_id}",
            headers={"X-API-KEY": self._settings.wm_api_key},
        ))
        poll_response.raise_for_status()
        return poll_response.json().get("data", {})

    def _download(self, result_url: str, expected_shape: tuple[int, int]) -> np.ndarray | None:
        """下载结果（OSS 签名 URL，1h 有效）；高级版输出可能放大，缩回原幅。"""
        result_bytes = http_sync(self._http.get(result_url)).content
        result_bgr = cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if result_bgr is None:
            return None
        if result_bgr.shape[:2] != expected_shape:
            result_bgr = cv2.resize(
                result_bgr, (expected_shape[1], expected_shape[0]), interpolation=cv2.INTER_AREA
            )
        return result_bgr
