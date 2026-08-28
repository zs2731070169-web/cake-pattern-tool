"""API 四接口线程域回归（2026-08-28 第二十三次修订）。

锁住的架构约束：四接口必须是**同步 def**——async def 形态下接口体的
SQLite/文件/图像编解码同步调用直接跑在 uvicorn 事件循环上，建批 9 张
3600×3600 图实测连续卡循环 1–4s、全站轮询停摆。同步 def 由 FastAPI
自动 run_in_threadpool 丢入 anyio 线程池。详见技术方案第二十三次修订。
"""

from __future__ import annotations

import asyncio
import inspect

from fastapi.routing import APIRoute

from src.app import api as api_module
from src.app.api import router
from tests.helpers import build_pattern_png_bytes


def test_all_api_endpoints_are_sync_defs():
    """四接口全部非协程——async 化回退会被本断言拦下（含 UploadFile 读取形态检查）。"""
    endpoints = {route.path: route for route in router.routes if isinstance(route, APIRoute)}
    expected = {"/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/images/{image_id}/result", "/api/meta"}
    assert set(endpoints) == expected
    for path, route in endpoints.items():
        assert not asyncio.iscoroutinefunction(route.endpoint), (
            f"{path} 是 async def——接口体将直接跑在 uvicorn 事件循环上，"
            "体内同步 SQLite/文件/编解码调用会卡住全站（第二十三次修订：四接口必须同步 def，"
            "UploadFile 读取用 .file.read()）"
        )


def test_create_job_source_has_no_await_reads():
    """同步接口体内不得残留 await upload 读取（await 在同步 def 内是 SyntaxError，
    裸 upload_file.read() 返回协程对象、len() 恒小 → 全部误判"图片过小"422）。"""
    source = inspect.getsource(api_module)
    assert "await upload_file" not in source
    assert "await original_file" not in source
    assert "upload_file.file.read()" in source


def test_create_job_accepts_rolled_upload(api_client):
    """>1MB 滚存上传（SpooledTemporaryFile 落盘态）走 .file.read() 全量读通——
    建批成功且响应字段齐（滚存分支是原 await read() 线程池路径的行为等价性验证）。"""
    large_png = build_pattern_png_bytes(width=1200, height=1200)
    # 叠加随机纹理把体积顶过 Starlette 1MB spool 上限（滚存到磁盘）
    import cv2
    import numpy as np

    rng = np.random.default_rng(7)
    noise_overlay = rng.integers(230, 256, (1200, 1200, 3), dtype=np.uint8)
    success, buffer = cv2.imencode(".png", noise_overlay)
    assert success
    rolled_bytes = buffer.tobytes()
    assert len(rolled_bytes) > 1024 * 1024, "测试前提：必须滚存"

    response = api_client.post(
        "/api/jobs",
        files=[
            ("images", ("big.png", large_png, "image/png")),
            ("images", ("rolled.png", rolled_bytes, "image/png")),
        ],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["image_ids"]) == 2
