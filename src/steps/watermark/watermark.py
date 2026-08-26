"""去水印步：qwen-vl 检测 + 佐糖 PicWish 修复（技术方案 4.4 / 5.2 图 B）。

- 检测：qwen-vl-plus 语义预检（唯一检测器，~¥0.003/次；OpenCV 检测
  2026-08-25 退役——实测 2/4 错且两方向都错）判有才修；
- 修复：佐糖 PicWish 高级版全屏自动去水印（2026-08-26 回退恢复主力——
  当日曾换 qwen-image-2.0 生成式，多图实测整体效果不理想，用户定案回退；
  10 算粒/次，同步等待至完成 110s 上限）；缓存优先（原始域键，同图免计费）；
- 修复失败/未配 → 原图 + heavy-watermark 提示（零误伤）。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings
from src.steps.imaging import ensure_bgra, flatten_to_white
from src.steps.watermark.cache import WatermarkResultCache
from src.steps.watermark.precheck import WatermarkPrecheck
from src.steps.watermark.picwish import PicwishWatermarkRemover

module_logger = logging.getLogger("pattern_tool.watermark")


class WatermarkStepResult:
    """去水印步的输出（管线据此回写 stage 与 quality_hint）。"""

    def __init__(
        self,
        image_bgr: np.ndarray,
        stage_value: str,
        quality_hint: str = "none",
        is_heavy_watermark: bool = False,
    ) -> None:
        self.image_bgr = image_bgr  # 修复后的图（skipped 时为原图）
        self.stage_value = stage_value  # done / done(api) / done(degraded) / skipped
        self.quality_hint = quality_hint  # none / heavy-watermark
        self.is_heavy_watermark = is_heavy_watermark  # 复杂档标记（多层或压主体）


# （2026-08-25 退役存档）OpenCV 检测源（RapidOCR 文字框 ∪ 频域邻域差分
# 小块的 detect_watermark_mask 与 OCR 引擎缓存）已删除——实测判定 2/4 错且
# 两方向都错（无水印图差分误触发、浅水印漏检），qwen-vl 语义预检 4/4 全对
# 成为唯一检测器（4.4 v5）；佐糖退役（2026-08-26）后连"平台水印走二级"
# 的分流职能也一并消失。git 历史可溯。


class WatermarkStep:
    """去水印步（管线第二阶段，见 pipeline.py 编排）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._picwish = PicwishWatermarkRemover(settings)
        self._cache = WatermarkResultCache(settings)
        self._precheck = WatermarkPrecheck(settings)

    def run(
        self,
        image_ndarray: np.ndarray,
        original_bgr: np.ndarray | None = None,
        crop_meta_json: str | None = None,
    ) -> WatermarkStepResult:
        """去水印（4.4 v2，2026-08-26 换装）；算法异常由管线捕获转 failed。

        检测：qwen-vl 语义预检（唯一检测器，~¥0.003/次）判有才修。
        修复降级链：缓存（原始域键）→ **佐糖 PicWish 高级版（2026-08-26
        回退恢复——qwen-image 换装当日实测整体效果不理想）** → 原图 +
        heavy-watermark（零误伤）。
        预检未配/失败/判无 → skipped 原图（不盲修，零误伤）。
        """
        image_bgra = ensure_bgra(image_ndarray)
        # 工作域 = 原始域优先（缓存键稳定）；无原始域回退裁剪版域
        working_bgr = original_bgr if original_bgr is not None else flatten_to_white(image_bgra)
        # 检测在白底合成副本上做（透明残值不进模型）
        analysis_bgr = flatten_to_white(ensure_bgra(working_bgr))

        # ---- qwen-vl 为唯一检测器（4.4 v5，2026-08-25）----
        module_logger.debug("precheck configured=%s", self._precheck.is_configured())
        if not self._precheck.is_configured():
            return WatermarkStepResult(image_ndarray, "skipped")
        precheck_verdict = self._precheck.has_watermark(analysis_bgr)
        module_logger.debug("precheck verdict=%s", precheck_verdict)
        is_heavy = False  # heavy 判定依赖 OpenCV mask（已退役），语义档无需此标记

        if precheck_verdict is not True:
            module_logger.debug("no watermark signal → skipped")
            return WatermarkStepResult(image_ndarray, "skipped")

        # ---- 修复：缓存优先（原始域键），主力 qwen-image，佐糖兜底 ----
        cached = self._cache.get(analysis_bgr)
        module_logger.debug("cache hit=%s", cached is not None)
        if cached is not None:
            repaired_cropped = self._map_to_cropped(cached, image_bgra, crop_meta_json)
            return self._merge_repaired_result(image_bgra, repaired_cropped, "done(api)", "none", is_heavy)

        repaired = None
        # 修复：佐糖 PicWish 高级版（2026-08-26 回退恢复主力）
        module_logger.debug("picwish configured=%s", self._picwish.is_configured())
        if self._picwish.is_configured():
            repaired = self._picwish.remove_watermark(analysis_bgr)
            module_logger.debug("picwish result=%s", "ok" if repaired is not None else "failed")

        if repaired is not None:
            self._cache.put(analysis_bgr, repaired)
            repaired_cropped = self._map_to_cropped(repaired, image_bgra, crop_meta_json)
            return self._merge_repaired_result(image_bgra, repaired_cropped, "done(api)", "none", is_heavy)

        # 修复失败：原样零误伤（检出水印时仍提示客户）
        return WatermarkStepResult(image_ndarray, "skipped", "heavy-watermark", is_heavy)

    @staticmethod
    def _map_to_cropped(
        repaired_original_bgr: np.ndarray,
        image_bgra: np.ndarray,
        crop_meta_json: str | None,
    ) -> np.ndarray:
        """原始域修复结果 → 裁剪版色域（按 crop 偏移取窗口；无 crop 原样返回）。

        crop_meta.data = {x, y, width, height}（前端 cropper 框，相对原图）；
        偏移非法/越界时回退整图缩放（不丢修复效果）。
        """
        if not crop_meta_json:
            return repaired_original_bgr
        try:
            import json

            crop_data = json.loads(crop_meta_json).get("data") or {}
            offset_x, offset_y = int(crop_data.get("x", 0)), int(crop_data.get("y", 0))
            crop_w, crop_h = int(crop_data.get("width", 0)), int(crop_data.get("height", 0))
        except (ValueError, TypeError):
            return repaired_original_bgr
        target_h, target_w = image_bgra.shape[:2]
        src_h, src_w = repaired_original_bgr.shape[:2]
        if crop_w <= 0 or crop_h <= 0 or offset_x < 0 or offset_y < 0:
            return repaired_original_bgr
        if offset_x + crop_w > src_w or offset_y + crop_h > src_h:
            # 越界（原始域与 crop 声明不匹配）：整图缩放回退
            return cv2.resize(repaired_original_bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)
        window = repaired_original_bgr[offset_y:offset_y + crop_h, offset_x:offset_x + crop_w]
        if window.shape[0] != target_h or window.shape[1] != target_w:
            window = cv2.resize(window, (target_w, target_h), interpolation=cv2.INTER_AREA)
        return window

    @staticmethod
    def _merge_repaired_result(
        image_bgra: np.ndarray,
        repaired_bgr: np.ndarray,
        stage_value: str,
        hint: str,
        is_heavy: bool,
    ) -> WatermarkStepResult:
        """修复结果（3 通道 BGR）与本地透明通道合并：颜色取修复图，alpha 取本地。

        佐糖返回的是不透明 JPG——透明=打印不印的语义由本地 alpha 保住。
        尺寸不一致时以区域缩放回原幅。
        """
        if repaired_bgr.shape[:2] != image_bgra.shape[:2]:
            repaired_bgr = cv2.resize(repaired_bgr, (image_bgra.shape[1], image_bgra.shape[0]), interpolation=cv2.INTER_AREA)
        merged = image_bgra.copy()
        merged[:, :, :3] = repaired_bgr
        return WatermarkStepResult(merged, stage_value, hint, is_heavy)
