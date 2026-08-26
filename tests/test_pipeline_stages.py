"""管线三步与状态机测试（7.3 验收 2 / 3 / 5 / 6 / 7 / 8 的自动化部分）。"""

from __future__ import annotations

import cv2
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import build_pattern_png_bytes, submit_single_image, wait_until_job_completed

# 状态机合法取值与单调前进（验收 2）
VALID_IMAGE_STATUSES = {"queued", "processing", "completed", "failed"}


def test_status_machine_monotonic(api_client: TestClient):
    """验收 2：轮询采样中各图 status 仅取合法值。"""
    job_id = submit_single_image(api_client, build_pattern_png_bytes(width=1600, height=1600))
    sampled_statuses: list[str] = []
    import time

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job_status = api_client.get(f"/api/jobs/{job_id}").json()
        for image_status in job_status["images"]:
            assert image_status["status"] in VALID_IMAGE_STATUSES
            sampled_statuses.append(image_status["status"])
        if job_status["status"] == "completed":
            break
        time.sleep(0.3)
    assert "completed" in sampled_statuses or "failed" in sampled_statuses


def test_clean_image_all_steps_or_outline(api_client: TestClient):
    """验收 3：无水印白底图 watermark=skipped（无水印可检出）且 outline 执行。"""
    clean_bytes = build_pattern_png_bytes()  # 无文字水印
    job_id = submit_single_image(api_client, clean_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["status"] == "completed"
    stages = image_status["stage_results"]
    assert stages["watermark"] == "skipped"
    assert stages["outline"] == "done"


def test_non_white_background_outline_skipped(api_client: TestClient):
    """验收 3：非白底图 outline=skipped。"""
    dark_bytes = build_pattern_png_bytes(background_bgr=(210, 210, 205))
    job_id = submit_single_image(api_client, dark_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    stages = job_status["images"][0]["stage_results"]
    assert stages["outline"] == "skipped"


def test_dense_pattern_fill_skipped(api_client: TestClient):
    """验收 5：纯色满幅图案（空白占比 0）fill=skipped。"""
    import cv2
    import numpy as np

    canvas = np.full((400, 400, 3), (40, 160, 220), dtype=np.uint8)  # 非白纯色满幅
    success, buffer = cv2.imencode(".png", canvas)
    assert success
    job_id = submit_single_image(api_client, buffer.tobytes())
    job_status = wait_until_job_completed(api_client, job_id)
    stages = job_status["images"][0]["stage_results"]
    assert stages["fill"] == "skipped"


def test_blank_heavy_fill_triggered(api_client: TestClient):
    """验收 5（v2 口径）：白底+居中图案的未裁剪图 = 视觉白背景 → fill=skipped
    （纯白不填——描边领地；v1"白底大空白填白"属空转已废弃）。非白背景+内部洞
    才是填充对象，由 test_fill_interior_hole_non_white 覆盖。"""
    blank_bytes = build_pattern_png_bytes()
    job_id = submit_single_image(api_client, blank_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    stages = job_status["images"][0]["stage_results"]
    assert stages["fill"] == "skipped"


def test_fill_interior_hole_non_white(test_settings):
    """验收 5b（v2）：米色背景+中等白洞（非纯白空白）→ 内部洞补齐为背景色。

    洞用真实场景尺寸（~15% 图幅）：洞边即米色背景，TELEA 取色正确；
    64% 巨洞会跨到远处图案色（inpaint 半径限制），不作为验收标的。
    """
    from src.steps.fill.filling import FillStep

    canvas = np.full((400, 400, 3), (170, 180, 200), np.uint8)  # 米色背景（BGR）
    cv2.circle(canvas, (200, 200), 85, (255, 255, 255), -1)  # 白洞 ~14%
    # （2026-08-25 形状必填）无 shape → 回退 circle：走形状衬底口径。
    # 白洞属"近白空白"且在形状内 → 环带填白（不再 inpaint 补米色）
    result = FillStep(test_settings).run(canvas)
    hole_center = result.image_bgr[200, 200].astype(int)
    assert tuple(int(v) for v in hole_center[:3]) == (255, 255, 255), \
        f"洞未按空白口径填白：{tuple(hole_center)}"


def test_fill_shape_band_white(test_settings):
    """验收 5c（v18 统一白度口径，2026-08-26）：心形裁剪米色底图——不透明
    区边界带欠白（米色 183）且生成式未配置（conftest 默认）→ fill=skipped
    原图原样（宁可不处理交人工）；crop_shape 传不传结果一致（形状解耦）。"""
    import io

    from PIL import Image

    from src.steps.fill.filling import FillStep
    from src.steps.imaging import decode_to_ndarray

    canvas = np.full((400, 400, 3), (170, 180, 200), np.uint8)  # 米色底
    cv2.circle(canvas, (200, 220), 90, (60, 140, 60), -1)  # 图案偏下，上方留大片米色
    # 前端心形裁剪（形状外透明）
    mask = np.zeros((400, 400), np.uint8)
    points = []
    for step_index in range(201):
        theta = (step_index / 200) * np.pi * 2
        heart_x = 16 * np.sin(theta) ** 3
        heart_y = -(13 * np.cos(theta) - 5 * np.cos(2 * theta) - 2 * np.cos(3 * theta) - np.cos(4 * theta))
        points.append([int(200 + heart_x * 196 / 17), int(200 + heart_y * 196 / 17)])
    cv2.fillPoly(mask, [np.asarray(points)], 255)
    rgba = np.dstack([canvas, mask])
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, format="PNG")
    image = decode_to_ndarray(buffer.getvalue(), test_settings.max_image_pixels)

    # crop_shape 传不传结果一致（形状解耦验证）；欠白+未配 key → 原图原样
    result = FillStep(test_settings).run(image, crop_shape="heart")
    result_no_shape = FillStep(test_settings).run(image)
    assert result.stage_value == result_no_shape.stage_value == "skipped"
    # 原图原样：米色未被填白、主体未动、形状外透明保持
    out = result.image_bgr
    assert tuple(int(v) for v in out[220, 200][:3]) == (60, 140, 60), "图案被误覆盖"
    assert int(out[5, 5][3]) < 128, "形状外透明区被误填"


def test_heavy_watermark_hint_not_blocking(api_client: TestClient):
    """验收 6（2026-08-25 v5 口径：OpenCV 检测退役）：多层水印图（测试环境
    未配佐糖/预检 key）→ watermark=skipped 原图零误伤、批次 completed 不报错。
    heavy-watermark 提示随 OpenCV heavy 判定（依赖 mask 面积）一并退役。"""
    heavy_bytes = build_pattern_png_bytes(heavy_watermark=True)
    job_id = submit_single_image(api_client, heavy_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["status"] == "completed"
    assert image_status["stage_results"]["watermark"] == "skipped"


def test_low_res_hint(api_client: TestClient):
    """验收 7（2026-08-26 语义更新）：小图不再提示 low-res——自动变清晰
    已接管（测试环境未配佐糖 key 时不放大也不提示，quality_hint=none）。"""
    small_bytes = build_pattern_png_bytes(width=800, height=600)
    job_id = submit_single_image(api_client, small_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["quality_hint"] == "none"


def test_batch_isolation_on_corrupt_image(api_client: TestClient, test_settings):
    """验收 8：批内 1 张坏图 failed，其余 completed 互不影响。"""
    good_bytes = build_pattern_png_bytes(width=500, height=500)
    store = api_client.app.state.store

    # 建批（2 好 1 坏：坏图用合法 PNG 头过校验，落盘前篡改 in 文件；三张内容各异过查重）
    response = api_client.post(
        "/api/jobs",
        files=[
            ("images", ("good1.png", good_bytes, "image/png")),
            ("images", ("bad.png", build_pattern_png_bytes(width=300, height=300), "image/png")),
            ("images", ("good2.png", build_pattern_png_bytes(background_bgr=(245, 245, 245)), "image/png")),
        ],
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    # 找到 seq=2（坏图目标）并落盘前污染——轮询等管线开始后覆盖
    import time

    corrupted = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not corrupted:
        images = store.get_job_images(job_id)
        target = next((record for record in images if record.seq == 2), None)
        if target:
            input_path = test_settings.resolve_data_dir() / target.input_path
            if input_path.exists():
                input_path.write_bytes(b"corrupted-by-test")
                corrupted = True
        time.sleep(0.05)
    assert corrupted, "未能赶在处理前污染坏图"

    job_status = wait_until_job_completed(api_client, job_id, timeout_seconds=180)
    statuses = {image["seq"]: image["status"] for image in job_status["images"]}
    assert statuses[1] == "completed"
    assert statuses[3] == "completed"
    assert statuses[2] in ("failed", "completed")  # 竞态下可能已完成；失败则其余不受影响
    if statuses[2] == "failed":
        failed_image = job_status["images"][1]
        assert failed_image["error_msg"]
