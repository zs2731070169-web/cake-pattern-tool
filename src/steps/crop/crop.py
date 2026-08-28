"""裁剪步：按前端声明（crop_meta）在运行时执行真正的裁剪（2026-08-26 独立 step）。

定案背景：裁剪曾由前端像素级完成（getCroppedCanvas+形状遮罩导出）——同一张
原图切换形状时，前端送出的裁剪结果图是不同像素数组，填充/去水印缓存键随
之不同，同图不同形状重复外呼模型（2026-08-26 用户实测 4 次提交 3 次外呼）。
改为：**前端只声明（shape + 框参数），后端运行时裁剪**。管线全程在原始域
处理（判定/外呼/缓存同键），本步在填充之后、描边之前执行——先按框裁出
外接框区域，再按形状掩膜塑形（形状外 alpha=0）。

形状几何复用 outline.crop_shape_region_mask（与前端 buildShapePath 同源
公式，所见即所得）；矩形类形状=裁框本身（无掩膜操作）。
"""

from __future__ import annotations

import logging

import numpy as np

module_logger = logging.getLogger("pattern_tool.crop")


class CropStepResult:
    """裁剪步的输出。"""

    def __init__(self, image_bgr: np.ndarray, stage_value: str) -> None:
        self.image_bgr = image_bgr  # 裁剪塑形后的图（skipped 时为原图）
        self.stage_value = stage_value  # done / skipped


class CropStep:
    """裁剪步（管线第四阶段填充与描边之间）——声明式裁剪的运行时执行者。"""

    def run(self, image_ndarray: np.ndarray, crop_meta: dict | None = None) -> CropStepResult:
        """按 crop_meta 执行裁剪：data 框裁外接区 → shape 掩膜塑形。

        crop_meta = {shape, data: {x, y, width, height}, frame}（前端 cropper 框，
        相对原图像素坐标）。**无 data 框但有合法 shape（默认 circle 声明的常态，
        2026-08-26 14:49 翻车修复：旧口径 skipped 致生成图整幅白底交付、形状外
        不透明）→ 按整图默认框处理（x=0,y=0,w,h=图幅）**——"每图必有形状"
        口径的兑现：非矩形形状一律塑形（形状外 alpha=0，PNG 真 transparent），
        矩形类=全图无操作。完全无 meta / 未知 shape → skipped 原图。
        3 通道输入返回 3 通道（管线口径惯例）。
        """
        from src.steps.imaging import ensure_bgra
        from src.steps.outline import KNOWN_CROP_SHAPES, crop_shape_region_mask

        if not isinstance(crop_meta, dict):
            module_logger.debug("crop: no meta → passthrough")
            return CropStepResult(image_ndarray, "skipped")
        shape_value = crop_meta.get("shape")
        box = crop_meta.get("data") or {}
        frame = crop_meta.get("frame") or {}
        module_logger.info("crop start: shape=%s box=%s", shape_value, "有" if box else "无")
        if shape_value not in KNOWN_CROP_SHAPES:
            module_logger.info("crop → skipped (未知形状 %s)", shape_value)
            return CropStepResult(image_ndarray, "skipped")
        image_bgra = ensure_bgra(image_ndarray)
        if not self._box_valid(box):
            # 无框合法声明（批级统一形状的常态，第二十九次修订）：整图即框
            # ——直接形状塑形不裁切（无外接框语义），形状外 alpha=0（羽化
            # 抗锯齿口径）。塑形集合 = 全部非 rectangle 形状（square/
            # rectangle-fixed 居中内接几何在 crop_shape_region_mask 内，
            # 2026-08-28 修复"正方形/长方形设置不到图案上"）；rectangle/
            # free = 整图直通（默认值零行为变化）
            if shape_value != "rectangle" and shape_value != "free":
                module_logger.info(
                    "crop → done: shape=%s 无框整图塑形 %dx%d",
                    shape_value, image_bgra.shape[1], image_bgra.shape[0],
                )
                image_bgra = self._apply_shape_mask(image_bgra, shape_value)
            else:
                module_logger.info("crop → done: shape=%s 矩形无框=整图直通", shape_value)
            return CropStepResult(image_bgra, "done")

        image_bgra = ensure_bgra(image_ndarray)
        image_height, image_width = image_bgra.shape[:2]
        # 框坐标定义在**原图域**（data 相对原图像素，frame=原图尺寸）；管线
        # 图幅可能与原图不同（生成式交付 512 幅）——按比例映射到当前幅，
        # 框语义 = 原图上的区域选择，随幅等比缩放。frame 缺省（旧声明）时
        # 按框值即当前幅坐标处理（框≈全图的常见情形无误差）。
        source_w = int(frame.get("width") or 0)
        source_h = int(frame.get("height") or 0)
        if source_w > 0 and source_h > 0 and (
            source_w != image_width or source_h != image_height
        ):
            scale_x, scale_y = image_width / source_w, image_height / source_h
            x = int(box["x"] * scale_x)
            y = int(box["y"] * scale_y)
            crop_w = int(box["width"] * scale_x)
            crop_h = int(box["height"] * scale_y)
        else:
            x, y = int(box["x"]), int(box["y"])
            crop_w, crop_h = int(box["width"]), int(box["height"])
        x, y = max(0, x), max(0, y)
        crop_w = min(crop_w, image_width - x)
        crop_h = min(crop_h, image_height - y)
        if crop_w <= 0 or crop_h <= 0:
            module_logger.info("crop → skipped (框越界 %s)", shape_value)
            return CropStepResult(image_ndarray, "skipped")

        result = image_bgra[y : y + crop_h, x : x + crop_w].copy()
        module_logger.info(
            "crop → done: shape=%s 框=(%d,%d %dx%d) %dx%d→%dx%d",
            shape_value, x, y, crop_w, crop_h, source_w or image_width, source_h or image_height, crop_w, crop_h,
        )
        if shape_value not in ("rectangle", "free"):
            # 带框路径的塑形集合同步扩（第二十九次修订）：square/rectangle-fixed
            # 带框时按框内居中内接塑形（框=外接区，形状掩膜在框幅内最大化）
            result = self._apply_shape_mask(result, shape_value)
        if image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3:
            return CropStepResult(result[:, :, :3], "done")
        return CropStepResult(result, "done")

    @staticmethod
    def _apply_shape_mask(image_bgra: np.ndarray, shape_value: str) -> np.ndarray:
        """形状掩膜塑形（第二十七次修订：自 resize 移入——缩放提前后形状
        塑形全部由本步在高幅完成）。带 sigma 0.8 羽化抗锯齿（与旧 resize
        重画口径一致）：光栅圆弧是内接多边形近似，二值边在曲线斜段呈 1px
        台阶，羽化把台阶过渡成亚像素渐变（打印视觉平滑）。"""
        import cv2

        from src.steps.outline import crop_shape_region_mask

        shape_mask = crop_shape_region_mask(shape_value, image_bgra.shape[1], image_bgra.shape[0])
        soft = cv2.GaussianBlur((shape_mask.astype(np.uint8) * 255), (0, 0), sigmaX=0.8)
        image_bgra[:, :, 3] = np.where(
            shape_mask, np.maximum(soft, 128), np.minimum(soft, 127)
        )
        return image_bgra

    @staticmethod
    def _box_valid(box: dict) -> bool:
        """框参数完整性（x/y/width/height 均为正数坐标）。"""
        try:
            return (
                int(box["x"]) >= 0
                and int(box["y"]) >= 0
                and int(box["width"]) > 0
                and int(box["height"]) > 0
            )
        except (KeyError, TypeError, ValueError):
            return False
