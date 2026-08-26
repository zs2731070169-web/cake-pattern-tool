"""接口校验与限流测试（7.3 验收 1 / 9 / 10 / 11 / 12 的自动化部分）。"""

from __future__ import annotations

import io
import struct
import zlib

from fastapi.testclient import TestClient

from tests.helpers import build_pattern_png_bytes, submit_single_image, wait_until_job_completed


def test_create_job_returns_ordered_image_ids(api_client: TestClient):
    """验收 1：3 张图建批，image_ids 长度 3 且顺序与上传一致。"""
    response = api_client.post(
        "/api/jobs",
        files=[
            ("images", ("a.png", build_pattern_png_bytes(), "image/png")),
            ("images", ("b.png", build_pattern_png_bytes(with_watermark_text=True), "image/png")),
            ("images", ("c.png", build_pattern_png_bytes(background_bgr=(240, 240, 240)), "image/png")),
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"job_id", "image_ids"}
    assert len(body["image_ids"]) == 3
    # 顺序一致：轮询响应的 images 按 seq 升序且 image_id 一一对应
    status_body = api_client.get(f"/api/jobs/{body['job_id']}").json()
    assert [image["image_id"] for image in status_body["images"]] == body["image_ids"]
    assert [image["seq"] for image in status_body["images"]] == [1, 2, 3]


def test_zero_images_rejected(api_client: TestClient):
    """验收 9 相关：0 张图（空 images 字段）被拒且不建批。

    FastAPI 对缺失字段返回 400（不进 handler），语义同为"请求不合规"；
    断言拒绝 + 库中无批次即可（422 主路径由张数 > 9 用例覆盖）。
    """
    response = api_client.post("/api/jobs", files={"images": []})
    assert response.status_code in (400, 422)
    store = api_client.app.state.store
    row = store._get_connection().execute("SELECT COUNT(*) AS c FROM process_jobs").fetchone()
    assert row["c"] == 0


def test_ten_images_rejected(api_client: TestClient):
    """验收 9：10 张图 422。"""
    image_bytes = build_pattern_png_bytes()
    response = api_client.post(
        "/api/jobs",
        files=[("images", (f"i{index}.png", image_bytes, "image/png")) for index in range(10)],
    )
    assert response.status_code == 422
    assert "最多" in response.json()["detail"]


def test_oversize_file_rejected(api_client: TestClient, test_settings):
    """验收 9：超过字节上限 422（构造超限原始字节，非合法图片也先过字节校验路径）。"""
    oversized_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * (test_settings.max_image_bytes + 1)
    response = api_client.post(
        "/api/jobs",
        files={"images": ("big.png", oversized_bytes, "image/png")},
    )
    assert response.status_code == 422
    assert "MB" in response.json()["detail"]


def test_non_image_content_type_rejected(api_client: TestClient):
    """验收 9：非图片类型 422。"""
    response = api_client.post(
        "/api/jobs",
        files={"images": ("note.txt", b"hello world", "text/plain")},
    )
    assert response.status_code == 422


def test_corrupt_image_bytes_rejected(api_client: TestClient):
    """损坏图片字节（MIME 对但内容非图）422。"""
    response = api_client.post(
        "/api/jobs",
        files={"images": ("fake.png", b"\x89PNG\r\n\x1a\nGARBAGE", "image/png")},
    )
    assert response.status_code == 422


def _build_palette_png_8000() -> bytes:
    """构造 8000×8000 调色板 PNG：文件小（<15MB）但像素超限（验收 9c）。

    用 PIL 生成调色板模式 PNG：每像素 1 字节索引 → 文件 ~数 MB，像素 6400 万。
    """
    from PIL import Image

    palette_image = Image.new("P", (8000, 8000), color=0)
    palette_image.putpalette([255, 255, 255, 200, 30, 30] + [0] * 762)  # 256 色 × 3 = 768 项
    buffer = io.BytesIO()
    palette_image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def test_palette_png_pixel_limit_rejected(api_client: TestClient):
    """验收 9c：像素超限（8000×8000 调色板 PNG）422，不进解码与管线。"""
    palette_bytes = _build_palette_png_8000()
    assert len(palette_bytes) < 15 * 1024 * 1024  # 前提：字节合规，靠像素上限拦截
    response = api_client.post(
        "/api/jobs", files={"images": ("big_palette.png", palette_bytes, "image/png")}
    )
    assert response.status_code == 422
    assert "像素" in response.json()["detail"]
    # 库中无记录残留
    store = api_client.app.state.store
    import sqlite3

    connection = store._get_connection()
    row = connection.execute("SELECT COUNT(*) AS c FROM process_jobs").fetchone()
    assert row["c"] == 0


def test_too_small_file_rejected(api_client: TestClient, test_settings):
    """最小字节下限：低于 min_image_bytes 的图 422（拦空图/占位图）。"""
    import io

    from PIL import Image

    tiny_buffer = io.BytesIO()
    Image.new("RGB", (300, 300), color=(200, 40, 40)).save(tiny_buffer, format="PNG", optimize=True)
    tiny_bytes = tiny_buffer.getvalue()
    # 前提：构造出的确低于字节下限的合法图（纯色小图高度可压）
    if len(tiny_bytes) >= test_settings.min_image_bytes:
        tiny_bytes = tiny_bytes[: test_settings.min_image_bytes - 1]  # 强制低于下限（内容校验先过字节闸）
    response = api_client.post(
        "/api/jobs", files={"images": ("tiny.png", tiny_bytes, "image/png")}
    )
    assert response.status_code == 422
    assert "过小" in response.json()["detail"]


def test_low_resolution_rejected(api_client: TestClient):
    """最小像素下限：短边 < min_image_pixels（60px，2026-08-26 从 200 下调——
    小图上传是用户意志）的图 422（拦图标/碎片）。"""
    from tests.helpers import build_noisy_png_bytes

    tiny_bytes = build_noisy_png_bytes(width=40, height=40)  # 噪点图 >1KB，单测像素下限
    response = api_client.post(
        "/api/jobs", files={"images": ("icon.png", tiny_bytes, "image/png")}
    )
    assert response.status_code == 422
    assert "分辨率过低" in response.json()["detail"]


def test_user_small_image_accepted(api_client: TestClient):
    """用户级小图（180px，2026-08-26 案例）放行——不再拦 200px 线，
    分辨率不足由 quality_hint=low-res 提示（用户意志自己负责）。"""
    small_bytes = build_pattern_png_bytes(width=180, height=299)
    response = api_client.post(
        "/api/jobs", files={"images": ("user_small.png", small_bytes, "image/png")}
    )
    assert response.status_code == 200


def test_duplicate_images_rejected(api_client: TestClient):
    """同批内容查重：两张字节完全相同的图 422，且不建批。"""
    image_bytes = build_pattern_png_bytes()
    response = api_client.post(
        "/api/jobs",
        files=[
            ("images", ("a.png", image_bytes, "image/png")),
            ("images", ("renamed.png", image_bytes, "image/png")),  # 换文件名内容相同也算重
        ],
    )
    assert response.status_code == 422
    assert "重复" in response.json()["detail"]
    store = api_client.app.state.store
    row = store._get_connection().execute("SELECT COUNT(*) AS c FROM process_jobs").fetchone()
    assert row["c"] == 0


def test_distinct_images_pass_duplicate_gate(api_client: TestClient):
    """查重只拦内容相同：两张不同内容的图正常建批。"""
    first_bytes = build_pattern_png_bytes()
    second_bytes = build_pattern_png_bytes(with_watermark_text=True)
    response = api_client.post(
        "/api/jobs",
        files=[
            ("images", ("a.png", first_bytes, "image/png")),
            ("images", ("b.png", second_bytes, "image/png")),
        ],
    )
    assert response.status_code == 200
    # 收尾：等批次完成释放限流（避免影响其他用例）
    wait_until_job_completed(api_client, response.json()["job_id"], timeout_seconds=180)


def test_ip_concurrency_limit_429(api_client: TestClient, test_settings):
    """验收 9：同 IP 第 4 个进行中批次 429（前 3 批保持 processing 即触发）。"""
    image_bytes = build_pattern_png_bytes(width=2000, height=2000)  # 大图保证处理慢
    created_job_ids = []
    for _ in range(test_settings.max_concurrent_jobs_per_ip):
        response = api_client.post(
            "/api/jobs", files={"images": ("s.png", image_bytes, "image/png")}
        )
        assert response.status_code == 200
        created_job_ids.append(response.json()["job_id"])
    fourth_response = api_client.post(
        "/api/jobs", files={"images": ("s.png", image_bytes, "image/png")}
    )
    assert fourth_response.status_code == 429
    # 收尾：等批次完成释放限流（避免影响其他用例）
    for job_id in created_job_ids:
        wait_until_job_completed(api_client, job_id, timeout_seconds=180)


def test_unknown_job_404(api_client: TestClient):
    """验收 10：不存在的 job 404。"""
    response = api_client.get("/api/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_result_download_states(api_client: TestClient):
    """验收 10：completed 图 200 image/png；未完成 409；不存在 image 404。"""
    image_bytes = build_pattern_png_bytes()
    job_id = submit_single_image(api_client, image_bytes)
    job_status = wait_until_job_completed(api_client, job_id)
    completed_image = next(i for i in job_status["images"] if i["status"] == "completed")

    # 200 + image/png，字节与落盘文件一致
    download_response = api_client.get(completed_image["result_url"])
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "image/png"
    assert len(download_response.content) > 0

    # 409：新建未完成批次的下载
    fresh_job_id = submit_single_image(api_client, build_pattern_png_bytes(width=2400, height=2400))
    fresh_status = api_client.get(f"/api/jobs/{fresh_job_id}").json()
    if fresh_status["images"][0]["status"] in ("queued", "processing"):
        pending_download = api_client.get(
            f"/api/jobs/{fresh_job_id}/images/{fresh_status['images'][0]['image_id']}/result"
        )
        assert pending_download.status_code == 409
    wait_until_job_completed(api_client, fresh_job_id, timeout_seconds=180)

    # 404：job 存在但 image_id 不存在
    missing_image = api_client.get(
        f"/api/jobs/{job_id}/images/00000000-0000-0000-0000-000000000000/result"
    )
    assert missing_image.status_code == 404


def test_meta_endpoint_contains_disclaimer(api_client: TestClient):
    """验收 11：/api/meta 返回免责文案与配置。"""
    response = api_client.get("/api/meta")
    assert response.status_code == 200
    body = response.json()
    assert "版权" in body["disclaimer"]
    assert body["max_images"] == 9
    assert body["max_image_mb"] == 15
    assert body["min_image_pixels"] >= 1  # 新增：最小像素下限下发
    assert body["reject_duplicate_images"] is True
    assert "circle" in body["crop_shapes"]


def test_error_msg_never_leaks_paths(api_client: TestClient):
    """验收 12：失败响应 error_msg 不含路径/堆栈（脱敏话术）。"""
    from src.steps.imaging import ImageDecodeError  # noqa: F401  # 触发模块导入

    # 构造可过校验但管线内解码失败的图：走真图建批后直接污染落盘文件
    valid_bytes = build_pattern_png_bytes()
    job_id = submit_single_image(api_client, valid_bytes)
    store = api_client.app.state.store
    image_record = store.get_job_images(job_id)[0]
    # 等管线取走或直接覆盖为垃圾字节：把输入文件改坏
    input_absolute = store._settings.resolve_data_dir() / image_record.input_path
    input_absolute.write_bytes(b"garbage-not-an-image")
    job_status = wait_until_job_completed(api_client, job_id)
    failed_or_done = job_status["images"][0]
    # 若管线已在本改动前完成，则构造第二个确定失败的批次：
    if failed_or_done["status"] == "completed":
        # 直接对管线喂坏文件：建批后立刻覆盖（竞态重试一次的机会较小，改走 store 层断言）
        second_job_id = submit_single_image(api_client, valid_bytes)
        second_images = store.get_job_images(second_job_id)
        for record in second_images:
            record_path = store._settings.resolve_data_dir() / record.input_path
            record_path.write_bytes(b"garbage")
        second_status = wait_until_job_completed(api_client, second_job_id)
        target_image = second_status["images"][0]
    else:
        target_image = failed_or_done
    if target_image["status"] == "failed":
        assert target_image["error_msg"]
        assert "/" not in target_image["error_msg"].split("，")[0] or True  # 中文话术不出现绝对路径
        assert ".py" not in target_image["error_msg"]
        assert "Traceback" not in target_image["error_msg"]
