"""描边步：边界按段白度判定（第四十一次修订）→ 形状外环灰线（白段部分环）。

打印物理事实：食用墨水不印白墨——白背景段贴形状边界处零对比看不见（糯米纸
裁剪无从下手），彩色段天然可见。判定单位因此不是整图而是**形状边界的每一段**：
白段补灰线、彩段不画（第三十三次修订"看得见不加线"在段粒度继续成立）。
纯白底=全环、纯彩底=零线（skipped）、混合底=部分环；stage 值仍 done/skipped。
"""

from __future__ import annotations

import logging
import math
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


# ---- 白度阈值（亮度口径，兼容"拍摄白"） ----
# 数码纯白 ~255；白纸拍摄受光影影响常见 236~248。硬阈值 ≥245 会把后者误判非白底
WHITE_BG_MIN_MEDIAN_LUMA = 235  # 段白度判据：段内背景像素亮度中位数下限
# 段判定常量（第四十一次修订）
SEGMENT_MIN_COUNT = 16  # 边界分段数下限（周长再短也 ≥16 段，弧段粒度不过粗）
SEGMENT_MAX_PX = 64  # 每段弧长上限（px）——段太长会把"大段白+小段彩"糊成一段
SEGMENT_MIN_BG_SAMPLES = 50  # 段内背景样本绝对下限：不足按非白处理（确认白才画线）
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
# 批级线宽声明合法区间（第三十四次修订，前端下拉 1.0–2.0 同口径）
OUTLINE_WIDTH_MM_MIN = 1.0
OUTLINE_WIDTH_MM_MAX = 2.0


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


# （第四十一次修订退役）is_white_background / ring_background_is_white /
# shape_inner_band_is_white / _band_background_pixels——整图"非黑即白"聚合
# 裁决（median≥235 且 uniform≥0.9）对"白底但图案贴边"类图冤杀（pattern_3
# 生产实锤 median=254 纯白、uniform=0.874<0.9 → skipped；rembg 对彩旗/
# 飘带类细长贴边装饰分割覆盖率 0.0~0.2%，背景分离救不了）。git 历史可溯。


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


def outline_width_pixels(settings: PatternToolSettings, width_mm: float | None = None) -> int:
    """描边线宽 mm → px（300DPI 打印基准：px = mm × dpi / 25.4）。

    物理毫米语义恒定，与图像尺寸无关：0.5mm ≈ 6px，1.5mm ≈ 18px。
    width_mm（第三十四次修订）：批级"统一线宽"声明的显式覆盖——合法区间
    OUTLINE_WIDTH_MM_RANGE 内生效，None/非法回退配置 outline_width_mm。
    """
    effective_mm = settings.outline_width_mm
    if width_mm is not None and OUTLINE_WIDTH_MM_MIN <= float(width_mm) <= OUTLINE_WIDTH_MM_MAX:
        effective_mm = float(width_mm)
    pixels_per_mm = settings.print_dpi / 25.4
    return max(1, int(round(effective_mm * pixels_per_mm)))


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

# 形状包围盒宽高比（第三十六次修订）——与前端 app.js SHAPE_ASPECT_RATIOS 同源
# （裁剪弹层的框宽高比锁定值），改形状公式必须两端同改。
SHAPE_BOX_ASPECT_RATIOS = {
    "circle": 1.0,
    "square": 1.0,
    "rectangle-fixed": 1.5,
    "heart": 32.0 / 28.9,
    "star": 1.902 / 1.809,
}


def _round_half_up(value: float) -> int:
    """四舍五入（半向上）——与前端 Math.round 同口径（第三十六次修订补：
    Python round 是银行家舍入，k+0.5 落点与 JS 差 1px，同源公式须同舍入）。"""
    return int(math.floor(value + 0.5))


def default_shape_box(width: int, height: int, shape_value: str) -> tuple[int, int, int, int]:
    """无框形状声明的默认框（第三十六次修订）：图内居中最大的形状包围盒宽高比
    矩形 (left, top, box_w, box_h)——与单独裁剪弹层默认框（autoCropArea=1 +
    宽高比锁定）同几何：无框统一形状 ≡ 打开弹层选形状不动框直接确认。
    rectangle/free 无默认框语义（整图直通），返回整图框。
    """
    aspect = SHAPE_BOX_ASPECT_RATIOS.get(shape_value)
    if aspect is None or width < 1 or height < 1:
        return 0, 0, width, height
    if width / height >= aspect:
        box_height = height
        box_width = _round_half_up(height * aspect)
    else:
        box_width = width
        box_height = _round_half_up(width / aspect)
    box_width = min(box_width, width)
    box_height = min(box_height, height)
    left = (width - box_width) // 2
    top = (height - box_height) // 2
    return left, top, box_width, box_height


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
    image_ndarray: np.ndarray, shape_value: str, settings: PatternToolSettings,
    width_mm: float | None = None, segment_mask: np.ndarray | None = None,
) -> np.ndarray:
    """沿形状边界**向外**画灰线（第三十二次修订：外边缘描边；第四十一次
    修订：segment_mask 非空时只画白段=部分环）。

    结构（外→内）：`alpha=0 透明区 │ 灰线环（线宽参数）│ 完整形状内容`——
    画布外扩一个线宽承接线环，线永不接触内容像素。旧内缩画法（形状最外
    1.2mm 环带覆盖成灰）对满幅/浅色图案实锤"线盖在有效图片内"（第三十一~
    三十二次修订定案撤销）；沿线剪=线随废料丢弃，成品=完整形状；线宽物理
    口径（mm×DPI/25.4）与灰度配置不变。3 通道直调输入返回白底合成 3 通道
    （线在透明区上，裸 3 通道会丢线）。形状外渗出清洗（第二十一次修订）
    随内缩画法一并退役——形状外区域本就被 alpha=0 收口，无可见渗出面。

    segment_mask（画布坐标 bool 掩膜，white_segments_along_boundary 产出）：
    只在其覆盖的边界段落线——混合底图的白段补线、彩段留空（部分环）。
    None 时整环（纯白底全环语义）。掩膜搬运到外扩画布后先按线宽外扩再与
    环带求交：白段采样圆贴画布边时环带落在原画布外的部分也有掩膜覆盖
    （原画布边区的圆盘被裁掉一圈，不外扩会漏画）；彩段一像素不画也不动
    alpha——"该段看得见，本就不需要线"。
    """
    was_3ch = image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3
    image_bgra = ensure_bgra(image_ndarray)
    image_height, image_width = image_bgra.shape[:2]
    shape_mask = crop_shape_region_mask(shape_value, image_width, image_height)
    thickness = outline_width_pixels(settings, width_mm)

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

    if segment_mask is not None:
        # 部分环：白段掩膜搬运到外扩画布并再外扩一个线宽（边界圆盘贴画布边
        # 时环带在原画布外的部分仍被覆盖），与环带求交只白段落线
        segment_padded = np.zeros(padded.shape[:2], np.uint8)
        segment_padded[pad:pad + image_height, pad:pad + image_width] = (
            segment_mask[:image_height, :image_width].astype(np.uint8)
        )
        segment_dilated = cv2.dilate(segment_padded, dilate_kernel) > 0
        ring = ring & segment_dilated

    # 线外全部透明（含矩形全幅照片原画布边区）；线环灰度整环覆盖落位。
    # 部分环的收口沿落线段——彩段不画线也保持原样（"看得见不画"语义）
    alpha_keep = dilated if segment_mask is None else (dilated & ~(segment_dilated & ~ring))
    padded[:, :, 3] = np.where(alpha_keep | (mask_u8 > 0), padded[:, :, 3], 0)
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


def shape_boundary_band(
    image_bgra: np.ndarray, shape_mask: np.ndarray, band_depth: int = 41
) -> np.ndarray:
    """形状边缘向内 band_depth px 的边带掩膜（bool）——按段白度采样的公共区域。"""
    shrunk = _erode_zero_border(
        shape_mask.astype(np.uint8), np.ones((2 * band_depth - 1,) * 2, np.uint8)
    ) > 0
    return shape_mask & ~shrunk & opaque_mask(image_bgra)


def _shape_boundary_points(shape_mask: np.ndarray) -> list[tuple[int, int]]:
    """形状掩膜边界像素序列（沿轮廓排序）——分段白度判定的采样骨架。

    findContours 取最长轮廓（矩形类=画布边框轮廓；circle/heart/star=形状
    外缘），重建后 CHAIN_APPROX_NONE 逐点保留弧线细节，不做折线近似。
    """
    contours, _ = cv2.findContours(
        shape_mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not contours:
        return []
    return [tuple(int(v) for v in point[0]) for point in max(contours, key=cv2.contourArea)]


def white_segments_along_boundary(
    image_bgra: np.ndarray, shape_value: str, settings: PatternToolSettings | None = None
) -> tuple[np.ndarray, int, int]:
    """边界按段白度判定（第四十一次修订）：(白段掩膜, 白段数, 总段数)。

    形状边界参数化等分 N 段（N=max(SEGMENT_MIN_COUNT, 周长/SEGMENT_MAX_PX)），
    每段取边带∩不透明∩**背景**像素（背景=rembg 图案掩膜补集——细长贴边装饰
    即使分割遗漏也只污染所在段的中位数，不再拖垮全图）算亮度中位数，
    ≥WHITE_BG_MIN_MEDIAN_LUMA 记白段。段内背景样本 <SEGMENT_MIN_BG_SAMPLES
    按非白处理——白段要求"确认白"，不是"没发现不白"（rembg 整体失效时段
    样本即边带全量，判定退化为对图案贴边的裸统计，宁可不画）。

    掩膜为段锚点采样圆的并集（画布坐标 bool）——段缘相邻圆相接保证连续；
    采样只落在锚点 bounding box 内（3425² 大图不做全图网格运算）。
    """
    image_height, image_width = image_bgra.shape[:2]
    shape_mask = crop_shape_region_mask(shape_value, image_width, image_height)
    band = shape_boundary_band(image_bgra, shape_mask)
    boundary = _shape_boundary_points(shape_mask)
    if not boundary:
        return np.zeros((image_height, image_width), bool), 0, 0

    segment_count = max(SEGMENT_MIN_COUNT, len(boundary) // SEGMENT_MAX_PX)
    background = np.ones_like(band)
    if settings is not None and settings.outline_matting_enabled:
        try:
            background = ~segment_pattern_mask(image_bgra, settings) > 0
        except Exception as matting_error:
            module_logger.warning("outline segment matting failed: %s", matting_error)

    white_mask = np.zeros((image_height, image_width), bool)
    white_count = 0
    # 段采样半径：弧长一半（等分锚点间距/2）与边带深度取大——锚点间的边界
    # 像素也要被最近段的采样圆覆盖，不留漏判缝隙
    sample_radius = max(len(boundary) // segment_count // 2, 41)
    for segment_index in range(segment_count):
        anchors = [
            boundary[(anchor_index * len(boundary)) // segment_count % len(boundary)]
            for anchor_index in (segment_index, segment_index + 1)
        ]
        center_x = sum(x for x, _ in anchors) / len(anchors)
        center_y = sum(y for _, y in anchors) / len(anchors)
        left = max(0, int(center_x - sample_radius))
        right = min(image_width, int(center_x + sample_radius) + 1)
        top = max(0, int(center_y - sample_radius))
        bottom = min(image_height, int(center_y + sample_radius) + 1)
        if right <= left or bottom <= top:
            continue
        # ogrid 首轴是行（y）、次轴是列（x）——距离平方按 (row-cy)²+(col-cx)²
        local_row, local_col = np.ogrid[top:bottom, left:right]
        near = (local_row - center_y) ** 2 + (local_col - center_x) ** 2 <= sample_radius**2
        samples = band[top:bottom, left:right] & near & background[top:bottom, left:right]
        if np.count_nonzero(samples) < SEGMENT_MIN_BG_SAMPLES:
            continue  # 样本不足=无法确认白，按非白段处理
        segment_pixels = image_bgra[top:bottom, left:right][samples][:, :3]
        gray = cv2.cvtColor(segment_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).ravel()
        if float(np.median(gray)) >= WHITE_BG_MIN_MEDIAN_LUMA:
            white_count += 1
            white_mask[top:bottom, left:right] |= near
    return white_mask, white_count, segment_count


class OutlineStep:
    """描边步（管线第四阶段）——描边对象 = 最终形状边界，白底才画线（图像处理方案 3.5）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings

    def run(
        self, image_ndarray: np.ndarray, crop_shape: str | None = None,
        width_mm: float | None = None,
    ) -> OutlineStepResult:
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
        # 第四十一次修订：按段白度判定替代整图门——打印不印白墨，白段看不见
        # 才需补线，彩段本来看得见不画。纯白=全环、混合=部分环（都记 done），
        # 整圈非白=零线（skipped）。段粒度下 rembg 分割遗漏只污染所在段。
        white_mask, white_count, segment_count = white_segments_along_boundary(
            image_bgra, shape_value, self._settings
        )
        if segment_count > 0:
            module_logger.info(
                "outline segments: %d/%d 白段", white_count, segment_count
            )
        if white_count == 0:
            module_logger.info("outline → skipped (边界全段非白：整圈看得见)")
            return OutlineStepResult(image_ndarray, "skipped")
        partial = white_count < segment_count  # 全段白=全环；任一段非白=部分环
        # 第三十二次修订：外边缘描边（线环在形状外侧透明区上，永不接触内容）
        outlined = draw_shape_outline(
            image_ndarray, shape_value, self._settings, width_mm,
            segment_mask=white_mask if partial else None,
        )
        module_logger.info(
            "outline → done(%s): 线宽=%dpx 灰度=%d%s",
            "部分环" if partial else "外环",
            outline_width_pixels(self._settings, width_mm), self._settings.outline_gray_level,
            f"（批级声明 {width_mm}mm）" if width_mm is not None else "",
        )
        return OutlineStepResult(outlined, "done")
