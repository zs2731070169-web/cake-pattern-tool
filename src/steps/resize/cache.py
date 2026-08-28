"""超分结果缓存（第十二次修订，镜像 watermark/fill cache 模式）。

键 = 送 API 的 BGR 内容 SHA-256 **+ 目标宽**（`{sha256}_{width}.png`）；
值 = 3 通道超分结果 PNG。与去水印/填充缓存的差异点：超分同一张图选
不同打印档是**不同外呼**（width 入参不同、结果不同），键必须含目标
尺寸——"同图同档"命中共享、换档新键不串档。
只存颜色（超分上传即 3 通道 BGR），alpha 由调用侧贴回；失败/插值降级
结果不缓存（重提交有机会重试真超分）。缓存文件随 TTLCleaner 同周期
清理（24h 口径）。
"""

from __future__ import annotations

import hashlib
import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.scale_cache")


class ScaleResultCache:
    """内容哈希+目标宽键的文件缓存（进程无锁：文件系统原子写，撞键=同图同档无害）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def _cache_path(self, cache_key: str) -> "any":
        from pathlib import Path

        cache_dir = self._settings.resolve_data_dir() / "cache" / "scale" / cache_key[:2]
        return cache_dir / f"{cache_key}.png"

    def get(self, image_bgr: np.ndarray, target_width: int) -> np.ndarray | None:
        """命中返回缓存的超分图（BGR，宽=target_width），未命中返回 None。"""
        cache_path = self._cache_path(self.key_of(image_bgr, target_width))
        if not cache_path.exists():
            return None
        cached = cv2.imread(str(cache_path), cv2.IMREAD_COLOR)
        if cached is None or cached.shape[1] != target_width:
            return None  # 损坏/宽不符（理论不发生）按未命中
        return cached

    def put(self, image_bgr: np.ndarray, target_width: int, upscaled_bgr: np.ndarray) -> None:
        """写入缓存（原子写：内存编码+临时文件+rename，2026-08-28 第二十六次
        修订——并发读者永不见半张 PNG；写失败只记日志不影响主流程）。"""
        from src.steps.imaging import atomic_write_png

        cache_path = self._cache_path(self.key_of(image_bgr, target_width))
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_png(cache_path, upscaled_bgr)
        except OSError as cache_error:
            module_logger.warning("scale cache write failed: %s", cache_error)

    @staticmethod
    def key_of(image_bgr: np.ndarray, target_width: int) -> str:
        """图内容 SHA-256 + 目标宽（送 API 的同一字节流与 width 决定同一键）。"""
        digest = hashlib.sha256(np.ascontiguousarray(image_bgr).tobytes()).hexdigest()
        return f"{digest}_{int(target_width)}"
