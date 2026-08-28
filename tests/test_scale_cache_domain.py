"""缩放提前后的超分缓存域回归（2026-08-28 第二十七次修订）。

锁住的核心收益：送佐糖的图 = 去水印+填充后的**原始域**图（裁剪前）——
超分缓存键（图内容 SHA-256+目标宽）不随形状/裁剪框变化，同图不同形状
二次提交**命中缓存零外呼**（旧顺序裁剪先行，键随形状变，必 miss）。
"""

from __future__ import annotations

import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import build_pattern_png_bytes, wait_until_job_completed


def test_same_image_different_shapes_hits_scale_cache(api_client: TestClient, monkeypatch):
    """同图同尺寸档：circle 提交后再 heart 提交——第二次超分必须缓存命中
    （佐糖 upscale 外呼次数 = 1，不是 2）。"""
    from src.steps.resize.picwish_scale import PicwishScalePro

    call_lock = __import__("threading").Lock()
    upscale_calls = []

    def stub_upscale(self, image_bgr):
        with call_lock:
            upscale_calls.append(image_bgr.shape[:2])
        # 服务端定倍语义：小图 → 2048 宽
        return cv2.resize(image_bgr, (2048, 2048), interpolation=cv2.INTER_LANCZOS4)

    monkeypatch.setattr(PicwishScalePro, "upscale", stub_upscale)
    monkeypatch.setattr(PicwishScalePro, "is_configured", lambda self: True)

    image_bytes = build_pattern_png_bytes(width=600, height=600)

    # 第一次：circle 形状 + 9cm 档
    first = api_client.post(
        "/api/jobs",
        files=[("images", ("a.png", image_bytes, "image/png"))],
        data={"crop_meta": json.dumps({"1": {"shape": "circle", "size": {"cm": 9}}})},
    )
    assert first.status_code == 200
    first_status = wait_until_job_completed(api_client, first.json()["job_id"])
    assert first_status["images"][0]["stage_results"]["resize"] == "done"

    # 第二次：同一张图 heart 形状 + 同尺寸档——只有形状声明不同
    second = api_client.post(
        "/api/jobs",
        files=[("images", ("b.png", image_bytes, "image/png"))],
        data={"crop_meta": json.dumps({"1": {"shape": "heart", "size": {"cm": 9}}})},
    )
    assert second.status_code == 200
    second_status = wait_until_job_completed(api_client, second.json()["job_id"])
    assert second_status["images"][0]["stage_results"]["resize"] == "done"

    assert len(upscale_calls) == 1, (
        f"同图不同形状二次提交必须命中超分缓存（外呼 1 次），实际 {len(upscale_calls)} 次"
    )


def test_scale_runs_before_crop_in_pipeline(api_client: TestClient, monkeypatch):
    """顺序回归：带框裁剪 + 尺寸档的图，送超分的图必须是裁剪前的整图
    （放大基准图 = 原始域，目标短边按整图算）。"""
    from src.steps.resize.picwish_scale import PicwishScalePro

    submitted_shapes = []

    def stub_upscale(self, image_bgr):
        submitted_shapes.append(image_bgr.shape[:2])
        return cv2.resize(image_bgr, (2048, 2048), interpolation=cv2.INTER_LANCZOS4)

    monkeypatch.setattr(PicwishScalePro, "upscale", stub_upscale)
    monkeypatch.setattr(PicwishScalePro, "is_configured", lambda self: True)

    image_bytes = build_pattern_png_bytes(width=600, height=600)
    # 裁剪框只取整图左半（300×600）：若裁剪先跑，送超分的应是 300 宽；
    # 缩放先跑（第二十七次修订），送的必须是 600 宽整图
    response = api_client.post(
        "/api/jobs",
        files=[("images", ("a.png", image_bytes, "image/png"))],
        data={"crop_meta": json.dumps({
            "1": {
                "shape": "circle",
                "frame": {"width": 600, "height": 600},
                "data": {"x": 0, "y": 0, "width": 300, "height": 600},
                "size": {"cm": 9},
            }
        })},
    )
    assert response.status_code == 200
    job_status = wait_until_job_completed(api_client, response.json()["job_id"])

    assert len(submitted_shapes) == 1
    submitted_width, submitted_height = submitted_shapes[0]
    assert (submitted_width, submitted_height) == (600, 600), (
        f"送超分的必须是裁剪前的原始域整图 600x600，实际 {submitted_width}x{submitted_height}"
    )
    # 裁剪在高幅执行：成品宽 < 放大后整图宽（左半框裁掉了右半）
    image_status = job_status["images"][0]
    download = api_client.get(image_status["result_url"])
    result = cv2.imdecode(np.frombuffer(download.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert result.shape[1] < 2048, f"裁剪应在放大后执行（成品应为左半幅），实际宽 {result.shape[1]}"
