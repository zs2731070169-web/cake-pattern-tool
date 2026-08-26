"""尺寸缩放步：按用户声明的打印尺寸（cm）缩放到 @300DPI 目标像素（2026-08-26 新增）。

置于管线最末（描边后）——线宽像素随缩放等比、打印物理毫米恒定
（描边线宽 = mm×DPI/25.4 在原幅算好，缩放后物理尺寸不变）。

策略：
- 未选尺寸 → skipped 原幅（默认，零回归）；
- 短边 ≥ 目标 → INTER_AREA 等比缩小（纯本地毫秒级）；
- 短边 < 目标 → 优先佐糖 scale-pro 超分（2026-08-27 恢复），仍不足目标再
  Lanczos 补尾程；佐糖失败/未配置 → Lanczos 插值兜底（low-res 提示已撤
  2026-08-27——用户实测超分效果达标，插值兜底仅保交付不断，不再提示）。

尺寸口径（cm，用户提供的档位表）：4寸=9 / 6寸=14 / 8寸=19 / 10寸=24 /
12寸=29，及自定义 5–100cm；目标短边 = cm/2.54×300。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.resize")

# 自定义尺寸合法区间（cm）：拦手滑输 0.1 或 500 的极端值
SIZE_CM_MIN = 5.0
SIZE_CM_MAX = 100.0


class ResizeStepResult:
    """尺寸缩放步的输出。"""

    def __init__(self, image_bgr: np.ndarray, stage_value: str, quality_hint: str = "none") -> None:
        self.image_bgr = image_bgr  # 缩放后的图（skipped/降级时可能为原图）
        self.stage_value = stage_value  # done / done(upscaled) / skipped
        self.quality_hint = quality_hint  # none（low-res 已撤 2026-08-27）


def target_short_side_pixels(cm: float, dpi: int) -> int:
    """cm → 目标短边像素（@DPI）：px = cm/2.54×dpi。"""
    return max(1, round(cm / 2.54 * dpi))


class ResizeStep:
    """尺寸缩放步（管线第五阶段，描边后）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def run(
        self, image_ndarray: np.ndarray, size_cm: float | None, shape_value: str | None = None
    ) -> ResizeStepResult:
        """按声明的 cm 缩放到目标短边；未声明/非法 → skipped 原幅。

        shape_value 给定时，放大路径在高分辨率域**重画形状掩膜**替换插值
        alpha——形状几何是解析式（圆/心/星），任意分辨率重画无损；低幅
        光栅掩膜放大 7 倍会把像素台阶拉成 ~7px 可见锯齿（2026-08-26 13:44
        放大查看翻车：alpha 渐变只柔化台阶边缘，切向折线感仍在）。
        """
        from src.steps.imaging import ensure_bgra

        if size_cm is None or not (SIZE_CM_MIN <= float(size_cm) <= SIZE_CM_MAX):
            module_logger.debug("resize: no valid size (%s) → skipped", size_cm)
            return ResizeStepResult(image_ndarray, "skipped")

        image_bgra = ensure_bgra(image_ndarray)
        short_side = min(image_bgra.shape[:2])
        target = target_short_side_pixels(float(size_cm), self._settings.print_dpi)
        module_logger.debug(
            "resize: short=%d target=%d (%.0fcm@%dDPI)",
            short_side, target, float(size_cm), self._settings.print_dpi,
        )

        if short_side == target:
            return ResizeStepResult(image_ndarray, "skipped")
        if short_side > target:
            # 缩小：纯本地等比
            scale = target / short_side
            resized = cv2.resize(
                image_bgra,
                (round(image_bgra.shape[1] * scale), round(image_bgra.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )
            module_logger.debug("resize: downscale → %dx%d", resized.shape[1], resized.shape[0])
            return ResizeStepResult(resized, "done")

        # 放大（2026-08-27 恢复超分——2026-08-26 曾退役："超分改主体"与
        # "主体不可变"红线冲突；但"显式选大尺寸"场景下小源图大倍率纯插值
        # 糊到不可用是更硬的痛点（299px 选 12 寸=19 倍），用户实测效果达标）：
        # 优先佐糖 scale-pro 超分，结果仍小于目标再 Lanczos 补尾程；佐糖
        # 失败/未配置降级纯插值保交付（不再提示 low-res——2026-08-27 撤）。
        upscaled_bgra = self._try_super_resolution(image_bgra)
        base_for_further = upscaled_bgra if upscaled_bgra is not None else image_bgra
        scale = target / min(base_for_further.shape[:2])
        resized = base_for_further if scale == 1 else cv2.resize(
            base_for_further,
            (round(base_for_further.shape[1] * scale), round(base_for_further.shape[0] * scale)),
            interpolation=cv2.INTER_LANCZOS4,
        )
        if shape_value in ("circle", "heart", "star"):
            # 高分辨率域重画形状掩膜：解析几何任意分辨率无损，消除低幅
            # 光栅台阶放大后的切向锯齿
            from src.steps.outline import crop_shape_region_mask

            shape_mask = crop_shape_region_mask(shape_value, resized.shape[1], resized.shape[0])
            # 边缘 1px 抗锯齿：掩膜用高斯核轻度羽化后重阈值到 [0,255] 渐变带
            # ——光栅圆弧是内接多边形近似，二值边在曲线斜段呈 1px 台阶，
            # 羽化把台阶边过渡成亚像素渐变（打印视觉平滑）
            soft = cv2.GaussianBlur(
                (shape_mask.astype(np.uint8) * 255), (0, 0), sigmaX=0.8
            )
            resized[:, :, 3] = np.where(shape_mask, np.maximum(soft, 128), np.minimum(soft, 127))
            module_logger.debug("resize: shape mask repainted at %dx%d", resized.shape[1], resized.shape[0])
        module_logger.debug("resize: upscale → %dx%d", resized.shape[1], resized.shape[0])
        return ResizeStepResult(resized, "done", quality_hint="none")

    def _try_super_resolution(self, image_bgra: np.ndarray) -> np.ndarray | None:
        """佐糖 scale-pro 超分（可用才调；失败/未配置返回 None 走插值降级）。

        注意：超分客户端按 3 通道 BGR 上传（PNG 无损），返回 3 通道——
        alpha 通道由本步末尾的形状掩膜重画恢复（shape 路径）或保真
        （无形状路径不透明区恒全图）。
        """
        try:
            from src.steps.resize.picwish_scale import PicwishScalePro

            scale_client = PicwishScalePro(self._settings)
            if not scale_client.is_configured():
                module_logger.debug("resize: scale-pro not configured → interpolation")
                return None
            result = scale_client.upscale(image_bgra[:, :, :3])
            if result is None:
                module_logger.warning("resize: scale-pro failed → interpolation fallback")
                return None
            module_logger.debug(
                "resize: scale-pro %dx%d → %dx%d",
                image_bgra.shape[1], image_bgra.shape[0], result.shape[1], result.shape[0],
            )
            # 原图透明域带回复：超分上传只含 BGR（3 通道），透明信息在服务端
            # 不可得——把原 alpha 等比重采样到超分幅贴回（形状掩膜随后重画
            # 覆盖有形状路径；无形状路径矩形类以此保住透明区语义）
            alpha_resized = cv2.resize(
                image_bgra[:, :, 3],
                (result.shape[1], result.shape[0]),
                interpolation=cv2.INTER_LANCZOS4,
            )
            result_bgra = cv2.cvtColor(result, cv2.COLOR_BGR2BGRA)
            result_bgra[:, :, 3] = alpha_resized
            return result_bgra
        except Exception as super_error:  # 超分异常不阻塞交付（插值兜底）
            module_logger.warning("resize: super resolution error → interpolation: %s", super_error)
            return None
