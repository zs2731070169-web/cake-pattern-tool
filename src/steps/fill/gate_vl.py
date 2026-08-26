"""qwen-vl 棋盘格背景判定客户端——填充步 v18.5 判定门（2026-08-26 用户定案）。

背景：本地像素判据（白度占比/最大色簇/熵）多轮实测效果差——米白误放、
卡通主体拉高熵、车照片误外呼；改 qwen-vl 视觉问答直接判"主体以外的背景
是否是棋盘格"（图片查看器表示透明的灰白相间方格）。语义对齐：真 alpha
素材被预览拍平截图、素材站假 alpha 导出都带棋盘格——"看到透明指示格"
= "这里本该透明" = 填白对象；白底/米白纯底/照片都不填。

标定（2026-08-26，qwen-vl-plus 4/4）：棋盘格底+主体→true；纯白底/米白
纯底/照片→false。VL 任何失败按 False（不填，零误伤）。
计费 ~¥0.003/次；判定结果随 fill 缓存键落盘免同图重复计费（调用侧）。

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

module_logger = logging.getLogger("pattern_tool.fill_gate_vl")

_UPLOAD_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/uploads"
_GENERATION_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)

_CHECKERBOARD_PROMPT = (
    "观察这张图片主体（图案/物体）以外的背景区域。判断背景是否是"
    '"棋盘格"样式（灰白相间的方格，即图片查看器表示透明的指示格）。'
    '只回答 JSON：{"checkerboard": true/false}'
)


class CheckerboardGate:
    """棋盘格背景判定（永不抛出：任何失败返回 False=不填，零误伤）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.wm_precheck_enabled and self._settings.wm_precheck_key)

    def has_checkerboard_background(self, image_bgr: np.ndarray) -> bool:
        """主体外背景是否棋盘格；失败/未配按 False（不填）。"""
        if not self.is_configured():
            module_logger.debug("checkerboard gate: not configured → False")
            return False
        answer = self._ask_vl(image_bgr, _CHECKERBOARD_PROMPT)
        if answer is None:
            return False
        normalized = answer.lower().replace(" ", "").replace("\n", "")
        if '"checkerboard":true' in normalized:
            module_logger.debug("checkerboard gate: true")
            return True
        module_logger.debug("checkerboard gate: false")
        return False

    # ---- VL 通道（与 watermark.precheck 同协议）----

    def _ask_vl(self, image_bgr: np.ndarray, prompt: str) -> str | None:
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
            module_logger.warning("checkerboard gate vl failed: %s", vl_error)
            return None

    def _upload_image(self, image_bgr: np.ndarray) -> str:
        """本地图 → 百炼临时存储 → oss:// URL（48h；模型随 wm_precheck_model）。"""
        image_png = cv2.imencode(".png", image_bgr)[1].tobytes()
        policy_response = http_sync(self._http.get(
            _UPLOAD_ENDPOINT,
            params={"action": "getPolicy", "model": self._settings.wm_precheck_model},
            headers=self._auth_headers(),
        ))
        policy_response.raise_for_status()
        policy = policy_response.json()["data"]
        object_key = policy["upload_dir"] + f"/fill_gate_{int(time.time() * 1000)}.png"
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
