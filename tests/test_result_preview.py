"""成品预览缩略图回归（第四十次修订）——回显不拉全幅。

三个性质：
1. 状态接口 result_width/height = 成品真实宽高（PNG IHDR 头直读）；
2. ?preview=1 交付最长边 ≤512px 且保 alpha（缩略可辨），全幅端点不受影响；
3. 预览缓存复用（二次请求同内容）。
"""

from __future__ import annotations

import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import build_pattern_png_bytes, wait_until_job_completed


def _submit_single(api_client: TestClient, image_bytes: bytes, meta: dict) -> dict:
    response = api_client.post(
        "/api/jobs",
        files={"images": ("t.png", image_bytes, "image/png")},
        data={"crop_meta": json.dumps({"1": meta})},
    )
    assert response.status_code == 200, response.text
    status = wait_until_job_completed(api_client, response.json()["job_id"])
    assert status["images"][0]["status"] == "completed", status["images"][0].get("error_msg")
    return status


def test_png_dimensions_header_read(test_settings):
    """PNG 头直读宽高（第四十次修订）：任意尺寸精确；非 PNG 文件回 (None, None)。"""
    from src.app.api import _png_dimensions

    for height, width in ((512, 512), (636, 636), (1234, 987), (61, 17)):
        canvas = np.zeros((height, width, 3), np.uint8)
        path = test_settings.resolve_data_dir() / f"dim_{width}x{height}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cv2.imencode(".png", canvas)[1].tobytes())
        assert _png_dimensions(path) == (width, height), f"{width}x{height} 头读失败"

    raw = test_settings.resolve_data_dir() / "not_a_png.bin"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\x00" * 64)
    assert _png_dimensions(raw) == (None, None), "非 PNG 应回 (None, None)"


def test_result_preview_and_status_dims(api_client: TestClient):
    """E2E：白底圆图（600 输入 + 描边外环 36 → 636² 成品）——状态接口带真实
    尺寸；?preview=1 输出 512px 保 alpha；全幅端点维持原幅。"""
    job_status = _submit_single(api_client, build_pattern_png_bytes(600, 600), {"shape": "circle"})
    image_status = job_status["images"][0]

    assert image_status["result_width"] == 636 and image_status["result_height"] == 636, (
        f"状态接口应报成品真实尺寸 636x636（含描边外环），实际 {image_status.get('result_width')}x{image_status.get('result_height')}"
    )

    full = api_client.get(image_status["result_url"])
    assert full.status_code == 200
    full_image = cv2.imdecode(np.frombuffer(full.content, np.uint8), cv2.IMREAD_UNCHANGED)
    assert full_image.shape[:2] == (636, 636), "全幅端点应维持原幅"

    first = api_client.get(image_status["result_url"] + "?preview=1")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    preview = cv2.imdecode(np.frombuffer(first.content, np.uint8), cv2.IMREAD_UNCHANGED)
    assert max(preview.shape[:2]) == 512, f"预览最长边应 512，实际 {preview.shape[:2]}"
    assert preview.shape[2] == 4, "预览必须保 alpha 通道（圆形塑形观感）"
    assert (preview[:, :, 3] > 200).mean() < 0.9, "预览应保留形状透明区"

    second = api_client.get(image_status["result_url"] + "?preview=1")
    assert second.content == first.content, "二次请求应命中缓存（同内容）"
