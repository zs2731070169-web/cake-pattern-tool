"""去水印 API 结果缓存（技术方案 4.4 / 2.2 数据归属）。

键 = 送入 API 的分析图（白底合成副本）内容 SHA-256；值 = 佐糖返回的
3 通道颜色图 PNG。命中直接用——不重复调用 API 不重复计费；缓存只存颜色，
透明通道由管线按原图 alpha 重新合并（同一图的裁剪版与原版各自成键）。
缓存文件随 TTLCleaner 同周期清理（24h 口径）。
"""

from __future__ import annotations

import hashlib
import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.watermark_cache")


class WatermarkResultCache:
    """内容哈希键的文件缓存（进程无锁：文件系统原子写，撞键=同图无害）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def _cache_path(self, cache_key: str) -> "any":
        from pathlib import Path

        cache_dir = self._settings.resolve_data_dir() / "cache" / "watermark" / cache_key[:2]
        return cache_dir / f"{cache_key}.png"

    def get(self, analysis_bgr: np.ndarray) -> np.ndarray | None:
        """命中返回缓存的颜色图（BGR），未命中返回 None。"""
        cache_key = self.key_of(analysis_bgr)
        cache_path = self._cache_path(cache_key)
        if not cache_path.exists():
            return None
        cached = cv2.imread(str(cache_path), cv2.IMREAD_COLOR)
        if cached is None or cached.shape[:2] != analysis_bgr.shape[:2]:
            return None  # 损坏/幅不符（理论不发生）按未命中
        return cached

    def put(self, analysis_bgr: np.ndarray, repaired_bgr: np.ndarray) -> None:
        """写入缓存（原子写：内存编码+临时文件+rename，2026-08-28 第二十六次
        修订——并发读者永不见半张 PNG；写失败只记日志不影响主流程）。"""
        from src.steps.imaging import atomic_write_png

        cache_path = self._cache_path(self.key_of(analysis_bgr))
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_png(cache_path, repaired_bgr)
        except OSError as cache_error:
            module_logger.warning("watermark cache write failed: %s", cache_error)

    @staticmethod
    def key_of(analysis_bgr: np.ndarray) -> str:
        """分析图内容 SHA-256（送 API 的同一字节流决定同一键）。"""
        return hashlib.sha256(np.ascontiguousarray(analysis_bgr).tobytes()).hexdigest()
