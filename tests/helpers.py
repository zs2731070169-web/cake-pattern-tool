"""测试造样图工具：白底图案 / 带水印 / 非白底等合成图（7.3 验收断言用）。"""

from __future__ import annotations

import io

import cv2
import numpy as np
from fastapi.testclient import TestClient


def build_pattern_png_bytes(
    width: int = 600,
    height: int = 600,
    background_bgr: tuple[int, int, int] = (255, 255, 255),
    with_watermark_text: bool = False,
    heavy_watermark: bool = False,
) -> bytes:
    """合成一张图案图：中央彩色圆 + 可选文字水印。

    白底默认（描边触发）；with_watermark_text 加单条角标水印；
    heavy_watermark 加多条铺开文字（复杂档）。
    """
    canvas = np.full((height, width, 3), background_bgr, dtype=np.uint8)
    cv2.circle(canvas, (width // 2, height // 2), min(width, height) // 4, (0, 200, 200), -1)
    if with_watermark_text:
        cv2.putText(
            canvas, "SHOP WM 2026", (width - 260, height - 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2,
        )
    if heavy_watermark:
        for index in range(6):
            cv2.putText(
                canvas, f"WATERMARK{index}", (20, 60 + index * 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (128, 128, 128), 2,
            )
    encode_success, png_buffer = cv2.imencode(".png", canvas)
    assert encode_success
    return png_buffer.tobytes()


def submit_single_image(
    client: TestClient, image_bytes: bytes, filename: str = "sample.png"
) -> str:
    """提交单图批次，返回 job_id。"""
    create_response = client.post(
        "/api/jobs", files={"images": (filename, image_bytes, "image/png")}
    )
    assert create_response.status_code == 200, create_response.text
    return create_response.json()["job_id"]


def wait_until_job_completed(client: TestClient, job_id: str, timeout_seconds: float = 120) -> dict:
    """轮询到批次 completed（失败断言），返回最终 JobStatusResponse。"""
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status_response = client.get(f"/api/jobs/{job_id}")
        assert status_response.status_code == 200
        job_status = status_response.json()
        if job_status["status"] == "completed":
            return job_status
        time.sleep(0.5)
    raise AssertionError("job did not complete in time: " + job_id)


def build_noisy_png_bytes(width: int, height: int) -> bytes:
    """高熵小图 PNG（>1KB 但尺寸任意）——绕开字节下限单测像素下限用。"""
    import cv2
    import numpy as np

    rng = np.random.default_rng(42)
    noise = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    success, buffer = cv2.imencode(".png", noise)
    assert success
    return buffer.tobytes()
