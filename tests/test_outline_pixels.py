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
    # 不裁形：无透明区（原默认圆形四角透明已撤销）
    if result_bgr.ndim == 3 and result_bgr.shape[2] == 4:
        assert result_bgr[:, :, 3].min() == 255, "默认矩形不应有透明区"
    gray_line = np.all(np.abs(result_bgr[:, :, :3].astype(int) - 190) <= 10, axis=2)
    # 整图边框（线宽 1.5mm ≈ 18px @300DPI，内缩一个线宽起笔：行 18–36）
    assert gray_line[18:36, 300].any(), "顶边框无灰线"
    assert gray_line[564:582, 300].any(), "底边框无灰线"
    assert not gray_line[200, 200], "画布中央出现灰线（非边框描边）"


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

    # 图案本体（原方块区域）像素逐一不变
    assert np.array_equal(result.image_bgr[100:300, 100:300], canvas[100:300, 100:300]), \
        "描边覆盖了图案本体像素"

    # （2026-08-25 形状必填）无 shape 回退 circle：线在整图内接圆缘（y=200 行
    # 圆左缘 x≈0-18），不在方块外侧——图案本体仍零覆盖
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
    # 图案本体保真；灰线在整图圆缘（y=200 行左缘）
    assert np.array_equal(result.image_bgr[100:300, 100:300], canvas[100:300, 100:300])
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
    """口径：形状裁剪图也要求白底——灰底照片裁圆不描边（边界看得见，无需补线）。"""
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
    result_bgr = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    gray_line = np.all(np.abs(result_bgr.astype(int) - 190) <= 10, axis=2)
    center_row = gray_line[200, :]
    circle_edge_left = np.nonzero(center_row[:200])[0]
    assert len(circle_edge_left) > 0, "圆左缘未见灰线"
    # 线带起画 = 内接圆缘 + 羽化补偿 2px（第二十五次修订：resize 形状 alpha
    # 羽化带吃线，起画内缩线宽+2px 厚度画足）——灰线延伸到 ≈12+2px 处
    assert abs(circle_edge_left.max() - 14) <= 6, f"灰线应贴内接圆左缘内侧（≈14px 含羽化补偿），实际 {circle_edge_left.max()}"
    corner = 20
    assert not gray_line[:corner, :corner].any(), "画布角出现灰线，疑似矩形描边"


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
    result_bgr = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    gray_line = np.all(np.abs(result_bgr.astype(int) - 190) <= 10, axis=2)
    # 四条边带都有灰线（边框带构造：内缩一个线宽起笔 18px，带宽 18px→行 18-35）
    assert gray_line[18:34, 100:200].any(axis=1).all(), "顶边带无灰线"
    assert gray_line[-34:-18, 100:200].any(axis=1).all(), "底边带无灰线"
    assert gray_line[100:200, 18:34].any(axis=0).all(), "左边带无灰线"
    assert gray_line[100:200, -34:-18].any(axis=0).all(), "右边带无灰线"
    # 角不外凸；画布最外缘一圈无灰线（防打印裁边）；中心区无灰线
    assert not gray_line[0:17, 0:17].any(), "左上角外凸"
    assert not gray_line[0, :].any() and not gray_line[-1, :].any(), "灰线贴死画布上下边"
    assert not gray_line[:, 0].any() and not gray_line[:, -1].any(), "灰线贴死画布左右边"
    assert not gray_line[100:200, 100:200].any(), "中心区出现灰线"


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


def test_crop_shape_unknown_falls_back_to_white_gate(api_client: TestClient):
    """畸形/未知形状声明按未裁剪处理：灰底图无 crop_meta 等效路径 → outline=skipped。"""
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
    gray = cv2.cvtColor(outlined[:, :, :3], cv2.COLOR_BGR2GRAY)
    # 清洗后：线带外沿与形状边界之间应为白（渗出色已被洗掉）
    # 心形同理；此处 circle：取中线行验证边界附近
    row = 100  # 非直径行：左右侧弧有真实边界（直径行贯穿全幅无下落沿）
    alpha = outlined[:, :, 3]
    # 直径行：右边界（形状内→形状外的下落沿）
    trans = np.where((alpha[row, :-1] >= 128) & (alpha[row, 1:] < 128))[0]
    assert len(trans), "应有形状边界"
    c = trans[-1]
    # 边界外侧 1-5px（原渗出区）：渗出色必须被洗白（alpha 已透明，颜色值本身干净）
    outside_pixels = outlined[row, c+1:c+6, :3]
    assert (outside_pixels >= 248).all(), f"边界外渗出色未清洗: {outside_pixels[:, 0].tolist()}"


def test_line_band_not_clear_skips_outline(test_settings):
    """线带净空判定（第三十一次修订）：内容越线（大圆触边，压住线带）时
    不画线——沿线剪会剪掉图案，裁切参考线语义失效。直接调 OutlineStep。"""
    from src.steps.outline import OutlineStep

    # 白底 + 居中大圆：半径 192（画布 400，边距 8px < 线宽内缩 14px 起）——
    # 圆边压进矩形线带（inset 14..28px 行列带）→ 旧白底判定通过（剔图案后
    # 背景白）、新净空判定必须拒绝
    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (200, 200), 192, (60, 140, 60), -1)
    bgra = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGRA)
    result = OutlineStep(test_settings).run(bgra, "square")
    assert result.stage_value == "skipped", "内容压线带必须 skipped（裁切线不可越内容）"


def test_line_band_clear_small_pattern_draws(test_settings):
    """对照组：内容离边 ≥40px（线带净空）时正常画线——净空判定不误杀。"""
    from src.steps.outline import OutlineStep

    canvas = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (200, 200), 150, (60, 140, 60), -1)
    bgra = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGRA)
    result = OutlineStep(test_settings).run(bgra, "square")
    assert result.stage_value == "done", "线带净空（内容不压线）应正常画线"
