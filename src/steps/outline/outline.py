"""描边步：白底判定 → 图案分割（rembg alpha 蒙版，阈值法兜底）→ 外侧环带灰线。

技术方案 5.2 图 B 与图像处理方案 3.5 口径：
- 白底（亮度+均匀性双判据，兼容拍摄白）才描边，非白底 skipped；
- 图案分割首选 rembg U²-Net alpha 蒙版（深度显著性分割，毛发/浅色/细凸起
  细节可用）；rembg 不可用/异常自动回退灰度自适应阈值法，描边不因此 failed；
- 掩膜后处理与分割来源无关：碎屑过滤 → 轮廓平滑 → 图案外侧膨胀环带灰线
  （零侵占图案本体；线宽 mm×DPI/25.4 物理恒定）。
"""

from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

from src.core.config import PatternToolSettings
from src.steps.imaging import ensure_bgra, flatten_to_white, opaque_mask

module_logger = logging.getLogger("pattern_tool.outline")


class OutlineStepResult:
    """描边步的输出。"""

    def __init__(self, image_bgr: np.ndarray, stage_value: str) -> None:
        self.image_bgr = image_bgr  # 描边后的图（skipped 时为原图）
        self.stage_value = stage_value  # done / skipped


# ---- 白底判定与图案掩膜阈值（亮度口径，兼容"拍摄白"） ----
# 数码纯白 ~255；白纸拍摄受光影影响常见 236~248。硬阈值 ≥245 会把后者误判非白底
WHITE_BG_MIN_MEDIAN_LUMA = 235  # 背景整体够亮：边缘亮度中位数下限
WHITE_BG_UNIFORM_LUMA = 230  # 均匀性：亮度低于此值的边缘像素视为杂色/暗物
WHITE_BG_UNIFORM_RATIO = 0.9  # ≥ 90% 边缘像素达均匀亮度才算白底
PATTERN_LUMA_MARGIN = 12  # 图案判定：比背景亮度低此值以上才算图案（容忍软阴影）
PATTERN_LUMA_CLIP = (220, 245)  # 图案亮度阈值的裁剪区间（上限=数码纯白语义不放宽）
MIN_PATTERN_AREA_RATIO = 0.05  # 连通块面积达主图案 5% 以上才描（碎屑过滤）
MIN_PATTERN_AREA_PIXELS = 25  # 碎屑绝对下限（灰尘点/压缩噪点不分图幅一律丢弃）
OUTLINE_SMOOTH_KERNEL = 7  # 轮廓平滑：开/闭运算核（去毛刺与豁口）
OUTLINE_SMOOTH_BLUR_SIGMA = 4.0  # 轮廓平滑：高斯模糊 σ（圆滑像素级阶梯锯齿）
# 形状边羽化补偿（2026-08-28 第二十五次修订）：resize 形状 alpha 重画的
# sigma 0.8 羽化使边界内 ~2px 是 alpha<255 过渡带；描边只落 alpha==255
# 硬区——线带最外 1-2px 被羽化吃掉（幅面越大占比越高）。起画内缩线宽
# +此补偿，厚度按线宽参数画足，物理线宽回到参数值。
SHAPE_EDGE_FEATHER_COMPENSATION_PX = 2


def edge_band_pixels(image_ndarray: np.ndarray) -> np.ndarray:
    """图像边缘条带颜色像素（四周 2% 宽，Nx3 BGR）——图案主体居中是常态，边缘即背景采样区。"""
    image_height, image_width = image_ndarray.shape[:2]
    band_height = max(2, image_height // 50)
    band_width = max(2, image_width // 50)
    edge_regions = [
        image_ndarray[:band_height, :, :],  # 顶
        image_ndarray[-band_height:, :, :],  # 底
        image_ndarray[:, :band_width, :],  # 左
        image_ndarray[:, -band_width:, :],  # 右
    ]
    pixels = np.concatenate([region.reshape(-1, image_ndarray.shape[2]) for region in edge_regions])
    return pixels[:, :3]  # 4 通道输入丢弃 alpha，只取颜色


def is_white_background(image_bgra: np.ndarray) -> bool:
    """第一闸（边缘带∩不透明）：亮度+均匀性双判据，兼容拍摄白。

    透明区（形状裁剪外）不参与采样——裁剪白边不能再把灰底照片"洗白"。
    不透明样本过少（<2% 图幅，形状裁剪常态）时放行，交给第二闸（环带复核）。
    """
    image_height, image_width = image_bgra.shape[:2]
    band_height = max(2, image_height // 50)
    band_width = max(2, image_width // 50)
    opaque = opaque_mask(image_bgra)
    band_opaque_flags = np.concatenate([
        opaque[:band_height, :].reshape(-1),
        opaque[-band_height:, :].reshape(-1),
        opaque[:, :band_width].reshape(-1),
        opaque[:, -band_width:].reshape(-1),
    ])
    # 边缘带大部分透明 = 形状裁剪常态（无背景页边可审），交第二闸环带复核裁决
    if band_opaque_flags.mean() < 0.5:
        return True
    sampled = edge_band_pixels(image_bgra)[band_opaque_flags]
    if sampled.shape[0] < 0.02 * image_height * image_width:
        return True
    edge_gray = cv2.cvtColor(sampled.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()
    median_luma = float(np.median(edge_gray))
    uniform_ratio = float(np.mean(edge_gray >= WHITE_BG_UNIFORM_LUMA))
    return median_luma >= WHITE_BG_MIN_MEDIAN_LUMA and uniform_ratio >= WHITE_BG_UNIFORM_RATIO


def ring_background_is_white(image_bgra: np.ndarray, pattern_mask: np.ndarray) -> bool:
    """第二闸（环带复核）：图案外扩 8–40px∩不透明 的背景是否够白。

    拦"形状裁剪白边 + 灰底/彩底照片"类假白底：边缘带被裁剪白边占据，
    但图案真实周边背景是灰/彩色的，不该描边。环带不透明样本不足时放行
    （图案铺满图幅的常态，无背景可审）。
    """
    opaque = opaque_mask(image_bgra)
    mask_bool = pattern_mask > 0  # 全程布尔——uint8 掩膜会被 numpy 当行号花式索引
    inner = cv2.dilate(mask_bool.astype(np.uint8), np.ones((17, 17), np.uint8)) > 0  # 外扩 8px
    outer = cv2.dilate(mask_bool.astype(np.uint8), np.ones((81, 81), np.uint8)) > 0  # 外扩 40px
    ring = (outer & ~inner & ~mask_bool & opaque).astype(bool)
    if np.count_nonzero(ring) < 0.02 * image_bgra.shape[0] * image_bgra.shape[1]:
        return True
    ring_pixels = image_bgra[ring][:, :3]
    ring_gray = cv2.cvtColor(ring_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()
    return float(np.median(ring_gray)) >= WHITE_BG_MIN_MEDIAN_LUMA


# ---- rembg 分割会话（进程级懒加载单例；onnxruntime InferenceSession 并发安全） ----
_matting_sessions: dict[str, object] = {}
_matting_session_lock = threading.Lock()


def _get_matting_session(model_name: str):
    """线程安全地初始化 rembg 会话（首次推理才加载/下载模型，避免启动阻塞）。"""
    with _matting_session_lock:
        if model_name not in _matting_sessions:
            from rembg import new_session

            _matting_sessions[model_name] = new_session(model_name)
        return _matting_sessions[model_name]


def segment_pattern_mask(
    image_ndarray: np.ndarray, settings: PatternToolSettings
) -> np.ndarray:
    """图案二值掩膜（255=图案，已交不透明区）：rembg alpha 蒙版为主，灰度阈值法兜底。"""
    image_bgra = ensure_bgra(image_ndarray)
    opaque = opaque_mask(image_bgra)
    if settings.outline_matting_enabled:
        try:
            from rembg import remove

            session = _get_matting_session(settings.outline_matting_model)
            # 分析在白底合成副本上做（透明区的 BGR 残值不进模型）；结果交不透明区
            analysis_bgr = flatten_to_white(image_bgra)
            module_logger.debug("outline matting: rembg session=%s", settings.outline_matting_model)
            rgba_result = remove(cv2.cvtColor(analysis_bgr, cv2.COLOR_BGR2RGB), session=session)
            alpha_mask = rgba_result[:, :, 3]
            binary = (alpha_mask >= settings.outline_matting_alpha_threshold).astype(np.uint8) * 255
            module_logger.debug(
                "outline matting: 图案掩膜占比=%.1f%%", float(np.mean(binary > 0)) * 100
            )
            return binary * opaque
        except Exception as matting_error:  # 组件缺失/模型下载失败/推理异常 → 阈值法兜底
            module_logger.warning("outline matting failed, fallback to threshold: %s", matting_error)
    return _threshold_pattern_mask(image_bgra)


def _threshold_pattern_mask(image_bgra: np.ndarray) -> np.ndarray:
    """灰度自适应阈值掩膜（兜底路径）：亮度低于"背景亮度 − 容差"且不透明即图案。"""
    edge_gray = cv2.cvtColor(edge_band_pixels(image_bgra).reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()
    background_luma = float(np.median(edge_gray))
    pattern_luma_threshold = int(np.clip(background_luma - PATTERN_LUMA_MARGIN, *PATTERN_LUMA_CLIP))
    image_gray = cv2.cvtColor(flatten_to_white(image_bgra), cv2.COLOR_BGR2GRAY)
    pattern = (image_gray < pattern_luma_threshold) & opaque_mask(image_bgra)
    return pattern.astype(np.uint8) * 255


def outline_width_pixels(settings: PatternToolSettings) -> int:
    """描边线宽 mm → px（300DPI 打印基准：px = mm × dpi / 25.4）。

    物理毫米语义恒定，与图像尺寸无关：0.5mm ≈ 6px，1.5mm ≈ 18px。
    """
    pixels_per_mm = settings.print_dpi / 25.4
    return max(1, int(round(settings.outline_width_mm * pixels_per_mm)))


def draw_outline(
    image_ndarray: np.ndarray, pattern_mask: np.ndarray, settings: PatternToolSettings
) -> np.ndarray:
    """沿图案掩膜向外描灰线（掩膜由 segment_pattern_mask 产出，交不透明区）。

    画法：膨胀环带 = 图案外扩 thickness 像素的区域 − 图案本体，
    灰线只落在环带∩不透明内——描边零侵占图案本体，也不画进透明区。镂空
    图案的内缘同样由膨胀自然覆盖。输入 3/4 通道皆可，输出与输入同通道。
    """
    image_bgra = ensure_bgra(image_ndarray)
    pattern_mask = (pattern_mask > 0).astype(np.uint8) * 255

    # 形态学闭运算抹平图案内部细小白点，避免轮廓碎片
    smoothing_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    pattern_mask = cv2.morphologyEx(pattern_mask, cv2.MORPH_CLOSE, smoothing_kernel)

    # 碎屑过滤：白底上的灰尘点/JPG 压缩噪点是几个像素的暗斑，
    # 不过滤会给每个小斑描一圈灰环（看起来是散落的灰色小圆点）。
    # 只保留面积达主图案 5%（且 ≥ 25px）的连通块。
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(pattern_mask, connectivity=8)
    if component_count > 1:  # 标签 0 是背景
        component_areas = stats[1:, cv2.CC_STAT_AREA]
        largest_area = int(component_areas.max())
        keep_threshold = max(largest_area * MIN_PATTERN_AREA_RATIO, MIN_PATTERN_AREA_PIXELS)
        keep_labels = [
            1 + area_index
            for area_index, area in enumerate(component_areas)
            if area >= keep_threshold
        ]
        pattern_mask = np.isin(labels, keep_labels).astype(np.uint8) * 255

    # 轮廓平滑：灰线打印后要沿线剪刀裁剪，锯齿轮廓不可用。
    # 逐像素阈值的掩膜边界带毛发/噪点级锯齿——开运算去毛刺豁口，
    # 高斯模糊+重阈值圆滑像素阶梯；平滑结果只作膨胀种子，
    # 环带仍以原始图案掩膜作排除（零侵占图案本体的性质不变）。
    smooth_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (OUTLINE_SMOOTH_KERNEL, OUTLINE_SMOOTH_KERNEL)
    )
    smoothed_mask = cv2.morphologyEx(pattern_mask, cv2.MORPH_OPEN, smooth_kernel)
    smoothed_mask = cv2.morphologyEx(smoothed_mask, cv2.MORPH_CLOSE, smooth_kernel)
    smoothed_mask = cv2.threshold(
        cv2.GaussianBlur(smoothed_mask, (0, 0), OUTLINE_SMOOTH_BLUR_SIGMA),
        127, 255, cv2.THRESH_BINARY,
    )[1]

    line_thickness_pixels = outline_width_pixels(settings)
    # 半径 = 线宽的结构元 → 图案向外生长恰好 thickness 像素
    dilate_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (2 * line_thickness_pixels + 1, 2 * line_thickness_pixels + 1),
    )
    dilated_mask = cv2.dilate(smoothed_mask, dilate_kernel)
    ring_mask = (dilated_mask > 0) & (pattern_mask == 0)  # 环带：外扩区去掉原始本体
    ring_mask &= opaque_mask(image_bgra)  # 透明区不落笔（打印不印的语义）

    gray_value = settings.outline_gray_level
    outlined_image = image_bgra.copy()
    outlined_image[ring_mask] = (gray_value, gray_value, gray_value, 255)
    if image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3:
        return outlined_image[:, :, :3]  # 3 通道输入保持 3 通道输出（直调兼容）
    return outlined_image


# ---- 裁剪形状边界几何（与前端 app.js buildShapePath 同源公式，保证所见即所得） ----

RECTANGLE_LIKE_SHAPES = {"square", "rectangle", "rectangle-fixed", "free"}
KNOWN_CROP_SHAPES = RECTANGLE_LIKE_SHAPES | {"circle", "heart", "star"}


def crop_shape_region_mask(shape_value: str, width: int, height: int) -> np.ndarray:
    """裁剪形状在图幅内的区域掩膜（bool）——形状按真实包围盒最大化内接。

    2026-08-26 15:39 配套前端（所见即所得）：前端选框按形状真实包围盒锁定
    宽高比（心 32:28.9、星 1.9:1.81），后端掩膜同源最大化——
    - circle：内接圆 r=min/2（圆对称，无变化）；
    - heart：scale = min(W/32, H/28.9)，y 垂直居中（公式 y 中心 ≈2.55）；
    - star：顶点朝上包围盒宽 1.9r，r = min(W,H)/1.9（宽贴边）。

    2026-08-28 第二十九次修订：square/rectangle-fixed 从"全图掩膜"改为
    **居中内接**（square=min 边正方形、rectangle-fixed=3:2 矩形，宽高比与
    弹层 SHAPE_ASPECT_RATIOS 同源）——批级无框声明下"选了形状就该看得见
    形状"（旧矩形类一律全图=直通，用户实锤"正方形/长方形设置不到图案上"）。
    rectangle（自由矩形）仍全图（默认直通零变化）；带框路径不受影响（框
    裁外接区后矩形类=裁框本身，不走本函数）。
    """
    canvas = np.zeros((height, width), np.uint8)
    if shape_value == "square":
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        canvas[top : top + side, left : left + side] = 1
        return canvas > 0
    if shape_value == "rectangle-fixed":
        # 3:2 长方形居中最大化：先按"宽=高×1.5"假设，越幅则按"高=宽/1.5"反推
        # （宽高比 1.5 与前端 SHAPE_ASPECT_RATIOS['rectangle-fixed'] 同源）
        aspect_ratio = 1.5
        if round(height * aspect_ratio) <= width:
            rect_h = height
            rect_w = round(height * aspect_ratio)
        else:
            rect_w = width
            rect_h = round(width / aspect_ratio)
        left = (width - rect_w) // 2
        top = (height - rect_h) // 2
        canvas[top : top + rect_h, left : left + rect_w] = 1
        return canvas > 0
    if shape_value in RECTANGLE_LIKE_SHAPES:
        canvas[:, :] = 1  # rectangle/free：图幅边界即形状边界（直通）
        return canvas > 0
    center_x, center_y = width / 2, height / 2
    if shape_value == "circle":
        radius = min(width, height) / 2
        cv2.circle(canvas, (int(center_x), int(center_y)), int(radius), 1, -1)
        return canvas > 0
    if shape_value == "heart":
        scale = min(width / 32.0, height / 28.9)
        points = []
        for step_index in range(201):
            theta = (step_index / 200) * np.pi * 2
            heart_x = 16 * np.sin(theta) ** 3
            heart_y = -(13 * np.cos(theta) - 5 * np.cos(2 * theta) - 2 * np.cos(3 * theta) - np.cos(4 * theta))
            points.append([
                int(center_x + heart_x * scale),
                int(center_y + (heart_y - 2.55) * scale),
            ])
        cv2.fillPoly(canvas, [np.asarray(points, dtype=np.int32)], 1)
        return canvas > 0
    # star：十顶点星形（内半径 0.44r 尖锐标准星——2026-08-26 15:53 用户定案
    # 撤回胖星；精确包围盒：宽 1.902r、高 1.809r、y∈[-1,0.809]（顶角非对称）——
    # r = min(W/1.902, H/1.809)，y 偏移 +0.0955r 垂直居中，顶角零截断）
    star_radius = min(width / 1.902, height / 1.809)
    star_points = []
    for vertex_index in range(10):
        vertex_angle = -np.pi / 2 + vertex_index * np.pi / 5
        vertex_radius = star_radius if vertex_index % 2 == 0 else star_radius * 0.44
        star_points.append([
            int(center_x + vertex_radius * np.cos(vertex_angle)),
            int(center_y + 0.0955 * star_radius + vertex_radius * np.sin(vertex_angle)),
        ])
    cv2.fillPoly(canvas, [np.asarray(star_points, dtype=np.int32)], 1)
    return canvas > 0


# （第三十一次修订存档，第三十二次修订退役）_shape_line_band_geometry 与
# line_band_is_clear（线带净空判定）随内缩画法退役——外环画法线永不接触
# 内容，判定前提不成立。git 历史可溯（v19.6）。

def draw_shape_outline(
    image_ndarray: np.ndarray, shape_value: str, settings: PatternToolSettings
) -> np.ndarray:
    """沿形状边界**向外**画灰线（第三十二次修订：外边缘描边）。

    结构（外→内）：`alpha=0 透明区 │ 灰线环（线宽参数）│ 完整形状内容`——
    画布外扩一个线宽承接线环，线永不接触内容像素。旧内缩画法（形状最外
    1.2mm 环带覆盖成灰）对满幅/浅色图案实锤"线盖在有效图片内"（第三十一~
    三十二次修订定案撤销）；沿线剪=线随废料丢弃，成品=完整形状；线宽物理
    口径（mm×DPI/25.4）与灰度配置不变。3 通道直调输入返回白底合成 3 通道
    （线在透明区上，裸 3 通道会丢线）。形状外渗出清洗（第二十一次修订）
    随内缩画法一并退役——形状外区域本就被 alpha=0 收口，无可见渗出面。
    """
    was_3ch = image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3
    image_bgra = ensure_bgra(image_ndarray)
    image_height, image_width = image_bgra.shape[:2]
    shape_mask = crop_shape_region_mask(shape_value, image_width, image_height)
    thickness = outline_width_pixels(settings)

    pad = thickness  # 形状掩膜内接贴画布边，外环需要落位空间
    padded = cv2.copyMakeBorder(
        image_bgra, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(0, 0, 0, 0)
    )
    mask_u8 = np.zeros(padded.shape[:2], np.uint8)
    mask_u8[pad:pad + image_height, pad:pad + image_width] = shape_mask.astype(np.uint8)
    dilate_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * thickness + 1, 2 * thickness + 1)
    )
    dilated = cv2.dilate(mask_u8, dilate_kernel) > 0
    ring = dilated & (mask_u8 == 0)

    # 线外全部透明（含矩形全幅照片原画布边区）；线环灰度整环覆盖落位
    padded[:, :, 3] = np.where(dilated, padded[:, :, 3], 0)
    gray_value = settings.outline_gray_level
    padded[ring] = (gray_value, gray_value, gray_value, 255)

    if was_3ch:
        alpha = padded[:, :, 3:4].astype(np.float32) / 255.0
        composited = (
            padded[:, :, :3].astype(np.float32) * alpha + 255.0 * (1.0 - alpha)
        ).astype(np.uint8)
        return composited
    return padded


def _erode_zero_border(mask_u8: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """边界填 0 的腐蚀（内缩）：OpenCV erode 默认边界值=极大，全图掩膜
    （square 等矩形类形状区域=画布）内缩完全不生效——显式 0 填充才啃边。"""
    return cv2.erode(mask_u8, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)


def shape_inner_band_is_white(
    image_bgra: np.ndarray, shape_value: str, settings: PatternToolSettings | None = None
) -> bool:
    """形状裁剪图的白底判定：边带内**背景像素**（剔除图案后）是否够白。

    口径修正（2026-08-24）：白底判定看的是"背景"，不是"边带里图案占多少"——
    图案贴形状边界（果冻类满幅图案）会把图案像素混进亮度统计误杀白底。
    背景像素识别：优先 rembg 图案掩膜的补集；rembg 不可用时退"亮度离群剔除"
    （边带亮度的高分位簇为背景）。灰底/彩底照片的背景像素整片暗，仍正确拒绝。
    """
    image_height, image_width = image_bgra.shape[:2]
    shape_mask = crop_shape_region_mask(shape_value, image_width, image_height)
    shrunk = _erode_zero_border(shape_mask.astype(np.uint8), np.ones((81, 81), np.uint8)) > 0  # 内缩 40px
    band = shape_mask & ~shrunk & opaque_mask(image_bgra)  # 形状边缘向内 0–40px 的带
    if np.count_nonzero(band) < 0.02 * image_height * image_width:
        return True
    band_pixels = image_bgra[band][:, :3]
    band_gray = cv2.cvtColor(band_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()

    background_gray = _band_background_pixels(band, band_gray, image_bgra, settings)
    if background_gray is None or background_gray.size == 0:
        return True  # 无法分离背景（极端图）：放行交画线，线断了也比漏描可接受
    median_luma = float(np.median(background_gray))
    uniform_ratio = float(np.mean(background_gray >= WHITE_BG_UNIFORM_LUMA))
    return median_luma >= WHITE_BG_MIN_MEDIAN_LUMA and uniform_ratio >= WHITE_BG_UNIFORM_RATIO


def _band_background_pixels(
    band: np.ndarray,
    band_gray: np.ndarray,
    image_bgra: np.ndarray,
    settings: PatternToolSettings | None,
) -> np.ndarray | None:
    """从边带亮度样本中分离背景像素（剔除图案）。

    优先按 rembg 图案掩膜取补集采样；不可用时按亮度聚类近似——
    边带亮度的上四分位簇视为背景（图案通常比白底暗）。
    """
    if settings is not None and settings.outline_matting_enabled:
        try:
            pattern_mask = segment_pattern_mask(image_bgra, settings) > 0
            background_band = band & ~pattern_mask
            if np.count_nonzero(background_band) >= 0.01 * image_bgra.shape[0] * image_bgra.shape[1]:
                pixels = image_bgra[background_band][:, :3]
                return cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()
        except Exception:
            pass
    # 亮度聚类兜底：上四分位为背景簇
    return band_gray[band_gray >= np.percentile(band_gray, 75)]


class OutlineStep:
    """描边步（管线第四阶段）——描边对象 = 最终形状边界，白底才画线（图像处理方案 3.5）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def run(self, image_ndarray: np.ndarray, crop_shape: str | None = None) -> OutlineStepResult:
        """描边沿形状边界（每图必有形状，2026-08-25 需求）。

        crop_shape 为 None / 未知值（防御：api 层已兜底补默认，正常不可达）
        回退 rectangle 整图边框（2026-08-27 默认值修订，原 circle 撤销——
        未选形状的图不应被圆裁形），不再走图案轮廓旧路径。
        """
        shape_value = crop_shape if crop_shape in KNOWN_CROP_SHAPES else "rectangle"
        image_bgra = ensure_bgra(image_ndarray)
        module_logger.info(
            "outline start: shape=%s size=%dx%d", shape_value, image_bgra.shape[1], image_bgra.shape[0]
        )
        # 第三十二次修订：外边缘描边（线环在形状外侧透明区上，永不接触内容）
        # ——白底判定（shape_inner_band_is_white）与线带净空判定
        # （line_band_is_clear）随内缩画法退役：它们存在的前提"线会压内容"
        # 在外环画法下不成立，且浅色图案（luma≥230 的冰蓝渐变）实测两闸
        # 都拦不住误画/误跳。函数保留作历史存档，不再被管线调用。
        outlined = draw_shape_outline(image_ndarray, shape_value, self._settings)
        module_logger.info(
            "outline → done(外环): 线宽=%dpx 灰度=%d",
            outline_width_pixels(self._settings), self._settings.outline_gray_level,
        )
        return OutlineStepResult(outlined, "done")
