"""生成式换白底结果缓存（填充步 v16，镜像 watermark_cache 模式）。

键 = 送判定的分析图（白底合成副本）内容 SHA-256；值 = qwen-image-2.0
返回并已过双验证门的原幅生成图 PNG。只存验证通过的结果（失败/被拒不
缓存——重提交有机会重试）；形状合成在调用侧按当前 crop_shape 做，
同图不同裁剪形状共享缓存。缓存文件随 TTLCleaner 同周期清理（24h 口径）。
"""

from __future__ import annotations

import hashlib
import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.fill_gen_cache")


class FillGenResultCache:
    """内容哈希键的文件缓存（进程无锁：文件系统原子写，撞键=同图无害）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def _cache_path(self, cache_key: str):
        from pathlib import Path

        cache_dir = Path(self._settings.resolve_data_dir()) / "cache" / "fill_gen" / cache_key[:2]
        return cache_dir / f"{cache_key}.png"

    def get(self, analysis_bgr: np.ndarray) -> np.ndarray | None:
        """命中返回缓存的生成图（BGR，生成幅——低清输入时大于原幅），未命中 None。

        不做幅校验（2026-08-25 22:35：缓存改存生成幅，幅可大于分析图幅；
        幅防御在调用侧 _apply_pure_white_on_generated / 验证缩回处）。
        """
        cache_path = self._cache_path(self.key_of(analysis_bgr))
        if not cache_path.exists():
            return None
        cached = cv2.imread(str(cache_path), cv2.IMREAD_COLOR)
        if cached is None:
            return None  # 损坏按未命中
        return cached

    def put(self, analysis_bgr: np.ndarray, generated_bgr: np.ndarray) -> None:
        """写入缓存（原子写：内存编码+临时文件+rename，2026-08-28 第二十六次
        修订——并发读者永不见半张 PNG；写失败只记日志不影响主流程）。"""
        from src.steps.imaging import atomic_write_png

        cache_path = self._cache_path(self.key_of(analysis_bgr))
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_png(cache_path, generated_bgr)
        except OSError as cache_error:
            module_logger.warning("fill_gen cache write failed: %s", cache_error)

    @staticmethod
    def key_of(analysis_bgr: np.ndarray) -> str:
        """分析图内容 SHA-256（送 API 的同一字节流决定同一键）。"""
        return hashlib.sha256(np.ascontiguousarray(analysis_bgr).tobytes()).hexdigest()
