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


def test_crop_shape_gray_background_outer_ring(api_client: TestClient):
    """第三十二次修订：外边缘描边无条件执行——灰底照片裁圆同样出外环线
    （线环在形状外侧透明区上，永不接触内容；白底判定门随内缩画法退役）。"""
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
    assert job_status["images"][0]["stage_results"]["outline"] == "done"


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


def test_batch_shape_square_rectfixed_nobox_shapes_image(api_client: TestClient):
    """第二十九次修订核心回归：批级无框 square/rectangle-fixed 声明真实塑形
    （用户实锤"长方形和正方形设置不到图案上去"——旧矩形类一律整图直通，
    实测输出 0 像素变化）。400 高×300 宽画布：
    - square：min 边=300 → 居中 300×300 正方形，上下各 ~50px 透明带；
    - rectangle-fixed：3:2 横向最大化=300×200 居中，上下各 ~100px 透明带。"""
    for shape_value in ("square", "rectangle-fixed"):
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
        alpha = result_bgra[:, :, 3]
        if shape_value == "square":
            # 居中 300×300：行 50-349 不透明，顶/底各 50px 透明（留 5px 容差）
            assert (alpha[:45, :] < 10).all(), "square 顶部未透明（未按 min 边居中塑形）"
            assert (alpha[-45:, :] < 10).all(), "square 底部未透明"
            assert (alpha[100:300, 100:200] == 255).all(), "square 中心区应不透明"
        else:
            # 3:2 → 300×200 居中：行 100-299 不透明，顶/底各 100px 透明
            assert (alpha[:95, :] < 10).all(), "rectangle-fixed 顶部未透明（3:2 未生效）"
            assert (alpha[-95:, :] < 10).all(), "rectangle-fixed 底部未透明"
            assert (alpha[150:250, 50:250] == 255).all(), "rectangle-fixed 中心区应不透明"


def test_crop_shape_unknown_falls_back_to_rectangle_outer_ring(api_client: TestClient):
    """畸形/未知形状声明按 rectangle 处理（第三十二次修订）：无条件外环整圈。"""
    from src.steps.outline import OutlineStep

    gray_canvas = np.full((300, 300, 3), 215, dtype=np.uint8)
    result = OutlineStep(_stub_settings()).run(gray_canvas, crop_shape="weird-shape")
    assert result.stage_value == "done"
    # 3 通道直调 → 白底合成回显，外环整圈可见（300+36=336）
    assert result.image_bgr.shape[0] == 336
    row = result.image_bgr[168, 0:16, 0].astype(int)
    assert any(abs(int(v) - _stub_settings().outline_gray_level) <= 10 for v in row), "外环灰线缺失"


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
