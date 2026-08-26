"""qwen-vl 语义预检——去水印检测器（4.4 v3 三段链路）。

has_watermark：视觉问答判有无水印（~¥0.003/次，唯一检测器——OpenCV 检测
2026-08-25 退役）；subject_bbox：主体边界框（旧填充语义掩膜通道，保留接口）。
图经百炼临时存储（getPolicy → OSS 上传 → oss:// URL，模型绑定可配 wm_precheck_model）。
2026-08-26 重构：httpx.Client → core 全局 AsyncClient（http_sync 适配）。
"""

from __future__ import annotations

import logging
import time

import cv2
import httpx
import numpy as np

from src.core.config import PatternToolSettings
from src.core.http import get_http_client, http_sync

module_logger = logging.getLogger("pattern_tool.watermark_precheck")

_VL_MODEL_FALLBACK = "qwen-vl-plus"
_VL_PROMPT = (
    "仔细观察这张图片，判断是否存在水印（包括半透明文字、斜排浅色文字、"
    'logo水印、角标日期、平台水印等）。只回答 JSON：{"has_watermark": true/false}'
)
_SUBJECT_BBOX_PROMPT = (
    "观察这张图片，找到主体图案（人物/动物/物品等前景内容）的边界框。"
    '只回答 JSON：{"bbox": [x, y, width, height]}，坐标为像素值相对整图，'
    '框要完整包住主体。若无主体图案回答 {"bbox": null}'
)
_UPLOAD_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/uploads"
_GENERATION_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)


class WatermarkPrecheck:
    """qwen-vl 语义预检（线程安全由连接池保证；无状态）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.wm_precheck_enabled and self._settings.wm_precheck_key)

    def has_watermark(self, image_bgr: np.ndarray) -> bool | None:
        """语义判定有无水印；True/False，任何失败返回 None（按无水印处理）。"""
        answer_text = self._ask_vl(image_bgr, _VL_PROMPT)
        if answer_text is None:
            return None
        normalized = answer_text.lower().replace(" ", "").replace("\n", "")
        if '"has_watermark":true' in normalized:
            return True
        if '"has_watermark":false' in normalized:
            return False
        return None

    def subject_bbox(self, image_bgr: np.ndarray) -> tuple[int, int, int, int] | None:
        """主体图案边界框：返回 (x, y, w, h)；无主体/失败 None。"""
        answer_text = self._ask_vl(image_bgr, _SUBJECT_BBOX_PROMPT)
        if answer_text is None:
            return None
        try:
            import json
            import re

            json_match = re.search(r"\{[^}]*\}", answer_text)
            if not json_match:
                return None
            bbox = json.loads(json_match.group(0)).get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                return None
            return int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        except (ValueError, TypeError):
            return None

    def _ask_vl(self, image_bgr: np.ndarray, prompt: str) -> str | None:
        """qwen-vl 视觉问答公共通道：上传图 → 同步问答 → 回答文本；失败 None。"""
        try:
            oss_url = self._upload_image(image_bgr)
            response = http_sync(self._http.post(
                _GENERATION_ENDPOINT,
                headers=self._auth_headers({"X-DashScope-OssResourceResolve": "enable"}),
                json={
                    "model": self._settings.wm_precheck_model,
                    "input": {
                        "messages": [
                            {"role": "user", "content": [
                                {"image": oss_url},
                                {"text": prompt},
                            ]}
                        ]
                    },
                },
            ))
            response.raise_for_status()
            content = response.json()["output"]["choices"][0]["message"]["content"]
            return next((str(item["text"]) for item in content if "text" in item), "")
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as vl_error:
            module_logger.warning("qwen vl ask failed: %s", vl_error)
            return None

    def _upload_image(self, image_bgr: np.ndarray) -> str:
        """本地图 → 百炼临时存储 → oss:// URL（48h；模型随配置 wm_precheck_model）。"""
        image_png = cv2.imencode(".png", image_bgr)[1].tobytes()
        policy_response = http_sync(self._http.get(
            _UPLOAD_ENDPOINT,
            params={"action": "getPolicy", "model": self._settings.wm_precheck_model},
            headers=self._auth_headers(),
        ))
        policy_response.raise_for_status()
        policy = policy_response.json()["data"]
        object_key = policy["upload_dir"] + f"/wm_pre_{int(time.time() * 1000)}.png"
        upload_response = http_sync(self._http.post(
            policy["upload_host"],
            data={
                "OSSAccessKeyId": policy["oss_access_key_id"],
                "Signature": policy["signature"],
                "policy": policy["policy"],
                "x-oss-object-acl": policy["x_oss_object_acl"],
                "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
                "key": object_key,
                "success_action_status": "200",
            },
            files={"file": ("image.png", image_png, "image/png")},
        ))
        upload_response.raise_for_status()
        return "oss://" + object_key

    def _auth_headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._settings.wm_precheck_key}"}
        if extra:
            headers.update(extra)
        return headers
