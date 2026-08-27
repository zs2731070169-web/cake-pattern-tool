"""尺寸缩放步：按用户声明的打印尺寸（cm）缩放到 @300DPI 目标像素（2026-08-26 新增）。

置于管线最末（描边后）——线宽像素随缩放等比、打印物理毫米恒定
（描边线宽 = mm×DPI/25.4 在原幅算好，缩放后物理尺寸不变）。

策略：
- 未选尺寸 → skipped 原幅（默认，零回归）；
- 短边 ≥ 目标 → INTER_AREA 等比缩小（纯本地毫秒级）；
- 短边 < 目标 → 佐糖 scale-pro 高级变清晰（2026-08-28 第十八次修订：
  石榴历版目检不符，主力回佐糖；sync=0 异步提交-轮询-下载，服务端定倍，
  出幅对齐目标缩回/补尾程）；失败/未配置 → 记 failed 不交付（第十七次
  修订定案"失败了就失败了"：插值废图不是交付）。

尺寸口径（cm，用户提供的档位表）：4寸=9 / 6寸=14 / 8寸=19 / 10寸=24 /
12寸=29，及自定义 5–33cm（上限=打印机 iX6880 A3+ 无边距 329mm）；
目标短边 = cm/2.54×300。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.resize")

# 自定义尺寸合法区间（cm）：拦手滑输 0.1 或极端值
SIZE_CM_MIN = 5.0
# 上限 33cm = 打印机佳能 iX6880 无边距最大宽度 329mm（A3+ 13"，
# 官方规格；2026-08-27 定标，原 100 拍脑袋值超出设备物理能力）
SIZE_CM_MAX = 33.0


class ResizeStepResult:
    """尺寸缩放步的输出。"""

    def __init__(self, image_bgr: np.ndarray, stage_value: str, quality_hint: str = "none") -> None:
        self.image_bgr = image_bgr  # 缩放后的图（skipped/降级时可能为原图）
        self.stage_value = stage_value  # done / skipped / failed（interpolated 已退役 2026-08-28）
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
        module_logger.info(
            "resize start: %.0fcm@%dDPI 短边=%d→%d（%s）",
            float(size_cm), self._settings.print_dpi, short_side, target,
            "放大" if short_side < target else "缩小",
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

        # 放大（2026-08-28 第十八次修订——佐糖 scale-pro 高级变清晰主力）。
        # 失败记档口径（第十七次修订定案"失败了就失败了"）：任一环失败记 failed——
        # 前端红显"执行失败"不交付（插值兜底已删：9.5 倍拉伸锐度 ~3 是废图，
        # 交付废图不是保交付；失败提示倒逼上游图源质量）。
        upscale_outcome = self._try_super_resolution(image_bgra, target)
        if upscale_outcome is None:
            module_logger.warning(
                "resize: 超分失败（欠费/超时）→ 记 failed 不交付插值废图"
            )
            return ResizeStepResult(image_bgra, "failed")
        upscaled_bgra, tail_scale = upscale_outcome
        # 尾程提示（2026-08-28 用户定案：维持 3425 目标接受尾程，但客户可见建议）：
        # 佐糖出幅 < 目标（服务端定倍封顶）时 LANCZOS 补尾程稀释细节——
        # 尾程放大超 15% 的图提示"建议提供更大尺寸原图"（不阻塞交付）。
        quality_hint = "suggest-larger-source" if tail_scale > 1.15 else "none"
        scale = target / min(upscaled_bgra.shape[:2])
        resized = upscaled_bgra if scale == 1 else cv2.resize(
            upscaled_bgra,
            (round(upscaled_bgra.shape[1] * scale), round(upscaled_bgra.shape[0] * scale)),
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
        return ResizeStepResult(resized, "done", quality_hint=quality_hint)

    def _try_super_resolution(
        self, image_bgra: np.ndarray, target_short_side: int
    ) -> tuple[np.ndarray, float] | None:
        """佐糖 scale-pro 高级变清晰（第十八次修订主力；失败返 None 记 failed）。

        返回 (对齐目标后的 BGRA 图, 尾程放大倍率)——尾程倍率 >1 表示佐糖
        出幅不足目标需 LANCZOS 补（提示口径见调用侧）。

        协议：异步 sync=0 提交 → 轮询 state==1 → 下载 OSS URL；服务端决定
        放大倍率。出幅 ≥ 目标 → INTER_AREA 无损缩到目标；出幅 < 目标 →
        LANCZOS 补尾程到目标（佐糖服务端定倍，尾程是协议结构不是缺陷）。
        缓存（第十二次修订沿袭）：键=图内容+目标宽——同图同档二次提交零算粒。
        """
        try:
            from src.steps.resize.cache import ScaleResultCache
            from src.steps.resize.picwish_scale import PicwishScalePro

            scale_client = PicwishScalePro(self._settings)
            if not scale_client.is_configured():
                module_logger.info("resize: 佐糖超分未配置 → 记 failed")
                return None
            target_width = (
                target_short_side if image_bgra.shape[1] <= image_bgra.shape[0]
                else round(image_bgra.shape[1] * target_short_side / image_bgra.shape[0])
            )
            bgr = image_bgra[:, :, :3]
            cache = ScaleResultCache(self._settings)
            cached = cache.get(bgr, target_width)
            if cached is not None:
                module_logger.info("resize: 超分缓存命中 width=%d（零外呼零算粒）", target_width)
                return self._reattach_alpha(cached, image_bgra), 1.0  # 缓存即目标幅，零尾程

            module_logger.info(
                "resize: 佐糖 scale-pro 外呼中（sync=0 异步）… 源=%dx%d → 目标宽=%d",
                image_bgra.shape[1], image_bgra.shape[0], target_width,
            )
            result = scale_client.upscale(bgr)
            if result is None:
                return None  # 调用方记 failed（第十七次修订：不交付插值废图）
            module_logger.info(
                "resize: 佐糖超分成功 %dx%d → %dx%d（服务端定倍）",
                image_bgra.shape[1], image_bgra.shape[0], result.shape[1], result.shape[0],
            )
            # 出幅对齐目标：服务端倍率不定 → 出幅 ≥ 目标缩回（无损）、不足补尾程
            tail_scale = target_width / result.shape[1]
            if tail_scale > 1.0:
                module_logger.info(
                    "resize: 佐糖出幅 %d < 目标 %d → 尾程 LANCZOS ×%.2f（提示建议换大图）",
                    result.shape[1], target_width, tail_scale,
                )
            if result.shape[1] != target_width:
                ratio = target_width / result.shape[1]
                result = cv2.resize(
                    result,
                    (target_width, round(result.shape[0] * ratio)),
                    interpolation=cv2.INTER_AREA if ratio < 1 else cv2.INTER_LANCZOS4,
                )
            cache.put(bgr, target_width, result)
            return self._reattach_alpha(result, image_bgra), max(1.0, tail_scale)
        except Exception as super_error:  # 超分异常：与失败同口径记 failed
            module_logger.warning("resize: super resolution error: %s", super_error)
            return None

    @staticmethod
    def _reattach_alpha(upscaled_bgr: np.ndarray, image_bgra: np.ndarray) -> np.ndarray:
        """3 通道超分结果贴回原 alpha（等比重采样到超分幅）。"""
        alpha_resized = cv2.resize(
            image_bgra[:, :, 3],
            (upscaled_bgr.shape[1], upscaled_bgr.shape[0]),
            interpolation=cv2.INTER_LANCZOS4,
        )
        result_bgra = cv2.cvtColor(upscaled_bgr, cv2.COLOR_BGR2BGRA)
        result_bgra[:, :, 3] = alpha_resized
        return result_bgra
