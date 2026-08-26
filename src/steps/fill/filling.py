"""填充步：背景换白 v18.6 终版路由（技术方案 5.2 图 B，2026-08-26 用户定案）。

本地像素判据全线退役（白度/色簇/熵多轮实测效果差）：
① 棋盘格背景判定（qwen-vl 视觉问答"主体以外的背景是否是棋盘格"——真
   alpha 预览拍平截图/素材站假 alpha 导出都带棋盘格；白底/米白纯底/照片
   都不带；VL 失败按 false 零误伤）→ 非棋盘格 skipped 原图交付；
② 棋盘格 → qwen-image-2.0 生成式换白底（配置门 + 主体贴满拦截 + 缓存），
   **生成成功即交付（v18.6 验证门移除：模型对复杂主体大图稳定输出
   247-249 纸白，252 纯白口径拒致整类图降级失效——能力边界不本地兜底，
   直接交给模型）**，模型输出即交付零后处理；
③ API 失败/超时 → 原图 done(degraded)（已尝试记档）。

与形状无关（2026-08-25 定案）：判定/外呼/缓存全在整图域，形状裁剪由
管线 CropStep 在本步之后执行。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.filling")

# 视觉白判定（边缘带背景主色）：亮度 ≥235 且低色差——244 级"视觉白"不因
# 差 1 灰度级被误判为"非纯白"（2026-08-25 images_4 案例暴露的容差问题）
VISUAL_WHITE_LUMA = 235
VISUAL_WHITE_MAX_CHROMA = 12

# ---- Path C 生成式路径阈值（v16；互相耦合的一组，不进配置防拆散语义）----
# 主体门（"背景与主体可分"双判据）：
# a. 浅底：非白带中宽白系（luma≥170 且 chroma≤60）占比下限——渐变米白
#    天然跨多色阶，单一色簇判据会误拦（2026-08-26 心形渐变米白实测最大簇
#    仅 32%）；主体彩色高饱和/深色与浅底天然可分
# b. 深底：非白带最大色簇占比下限（单色深彩背景成片）
# 主体贴满时两判据都不满足 → 判"不可分"放弃（重绘真伪不可验，防误采纳）
GEN_BG_WHITEISH_MIN_RATIO = 0.6
GEN_BG_CLUSTER_MIN_RATIO = 0.9
# （v18.5 存档）空/非空背景的像素判据（白度占比/最大色簇/熵）全线退役
# （2026-08-26 用户定案"本地计算判定效果非常差"）——改 qwen-vl 棋盘格
# 视觉问答（fill_gate_vl.py）：多轮实测米白误放、卡通主体拉高熵（2.83）、
# 车照片误外呼；棋盘格判定标定 4/4。git 历史可溯。
# 米白脏底分支（2026-08-25 22:12 实测翻车补）：素材站导出"透明背景"时
# alpha 与暖白底（243-249）合成，主体边缘混出暖色过渡带（chroma 21-31），
# Path B 严口径 chroma≤12 填不掉——边界带宽白像素中欠纯白（luma<250）
# 占比达此值即判脏底触发生成式。纯白底（数码 255）占比低不触发
GEN_DIRTY_WHITE_LUMA = 250
GEN_DIRTY_WHITE_RATIO = 0.4
# （v18.6 存档）验证门已移除（2026-08-26 用户定案"直接交给模型"）——
# 门A 主体保真（v18 已退役）与门B 背景纯白 ≥0.9（交付域）曾先后守在此处；
# 移除原因：模型对复杂主体大图稳定输出 247-249 纸白，252 纯白口径把该类图
# 整体拒成 done(degraded)——能力边界不该由本地拦截兜底，生成成功即交付。
# git 历史可溯。GEN_PURE_WHITE_* 仅存档供打印质量口径参考。
GEN_PURE_WHITE_LUMA = 252
GEN_PURE_WHITE_MAX_CHROMA = 6


class FillStepResult:
    """填充步的输出。"""

    def __init__(self, image_bgr: np.ndarray, stage_value: str) -> None:
        self.image_bgr = image_bgr  # 填充后的图（skipped 时为原图）
        self.stage_value = stage_value  # done / skipped / fallback


class FillStep:
    """填充步（管线第三阶段）——背景换白 v18.6（VL 棋盘格判定 / 生成式直接交付）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        from src.steps.fill.gate_vl import CheckerboardGate
        from src.steps.fill.cache import FillGenResultCache
        from src.steps.fill.qwenbg import QwenBackgroundReplacer

        self._settings = settings
        self._qwen_bg = QwenBackgroundReplacer(settings)
        self._gen_cache = FillGenResultCache(settings)
        self._checkerboard_gate = CheckerboardGate(settings)

    def run(self, image_ndarray: np.ndarray, crop_shape: str | None = None) -> FillStepResult:
        """背景换白 v18.5 终版路由（5.2 图 B，2026-08-26 用户定案）。

        **与形状无关（2026-08-25 定案）**：判定/外呼/缓存全在整图域，
        crop_shape 仅为兼容签名保留。
        **v18.5 定案**：本地像素判据全线退役（白度/色簇/熵多轮实测效果差），
        改 qwen-vl 视觉问答判定。路由：
        ① 棋盘格背景判定（qwen-vl 问"主体以外的背景是否是棋盘格"——真
           alpha 预览拍平截图/素材站假 alpha 导出都带棋盘格；白底/米白纯底/
           照片都不带）→ 非棋盘格跳过填充（skipped 原图）；VL 失败按 false
           （不填，零误伤）；
        ② 棋盘格 → 生成式换白底（配置门 + 主体贴满拦截 + 缓存），生成成功
           即交付（v18.6 验证门移除，直接交给模型）。
        API 失败/超时 → 原图 done(degraded)。
        前置门：整图真实内容 ≥5%（防满幅背景刷白）。
        """
        from src.steps.imaging import ensure_bgra, flatten_to_white, opaque_mask

        image_bgra = ensure_bgra(image_ndarray)
        analysis_bgr = flatten_to_white(image_bgra)

        image_height, image_width = analysis_bgr.shape[:2]
        module_logger.debug("fill start: size=%dx%d (shape-agnostic)", image_width, image_height)
        # 真实内容门（不透明∩非视觉白，整图域）
        content_mask = opaque_mask(image_bgra) & ~_visual_white_pixels(analysis_bgr)
        content_ratio = float(np.mean(content_mask))
        if content_ratio < 0.05:
            module_logger.debug("fill content gate <5%% (%.3f) → skipped", content_ratio)
            return FillStepResult(image_ndarray, "skipped")  # 满幅背景无"图案外空白"语义

        # ---- ① 棋盘格背景判定（v18.5：qwen-vl 视觉问答，像素判据退役）----
        alpha_channel = image_bgra[:, :, 3]
        opaque = alpha_channel >= 128
        band = self._opaque_border_band(opaque)
        pure_white_ratio = self._band_pure_white_ratio(analysis_bgr, band, alpha_channel)
        has_checkerboard = self._checkerboard_gate.has_checkerboard_background(analysis_bgr)
        module_logger.debug("fill checkerboard gate: vl=%s", has_checkerboard)
        if not has_checkerboard:
            module_logger.debug("fill → skipped (背景非棋盘格)")
            return FillStepResult(image_ndarray, "skipped")

        # ---- 棋盘格 → 生成式换白底（v18.6：生成成功即交付，验证门移除）----
        # 降级链：缓存 → API → 失败才原图（done(degraded)）。
        gen_attempted = False
        if self._should_use_generative(analysis_bgr, band, pure_white_ratio):
            gen_attempted = True
            module_logger.debug("fill gen path triggered")
            cached = self._gen_cache.get(analysis_bgr)
            module_logger.debug("fill gen cache hit=%s", cached is not None)
            if cached is not None:
                # 缓存 = 模型原样输出，直接交付（无像素级后处理）
                module_logger.debug(
                    "fill gen → cache hit, deliver %dx%d", cached.shape[1], cached.shape[0]
                )
                return self._deliver_generated(image_ndarray, cached, "白色背景")
            try:
                generated = self._qwen_bg.replace_background(analysis_bgr)
            except Exception as api_error:  # 客户端契约外的意外异常也降级，不 failed
                module_logger.warning("fill gen api unexpected error: %s", api_error)
                generated = None
            module_logger.debug("fill gen api result=%s", "ok" if generated is not None else "failed")
            if generated is not None:
                # 生成成功即交付（v18.6，2026-08-26 用户定案"直接交给模型"——
                self._gen_cache.put(analysis_bgr, generated)
                module_logger.debug(
                    "fill gen → deliver %dx%d", generated.shape[1], generated.shape[0]
                )
                return self._deliver_generated(image_ndarray, generated, "白色背景")

        # ---- 兜底（v17 起）：未触发/生成式失败 → 原图原样，不产出半成品 ----
        module_logger.debug("fill → passthrough: gen_attempted=%s", gen_attempted)
        if gen_attempted:
            return FillStepResult(image_ndarray, "done(degraded)")
        return FillStepResult(image_ndarray, "skipped")

    @staticmethod
    def _opaque_border_band(opaque: np.ndarray, erode_px: int = 15) -> np.ndarray:
        """不透明区边界带：opaque 掩膜 − 腐蚀 15px（不透明区外沿环）。

        非画布外圈——形状裁剪图的画布外圈是透明区，测那里恒白无意义；
        本带贴着不透明区边缘，是背景（若背景铺边）与主体边缘的所在地。
        全不透明图（无 alpha/假 alpha）：cv2.erode 对全 1 掩膜不腐蚀边界
        （BORDER_REFLECT 语义），带为空集——退化为画布外圈 erode_px 环
        （图幅边界即背景边界）。其余空带（主体贴满/小图）同样退化。
        """
        eroded = cv2.erode(opaque.astype(np.uint8), np.ones((erode_px, erode_px), np.uint8)) > 0
        band = opaque & ~eroded
        if band.any():
            return band
        image_height, image_width = opaque.shape[:2]
        frame_band = np.zeros((image_height, image_width), dtype=bool)
        frame_band[:erode_px, :] = True
        frame_band[-erode_px:, :] = True
        frame_band[:, :erode_px] = True
        frame_band[:, -erode_px:] = True
        return frame_band

    @staticmethod
    def _band_pure_white_ratio(
        analysis_bgr: np.ndarray, band: np.ndarray, alpha_channel: np.ndarray
    ) -> float:
        """边界带纯白占比（v18.1 白色/非白二分）：透明区按白计入，其余按打印
        纯白口径（luma≥252 且 chroma≤6）计白。"""
        pixels = analysis_bgr.astype(int)
        chroma = pixels.max(axis=2) - pixels.min(axis=2)
        luma = pixels.mean(axis=2)
        pure_white = (luma >= GEN_PURE_WHITE_LUMA) & (chroma <= GEN_PURE_WHITE_MAX_CHROMA)
        white_count = int(np.count_nonzero(band & (pure_white | (alpha_channel < 128))))
        return white_count / max(1, np.count_nonzero(band))

    def _should_use_generative(
        self,
        analysis_bgr: np.ndarray,
        band: np.ndarray,
        pure_white_ratio: float,
    ) -> bool:
        """生成式触发门（v18.5：VL 棋盘格门已在上游放行，此处两条件）。

        1) 配置门：fill_gen 双条件开（未配置 → False，原图交付零回归）；
        2) 主体贴满拦截（像素判据，仅此一项保留——拦"主体铺满无背景"的
           图，整幅重绘真伪不可验）：非白带（opaque 边界环）宽白系占比
           ≥0.6 或最大色簇 ≥0.9；主体贴满时带内全是碎片化主体色判"不可分"
           放弃。棋盘格底图必过（格纹带内成片）。
        """
        if not self._qwen_bg.is_configured():
            module_logger.debug("fill gen gate: not configured → passthrough")
            return False
        pixels = analysis_bgr.astype(int)
        chroma = pixels.max(axis=2) - pixels.min(axis=2)
        luma = pixels.mean(axis=2)
        pure_white = (luma >= GEN_PURE_WHITE_LUMA) & (chroma <= GEN_PURE_WHITE_MAX_CHROMA)
        under_white = band & ~pure_white  # 非白带（白色门已判非白，非空）
        under_count = int(np.count_nonzero(under_white))
        if under_count == 0:
            return False
        whiteish = (luma >= 170) & (chroma <= 60)
        whiteish_ratio = float(np.count_nonzero(under_white & whiteish)) / under_count
        cluster_luma = luma[under_white]
        cluster_chroma = chroma[under_white]
        quantized = np.stack([cluster_luma // 10, cluster_chroma // 10], axis=1)
        _, cluster_counts = np.unique(quantized, axis=0, return_counts=True)
        dominant_cluster_ratio = float(cluster_counts.max()) / under_count
        module_logger.debug(
            "fill gen gate: pure_white=%.3f under_white=%d whiteish=%.3f cluster=%.3f",
            pure_white_ratio, under_count, whiteish_ratio, dominant_cluster_ratio,
        )
        separable = (
            whiteish_ratio >= GEN_BG_WHITEISH_MIN_RATIO
            or dominant_cluster_ratio >= GEN_BG_CLUSTER_MIN_RATIO
        )
        return separable

    # （v18.6.1 归一已撤 2026-08-26 15:20——用户实测背景区色阶拉伸在主体
    # 交界处产生颜色断层；纸白问题回归提示词约束（见 qwenbg.py _PROMPT 强化））

    @staticmethod
    def _deliver_generated(
        image_ndarray: np.ndarray,
        generated_bgr: np.ndarray,
        stage_value: str,
    ) -> FillStepResult:
        """生成图整幅交付（无形状信息——形状裁剪在描边步前由管线统一做）。

        3 通道输入返回 3 通道（管线口径惯例）。
        """
        if image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3:
            return FillStepResult(generated_bgr[:, :, :3], stage_value)
        alpha = np.full(generated_bgr.shape[:2], 255, np.uint8)
        delivered = np.dstack([generated_bgr, alpha])
        return FillStepResult(delivered, stage_value)

    # ---- 退役代码存档（2026-08-25 v17）----
    # Path B 拓扑填白：_topology_fill_mask / _subject_alpha_v141 / _run_local /
    # _discrete_color_blocks / _isnet_subject_mask(_soft) / _background_white_by_topology
    # （v3→v15 七轮迭代产出均需人工返工，用户定案移除，未触发/被拒图原图交付）。
    # 形状耦合版判定/合成：_compose_generated / _compose_on_white（2026-08-25
    # 形状解耦定案——填充在整图域工作，形状裁剪移交管线描边前环节）。
    # git 历史可溯。


def _visual_white_pixels(image_bgr: np.ndarray) -> np.ndarray:
    """视觉白像素掩膜（亮度 ≥235 且低色差）——图案判定时白不算图案。"""
    pixels = image_bgr.astype(int)
    luma = pixels.mean(axis=2)
    chroma = pixels.max(axis=2) - pixels.min(axis=2)
    return (luma >= VISUAL_WHITE_LUMA) & (chroma <= VISUAL_WHITE_MAX_CHROMA)
