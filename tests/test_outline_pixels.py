"""描边像素级断言（7.3 验收 4：灰线沿实际轮廓，非外接矩形）。"""

from __future__ import annotations

import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import submit_single_image, wait_until_job_completed


def test_outline_default_rectangle_when_uncropped(api_client: TestClient):
    """验收 4（2026-08-27 修订口径：未裁剪图默认矩形整图）：白底心形图案、无 crop_meta
    → 不裁形（无透明区）、描边为整图边框灰线（原默认圆形撤销——用户实测
    "没选形状被裁成圆"非预期）。"""

    canvas = np.full((600, 600, 3), 255, dtype=np.uint8)
    points: list[list[int]] = []
    center_x, center_y, radius = 300, 300, 240
    for step in range(201):
        theta = (step / 200) * np.pi * 2
        heart_x = 16 * np.sin(theta) ** 3
        heart_y = -(13 * np.cos(theta) - 5 * np.cos(2 * theta) - 2 * np.cos(3 * theta) - np.cos(4 * theta))
        points.append([int(center_x + heart_x * radius / 17), int(center_y + heart_y * radius / 17)])
    cv2.fillPoly(canvas, [np.asarray(points, dtype=np.int32)], (180, 60, 200))

    success, buffer = cv2.imencode(".png", canvas)
    assert success
    job_id = submit_single_image(api_client, buffer.tobytes())
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["status"] == "completed"
    assert image_status["stage_results"]["outline"] == "done"
    assert json.loads(image_status["stage_results"]["crop"])["shape"] == "rectangle"

    download = api_client.get(image_status["result_url"])
    result_bgr = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    # 外环四角圆角缺口（椭圆核等宽膨胀的正确偏移曲线，半径=线宽）：缺口
    # alpha=0，主体不透明（第三十二次修订语义）
    if result_bgr.ndim == 3 and result_bgr.shape[2] == 4:
        assert int(result_bgr[0, 0, 3]) == 0, "外环圆角缺口应透明"
        assert int(result_bgr[300, 300, 3]) == 255, "主体应不透明"
    gray_line = np.all(np.abs(result_bgr[:, :, :3].astype(int) - 190) <= 10, axis=2)
    # 外边缘描边（第三十二次修订）：画布外扩 18px 承接环带（600→636），
    # 环带在最外圈行 0–17 / 618–635，中央无灰线
    assert gray_line[0:18, 300].any(), "顶环带无灰线"
    assert gray_line[618:636, 300].any(), "底环带无灰线"
    assert not gray_line[300, 300], "画布中央出现灰线（非外环描边）"


def legacy_test_outline_follows_actual_contour(api_client: TestClient):
    """（旧口径归档，2026-08-25 需求变更后不再适用——默认圆形取代图案轮廓）
    验收 4：心形图案描边沿心形轮廓灰线（RGB 190±10），不是外接矩形。

    造图：白底 + 中央实心心形 → 描边后检查：(a) 灰线像素存在且色值达标；
    (b) 灰线贴近心形边界而非画布四角矩形边框。
    """
    # 造心形图案（与前端 buildShapePath 同一参数化曲线）
    canvas = np.full((600, 600, 3), 255, dtype=np.uint8)
    points: list[list[int]] = []
    center_x, center_y, radius = 300, 300, 240
    for step in range(201):
        theta = (step / 200) * np.pi * 2
        heart_x = 16 * np.sin(theta) ** 3
        heart_y = -(13 * np.cos(theta) - 5 * np.cos(2 * theta) - 2 * np.cos(3 * theta) - np.cos(4 * theta))
        points.append([int(center_x + heart_x * radius / 17), int(center_y + heart_y * radius / 17)])
    heart_contour = np.asarray(points, dtype=np.int32)
    cv2.fillPoly(canvas, [heart_contour], (180, 60, 200))  # 紫色心形（BGR）

    success, buffer = cv2.imencode(".png", canvas)
    assert success
    job_id = submit_single_image(api_client, buffer.tobytes())
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["status"] == "completed"
    assert image_status["stage_results"]["outline"] == "done"

    # 取结果图
    download_response = api_client.get(image_status["result_url"])
    assert download_response.status_code == 200
    result_bgr = cv2.imdecode(
        np.frombuffer(download_response.content, dtype=np.uint8), cv2.IMREAD_COLOR
    )

    # (a) 灰线存在：值 190±10 的三通道等值像素
    gray_line_mask = np.all(np.abs(result_bgr.astype(int) - 190) <= 10, axis=2)
    gray_line_pixel_count = int(np.count_nonzero(gray_line_mask))
    assert gray_line_pixel_count > 500, "灰线像素过少，描边可能未生效"

    # (b) 沿实际轮廓（管线语义：描边对象是"填充后"的图案轮廓——
    #     心形凹谷会被填充步向内长入填高，属填充语义的正常边界效应）。
    #     抽样填充后图案的边缘带点（腐蚀差集=内边界），断言灰线贴近；
    #     邻域 ±25px 覆盖轮廓顶点舍入偏差与凹点线宽收窄。
    pattern_after_pipeline = ~np.all(result_bgr >= 245, axis=2)
    # 描边灰线本身也非白：用"去灰线像素"的图案近似填充后形状
    gray_line_values = np.all(np.abs(result_bgr.astype(int) - 190) <= 10, axis=2)
    pattern_without_line = pattern_after_pipeline & ~gray_line_values
    eroded_pattern = cv2.erode(
        pattern_without_line.astype(np.uint8), np.ones((5, 5), np.uint8)
    ) > 0
    edge_band = pattern_without_line & ~eroded_pattern
    edge_ys, edge_xs = np.nonzero(edge_band)
    assert len(edge_ys) > 100, "图案边缘带采样点过少"
    random_sampler = np.random.RandomState(20260824)
    sample_positions = random_sampler.choice(len(edge_ys), size=200, replace=False)
    hit_count = 0
    for sample_position in sample_positions:
        edge_x, edge_y = int(edge_xs[sample_position]), int(edge_ys[sample_position])
        neighborhood = gray_line_values[
            max(0, edge_y - 25):edge_y + 25, max(0, edge_x - 25):edge_x + 25
        ]
        if neighborhood.any():
            hit_count += 1
    assert hit_count >= 190, f"灰线仅 {hit_count}/200 贴合实际轮廓，疑似外接矩形描边"
    # 四角无灰线（排除外接矩形描边）
    corner_size = 20
    corners = [
        gray_line_mask[:corner_size, :corner_size],
        gray_line_mask[:corner_size, -corner_size:],
        gray_line_mask[-corner_size:, :corner_size],
        gray_line_mask[-corner_size:, -corner_size:],
    ]
    for corner_index, corner_mask in enumerate(corners):
        assert not corner_mask.any(), f"画布角 {corner_index} 出现灰线，疑似外接矩形描边"


def test_outline_never_covers_pattern_pixels(test_settings):
    """描边零侵占：图案本体像素在描边前后保持不变（灰线只落图案外侧环带）。"""
    from src.steps.outline import OutlineStep

    # 造图：白底 + 中央实心方块图案（边角锐利便于精确断言）
    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    canvas[100:300, 100:300] = (180, 60, 200)

    step = OutlineStep(test_settings)
    result = step.run(canvas)
    assert result.stage_value == "done"

    # 图案本体（原方块区域）像素逐一不变——外环画法画布外扩 18px（第三十二次
    # 修订），内容区平移到 [118:318]
    assert np.array_equal(result.image_bgr[118:318, 118:318], canvas[100:300, 100:300]), \
        "描边覆盖了图案本体像素"

    # 无 shape 回退 rectangle：外环在最外圈 pad 带（y=200 行左缘 x≈0-17）
    row = result.image_bgr[200, :]
    gray_run = 0
    for x in range(0, 40):
        if abs(int(row[x][0]) - test_settings.outline_gray_level) <= 10:
            gray_run += 1
    assert gray_run >= 6, f"圆缘灰线缺失（左缘仅 {gray_run}px）"


def test_outline_threshold_fallback(test_settings):
    """验收 4a：禁用 rembg 后描边仍 done（灰度阈值法兜底），不因分割组件缺失 failed。"""
    from src.steps.outline import OutlineStep

    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    canvas[100:300, 100:300] = (180, 60, 200)

    test_settings.outline_matting_enabled = False
    result = OutlineStep(test_settings).run(canvas)  # 无 shape → 回退 circle
    assert result.stage_value == "done"
    # 图案本体保真（外扩 18px 偏移）；灰线在外环 pad 带（y=200 行左缘）
    assert np.array_equal(result.image_bgr[118:318, 118:318], canvas[100:300, 100:300])
    row = result.image_bgr[200, 0:40, 0].astype(int)
    assert any(abs(int(v) - test_settings.outline_gray_level) <= 10 for v in row), "圆缘灰线缺失"


def test_outline_matting_mask_light_pattern_kept(test_settings):
    """验收 4b（4a 附带）：浅色图案（灰 248 接近背景）阈值法整片丢失、alpha 蒙版法保留。"""
    from src.steps.outline import _threshold_pattern_mask, segment_pattern_mask

    canvas = np.full((300, 300, 3), 255, dtype=np.uint8)
    canvas[80:220, 80:220] = (250, 250, 250)  # 浅色方块（高于阈值法的 245 上限）

    threshold_mask_area = int(np.count_nonzero(_threshold_pattern_mask(canvas)))
    matting_mask_area = int(np.count_nonzero(segment_pattern_mask(canvas, test_settings)))
    assert threshold_mask_area == 0, "前提：阈值法应丢失浅色图案"
    assert matting_mask_area > 0, "alpha 蒙版法应保留浅色图案"


def test_crop_shape_gray_background_skipped(api_client: TestClient):
    """口径（第三十三次修订：白底判定恢复）：灰底照片裁圆不描边——非白底
    边界本来就看得见，外环画法只改"怎么画"不改"画不画"。"""
    gray_canvas = np.full((400, 400, 3), 215, dtype=np.uint8)  # 灰底
    success, buffer = cv2.imencode(".png", gray_canvas)
    assert success
    response = api_client.post(
        "/api/jobs",
        files={"images": ("gray.png", buffer.tobytes(), "image/png")},
        data={"crop_meta": json.dumps({"1": {"shape": "circle", "box": {"width": 400, "height": 400}}})},
    )
    assert response.status_code == 200
    job_status = wait_until_job_completed(api_client, response.json()["job_id"])
    assert job_status["images"][0]["stage_results"]["outline"] == "skipped"


def test_crop_shape_white_background_outlines_shape(api_client: TestClient):
    """口径（图像处理方案 3.5）：白底图 + 裁剪形状 → 沿形状边界描灰线（内接圆环）。"""
    white_canvas = np.full((400, 400, 3), 255, dtype=np.uint8)  # 白底
    # 图案居中偏下、与心形边界（含顶部凹口 y≈134）保持 >40px 边距——图案贴边的图按口径跳过
    cv2.circle(white_canvas, (200, 240), 55, (60, 140, 60), -1)
    success, buffer = cv2.imencode(".png", white_canvas)
    assert success
    response = api_client.post(
        "/api/jobs",
        files={"images": ("white.png", buffer.tobytes(), "image/png")},
        data={"crop_meta": json.dumps({"1": {"shape": "circle", "box": {"width": 400, "height": 400}}})},
    )
    assert response.status_code == 200
    job_status = wait_until_job_completed(api_client, response.json()["job_id"])
    image_status = job_status["images"][0]
    assert image_status["stage_results"]["outline"] == "done", "白底形状裁剪图应沿形状描线"

    download = api_client.get(image_status["result_url"])
    result_bgra = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    # 第三十二次修订外环画法：画布外扩 18px（400→436），圆心 (218,218) r=200，
    # 灰线环在外侧 r∈[200,218)——中线行左端 x∈[0,18) 全灰；内容区零灰线
    gray_line = np.all(np.abs(result_bgra[:, :, :3].astype(int) - 190) <= 10, axis=2)
    assert gray_line[218, 0:16].all(), "圆左缘外侧未见外环灰线"
    assert not gray_line[60:160, 160:260].any(), "内容区出现灰线（线压内容）"
    # 线外全部透明（远角距圆心 >218px）
    assert result_bgra[0:10, 0:10, 3].max() < 10, "线外未透明"
    # 图案本体保真：内容圆（(200,240) r55 原坐标 → (218,258) 外扩坐标）绿色不变
    assert abs(int(result_bgra[258, 218, 0]) - 60) <= 10, "图案本体像素被改动"


def test_crop_shape_square_draws_border_frame(api_client: TestClient):
    """方形裁剪回归（第二十九次修订口径：square=居中内接正方形掩膜，非全图
    直通）——300×300 画布上内接正方形恰为全图，四边有边带灰线且角不外凸；
    square 走"区域−内缩"通式（掩膜非全图后通式产出形状边带）。"""
    white_canvas = np.full((300, 300, 3), 255, dtype=np.uint8)
    cv2.circle(white_canvas, (150, 150), 50, (60, 140, 60), -1)
    response = api_client.post(
        "/api/jobs",
        files={"images": ("sq.png", cv2.imencode(".png", white_canvas)[1].tobytes(), "image/png")},
        data={"crop_meta": json.dumps({"1": {"shape": "square"}})},
    )
    assert response.status_code == 200
    job_status = wait_until_job_completed(api_client, response.json()["job_id"])
    image_status = job_status["images"][0]
    assert image_status["stage_results"]["outline"] == "done"

    download = api_client.get(image_status["result_url"])
    result_bgra = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    # 第三十二次修订：square 掩膜=全图（300 内接）→ 外环=画布外扩 18px 的整圈
    # pad 边框（336 画布，行/列 0:17 与 318:335 全周灰线，含四角连续）
    gray_line = np.all(np.abs(result_bgra[:, :, :3].astype(int) - 190) <= 10, axis=2)
    assert result_bgra.shape[0] == 336, f"画布应外扩到 336，实际 {result_bgra.shape[0]}"
    assert gray_line[0:16, 100:200].all(), "顶环带无灰线"
    assert gray_line[320:336, 100:200].all(), "底环带无灰线"
    assert gray_line[100:200, 0:16].all(), "左环带无灰线"
    assert gray_line[100:200, 320:336].all(), "右环带无灰线"
    # 四角为圆角环（等宽偏移曲线）：外角缺口透明、内角 6px 起环带连续
    assert gray_line[6:16, 6:16].all(), "四角环带应连续（外环整圈，圆角）"
    assert result_bgra[0, 0, 3] == 0, "外环圆角缺口应透明"
    assert not gray_line[120:216, 120:216].any(), "中心区出现灰线"


def test_batch_shape_square_rectfixed_nobox_shapes_image(api_client: TestClient, test_settings):
    """第二十九次修订核心回归：批级无框 square/rectangle-fixed 声明真实塑形
    （用户实锤"长方形和正方形设置不到图案上去"）。第三十六次修订：无框=
    形状默认框（图内居中最大形状包围盒宽高比框，与单独裁剪默认框同几何）。
    400 高×300 宽画布：
    - square：框=300×300 居中（上下各裁 50px）→ 满幅不透明；
    - rectangle-fixed：框=300×200 居中（上下各裁 100px）→ 3:2 满幅不透明。"""
    from src.steps.outline import outline_width_pixels

    ring_pad = 2 * outline_width_pixels(test_settings)  # 描边外环画布外扩（勿硬编码——线宽配置可变）
    for shape_value, box_h in (("square", 300), ("rectangle-fixed", 200)):
        canvas = np.full((400, 300, 3), 255, dtype=np.uint8)
        cv2.circle(canvas, (150, 200), 60, (60, 140, 60), -1)
        response = api_client.post(
            "/api/jobs",
            files={
                "images": (
                    f"{shape_value}.png",
                    cv2.imencode(".png", canvas)[1].tobytes(),
                    "image/png",
                )
            },
            data={"crop_meta": json.dumps({"1": {"shape": shape_value}})},
        )
        assert response.status_code == 200, response.text
        job_status = wait_until_job_completed(api_client, response.json()["job_id"])
        image_status = job_status["images"][0]
        assert image_status["status"] == "completed", image_status.get("error_msg")
        assert image_status["stage_results"]["crop_applied"] == "done"

        download = api_client.get(image_status["result_url"])
        result_bgra = cv2.imdecode(
            np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED
        )
        assert result_bgra.shape[2] == 4, "塑形交付应带 alpha 通道"
        # 输出幅=默认框幅 300×box_h + 描边外环 2×18px（白底 → outline done）；
        # 框比例=形状包围盒比例 → 掩膜撑满框，整幅近满不透明（无补方透明带/
        # 黑边——三十五次补方满幅退役）
        alpha = result_bgra[:, :, 3]
        assert result_bgra.shape[:2] == (box_h + ring_pad, 300 + ring_pad), (
            f"{shape_value} 应交付默认框幅 300x{box_h}+外环 {ring_pad}，实际 {result_bgra.shape[:2]}"
        )
        assert (alpha[30:-30, 30:-30] > 200).all(), f"{shape_value} 框内应满幅不透明"


def test_crop_shape_unknown_falls_back_to_white_gate(api_client: TestClient):
    """畸形/未知形状声明按 rectangle 处理，仍过白底判定：灰底图 → skipped。"""
    from src.steps.outline import OutlineStep

    gray_canvas = np.full((300, 300, 3), 215, dtype=np.uint8)
    result = OutlineStep(_stub_settings()).run(gray_canvas, crop_shape="weird-shape")
    assert result.stage_value == "skipped"


def _stub_settings():
    from src.core.config import PatternToolSettings

    return PatternToolSettings(data_dir="/tmp/pt-shape-test-data", _env_file=None)


def test_shape_outline_cleans_bleed_outside_shape(tmp_path):
    """形状外颜色清洗（第二十一次修订）：放大链渗出边界的图案颜色在描边时
    被洗白——线-边界隔离带恢复，图案不再"绕过"描边线。"""
    from src.core.config import PatternToolSettings
    from src.steps.outline import draw_shape_outline, crop_shape_region_mask

    settings = PatternToolSettings(data_dir=str(tmp_path), _env_file=None,
                                   outline_width_mm=1.2)
    # 白底圆形画布 + 伪造"渗出"：形状边界外紧邻处放深色像素（模拟 LANCZOS 外渗）
    canvas = np.full((400, 400, 4), (255, 255, 255, 255), np.uint8)
    mask = crop_shape_region_mask("circle", 400, 400)
    canvas[:, :, :3][mask] = (60, 150, 90)  # 形状内图案
    # 形状外透明化（crop 塑形语义——alpha 边界即解析形状边界）
    canvas[:, :, 3][~mask] = 0
    # 渗出：形状外 0-6px 环带塞图案色（模拟放大链外渗——alpha 已被 resize
    # 解析重画，但颜色通道的图案渗到了边界外）
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))
    grown = cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)
    bleed_zone = grown & ~mask
    canvas[:, :, :3][bleed_zone] = (70, 140, 100)

    outlined = draw_shape_outline(canvas, "circle", settings)
    # 第三十二次修订：渗出清洗随内缩画法退役——渗出色所在区（形状外）如今
    # 要么被外环灰线覆盖、要么收口为 alpha=0（打印不可见），保护意图由
    # 透明收口承接。中线行：外环之外（右端 alpha 下落沿之后）必须全透明
    row = 100
    alpha = outlined[:, :, 3]
    trans = np.where((alpha[row, :-1] >= 128) & (alpha[row, 1:] < 128))[0]
    assert len(trans), "应有环带外沿"
    c = trans[-1]
    assert (outlined[row, c+1:, 3] == 0).all(), "外环之外未收口透明（渗出可见）"


def test_outer_ring_never_covers_content(test_settings):
    """第三十二次修订核心回归：内容满幅到边（旧净空判定必拒的形态）也画线，
    且内容像素逐一不变——外环在形状外侧，线永不接触内容。"""
    from src.steps.outline import OutlineStep

    # 白底 + 大圆半径 192（边距 8px，满幅形态）：旧内缩画法线压圆边，旧净空
    # 判定直接拒；新外环画法照画且内容零改动
    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (200, 200), 192, (60, 140, 60), -1)
    bgra = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGRA)
    result = OutlineStep(test_settings).run(bgra, "square")
    assert result.stage_value == "done", "外环画法应无条件画线"
    assert result.image_bgr.shape[0] == 436, "画布应外扩一个线宽（400+2×18）"
    # 内容区（外扩偏移 [18:418]）与输入逐一相等——零覆盖铁证
    assert np.array_equal(result.image_bgr[18:418, 18:418, :3], canvas), "外环覆盖了内容像素"
    # 外环在 pad 带上（最外圈 18px 灰线）
    ring = result.image_bgr[200, 0:16, 0].astype(int)
    assert any(abs(int(v) - test_settings.outline_gray_level) <= 10 for v in ring), "外环灰线缺失"


def test_outer_ring_transparent_beyond_line(test_settings):
    """外环之外全部 alpha=0（第三十二次修订第三要点）——沿线剪=线随废料丢弃。"""
    from src.steps.outline import OutlineStep

    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (200, 200), 150, (60, 140, 60), -1)
    bgra = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGRA)
    result = OutlineStep(test_settings).run(bgra, "square")
    assert result.stage_value == "done"
    alpha = result.image_bgr[:, :, 3]
    # square 掩膜=全图 → 外环=整圈 pad 带；环带内 alpha=255（线可打印）
    assert (alpha[0:16, 100:300] == 255).all(), "环带应不透明（灰线本体）"


def _ring_width_px(bgra: np.ndarray) -> int:
    """顶环带宽：中线列从画布顶向内数连续灰线像素。"""
    col = bgra[:, bgra.shape[1] // 2]
    run = best = 0
    for y in range(bgra.shape[0]):
        pixel = col[y]
        b, g, r = int(pixel[0]), int(pixel[1]), int(pixel[2])
        a = int(pixel[3]) if pixel.size == 4 else 255
        ok = abs(b - 190) <= 12 and abs(g - 190) <= 12 and abs(r - 190) <= 12 and a > 200
        run = run + 1 if ok else (0 if run <= 3 else run)
        best = max(best, run)
    return best


def test_batch_outline_width_override(test_settings):
    """批级线宽声明（第三十四次修订）：crop_meta.outline_width_mm 覆盖配置——
    2.0mm → 24px 线宽、画布外扩 2×24；非法值（5.0 超区间）回退配置 1.5mm。"""
    from src.jobs.pipeline import RetouchPipeline
    from src.jobs.store import JobStore

    pipeline = RetouchPipeline(test_settings, JobStore(test_settings))
    canvas = np.full((200, 200, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (100, 100), 60, (60, 140, 60), -1)
    png = cv2.imencode(".png", canvas)[1].tobytes()

    # 合法覆盖：2.0mm @300DPI = 24px（配置默认 1.5mm=18px，覆盖生效可分辨）
    meta = json.dumps({"shape": "square", "outline_width_mm": 2.0})
    result_bytes, _, _ = pipeline._run_steps(png, meta, None)
    img = cv2.imdecode(np.frombuffer(result_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.shape[0] == 200 + 2 * 24, f"2.0mm 外扩应 248，实际 {img.shape[0]}"
    assert _ring_width_px(img) >= 22, f"2.0mm 线宽应 ≈24px，实测 {_ring_width_px(img)}"

    # 非法值（5.0 超 1–2 区间）：回退配置 1.5mm → 18px
    meta_bad = json.dumps({"shape": "square", "outline_width_mm": 5.0})
    result_bad, _, _ = pipeline._run_steps(png, meta_bad, None)
    img_bad = cv2.imdecode(np.frombuffer(result_bad, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img_bad.shape[0] == 200 + 2 * 18, f"非法值应回退 1.5mm（236），实际 {img_bad.shape[0]}"


def test_nobox_shape_defaults_to_max_centered_box(test_settings):
    """第三十六次修订核心回归：无框形状=自动默认框（图内居中最大形状包围盒
    宽高比框）——与带框声明该默认框**逐像素同几何**；宽/长/方图形状区域
    逐像素一致（批统一）。三十五次补方满幅退役（宽图不再补方 400×400）。"""
    from src.steps.crop import CropStep

    # 三张不同宽高比（宽 400x200 / 长 200x400 / 方 200x200），中心放同色标记
    def make_canvas(height: int, width: int) -> np.ndarray:
        canvas = np.full((height, width, 4), (255, 255, 255, 255), np.uint8)
        canvas[height // 2 - 25 : height // 2 + 25, width // 2 - 25 : width // 2 + 25] = (60, 140, 60, 255)
        return canvas

    r_wide = CropStep().run(make_canvas(200, 400), {"shape": "circle"})
    r_tall = CropStep().run(make_canvas(400, 200), {"shape": "circle"})
    r_square = CropStep().run(make_canvas(200, 200), {"shape": "circle"})
    # 输出幅=默认框幅（min 边正方形），非补方 max 边正方形
    assert r_wide.image_bgr.shape[:2] == (200, 200), f"宽图默认框应 200x200，实际 {r_wide.image_bgr.shape[:2]}"
    assert r_tall.image_bgr.shape[:2] == (200, 200), "长图默认框应 200x200"
    assert r_square.image_bgr.shape[:2] == (200, 200), "方图框=整图 200x200"
    # 形状区域（不透明）逐像素一致——批统一铁证（circle 默认框=1:1 → 三图同框同圆）
    a_wide = r_wide.image_bgr[:, :, 3] > 200
    a_tall = r_tall.image_bgr[:, :, 3] > 200
    a_square = r_square.image_bgr[:, :, 3] > 200
    assert np.array_equal(a_wide, a_tall), "宽/长图形状区域应逐像素一致"
    assert np.array_equal(a_wide, a_square), "宽/方图形状区域应逐像素一致"
    # 中心标记保留（默认框居中裁，中心内容不丢）
    assert int(r_wide.image_bgr[100, 100, 0]) == 60, "默认框中心内容丢失"

    # 无框 ≡ 显式声明默认框（同域逐像素一致——统一形状=单独裁剪不动框确认；
    # 五形状全量：对抗验证变异实验证明 heart/star 此前零锁定，删掩膜调用全库仍绿）
    from src.steps.outline import default_shape_box

    for shape_value in ("circle", "square", "rectangle-fixed", "heart", "star"):
        wide = make_canvas(200, 400)
        left, top, box_w, box_h = default_shape_box(400, 200, shape_value)
        framed = CropStep().run(wide, {
            "shape": shape_value,
            "frame": {"width": 400, "height": 200},
            "data": {"x": left, "y": top, "width": box_w, "height": box_h},
        })
        nobox = CropStep().run(wide, {"shape": shape_value})
        assert np.array_equal(framed.image_bgr, nobox.image_bgr), (
            f"{shape_value}: 无框默认框与显式带框声明应同域逐像素一致（统一 ≡ 单独裁剪）"
        )


def test_boxed_shape_crops_to_box(test_settings):
    """带框声明走框裁（第三十六次修订只改无框路径）：框裁等比映射行为不变。"""
    from src.steps.crop import CropStep

    canvas = np.full((200, 400, 4), (255, 255, 255, 255), np.uint8)  # H200×W400（宽图）
    # frame=画布实际尺寸（不触发等比映射），框取左半 200x200
    meta = {"shape": "circle", "frame": {"width": 400, "height": 200},
            "data": {"x": 0, "y": 0, "width": 200, "height": 200}}
    result = CropStep().run(canvas, meta)
    assert result.image_bgr.shape[:2] == (200, 200), f"带框应按框裁 200x200，实际 {result.image_bgr.shape[:2]}"


def test_default_shape_box_geometry(test_settings):
    """default_shape_box（第三十六次修订）：图内居中最大形状包围盒宽高比框
    ——宽高比表与前端 SHAPE_ASPECT_RATIOS 同源（circle/square=1、
    rectangle-fixed=1.5、heart=32/28.9、star=1.902/1.809），改公式两端同改。"""
    from src.steps.outline import default_shape_box

    # 宽图 400×200：受高约束 → 框高=200，框宽=round(200×a)
    assert default_shape_box(400, 200, "circle") == (100, 0, 200, 200)
    assert default_shape_box(400, 200, "square") == (100, 0, 200, 200)
    assert default_shape_box(400, 200, "rectangle-fixed") == (50, 0, 300, 200)
    assert default_shape_box(400, 200, "heart") == (89, 0, 221, 200)      # round(200×32/28.9)=221
    assert default_shape_box(400, 200, "star") == (95, 0, 210, 200)       # round(200×1.902/1.809)=210
    # 长图 200×400：受宽约束 → 框宽=200，框高=round(200/a)
    assert default_shape_box(200, 400, "circle") == (0, 100, 200, 200)
    assert default_shape_box(200, 400, "rectangle-fixed") == (0, 133, 200, 133)  # round(200/1.5)=133
    # 方图 300×300：heart 高受约束 → 300×271 居中（非整图）
    assert default_shape_box(300, 300, "heart") == (0, 14, 300, 271)      # round(300×28.9/32)=271
    # rectangle/free 无默认框语义：整图
    assert default_shape_box(400, 200, "rectangle") == (0, 0, 400, 200)
    assert default_shape_box(400, 200, "free") == (0, 0, 400, 200)
    # 取整口径锁（第三十六次修订补）：半上取整对齐前端 Math.round——
    # 1003×1.5=1504.5 精确落点，Python 银行家舍入会给 1504 与 JS 差 1px
    assert default_shape_box(4000, 1003, "rectangle-fixed") == (1247, 0, 1505, 1003)


def test_nobox_heart_star_pixels(test_settings):
    """无框 heart/star 塑形像素锁（第三十六次修订补——对抗验证变异实验：
    跳过 heart/star 掩膜调用全库测试仍绿）。锁输出幅=默认框幅 + 不透明区
    与 crop_shape_region_mask 同形（心形凹口/星形凹角必须透明）。"""
    from src.steps.crop import CropStep
    from src.steps.outline import crop_shape_region_mask

    for shape_value, expected_w in (("heart", 221), ("star", 210)):
        canvas = np.full((200, 400, 4), (255, 255, 255, 255), np.uint8)
        result = CropStep().run(canvas, {"shape": shape_value})
        height, width = result.image_bgr.shape[:2]
        # 默认框幅（200×宽高比：heart 32/28.9→221、star 1.902/1.809→210）
        assert (height, width) == (200, expected_w), (
            f"{shape_value} 应交付默认框幅 {expected_w}x200，实际 {width}x{height}"
        )
        opaque = result.image_bgr[:, :, 3] > 200
        ratio = float(opaque.mean())
        assert 0.3 < ratio < 0.9, f"{shape_value} 不透明占比 {ratio:.3f} 异常（掩膜未生效？）"
        # 与掩膜同形（羽化 σ0.8 容差 ±2px）：不透明区 ⊆ 掩膜外扩、掩膜内核 ⊆ 不透明区
        mask = crop_shape_region_mask(shape_value, width, height)
        grown = cv2.dilate(mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        shrunk = cv2.erode(
            mask.astype(np.uint8), np.ones((5, 5), np.uint8),
            borderType=cv2.BORDER_CONSTANT, borderValue=0,
        ) > 0
        assert not (opaque & ~grown).any(), f"{shape_value} 不透明区越出掩膜（+2px）"
        assert not (shrunk & ~opaque).any(), f"{shape_value} 掩膜内核（−2px）出现透明洞"


def test_shape_mask_fills_default_box(test_settings):
    """掩膜与宽高比表耦合锁（第三十六次修订补——掩膜几何常量漂移曾零锁定）：
    在默认框幅画布上各形状掩膜包围盒须撑满画布两向（框比例=形状包围盒比例
    的兑现——「形状撑满框零空边」）。"""
    from src.steps.outline import SHAPE_BOX_ASPECT_RATIOS, crop_shape_region_mask, default_shape_box

    for shape_value in SHAPE_BOX_ASPECT_RATIOS:
        _, _, box_w, box_h = default_shape_box(1000, 700, shape_value)
        mask = crop_shape_region_mask(shape_value, box_w, box_h)
        ys, xs = np.where(mask)
        assert xs.min() <= box_w * 0.01 and (box_w - 1 - xs.max()) <= box_w * 0.01, (
            f"{shape_value} 掩膜未横向撑满默认框"
        )
        assert ys.min() <= box_h * 0.01 and (box_h - 1 - ys.max()) <= box_h * 0.01, (
            f"{shape_value} 掩膜未纵向撑满默认框"
        )


def test_nobox_vs_framed_after_resize_tolerant(test_settings):
    """frame≠画布（resize 先行）重映射路径等价锁界（第三十六次修订补——对抗
    验证发现取整链分叉）：无框（当前幅重算）与弹层默认框（原域框×缩放倍率
    int() 截断映射，既有代码）交付幅差 ≤1px、不透明区域一致率 ≥99%——
    「统一≡单独裁剪」在缩放链路的实测界（300DPI 下 1px≈0.08mm 不可见）。"""
    from src.steps.crop import CropStep
    from src.steps.outline import default_shape_box

    src_height, src_width = 3000, 1000  # 长图（对抗验证复现例）
    canvas = np.full((src_height, src_width, 4), (255, 255, 255, 255), np.uint8)
    # 模拟 5cm 档本地 INTER_AREA 缩放后幅（短边 1000→591）
    resized = cv2.resize(canvas, (591, 1773), interpolation=cv2.INTER_AREA)
    nobox = CropStep().run(resized, {"shape": "heart"}).image_bgr
    left, top, box_w, box_h = default_shape_box(src_width, src_height, "heart")
    framed = CropStep().run(resized, {
        "shape": "heart",
        "frame": {"width": src_width, "height": src_height},
        "data": {"x": left, "y": top, "width": box_w, "height": box_h},
    }).image_bgr
    assert abs(nobox.shape[0] - framed.shape[0]) <= 1 and abs(nobox.shape[1] - framed.shape[1]) <= 1, (
        f"重映射路径幅差超界：{nobox.shape[:2]} vs {framed.shape[:2]}"
    )
    h_min = min(nobox.shape[0], framed.shape[0])
    w_min = min(nobox.shape[1], framed.shape[1])
    agreement = float(((nobox[:h_min, :w_min, 3] > 200) == (framed[:h_min, :w_min, 3] > 200)).mean())
    assert agreement >= 0.99, f"重映射路径不透明区一致率 {agreement:.4f} < 0.99"
