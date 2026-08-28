"""裁剪步配置开关测试（2026-08-28 第二十次修订：PT_CROP_ENABLED）。

语义：false → CropStep 不执行（不裁框不塑形），原图直通 skipped 记档；
true（默认）→ 声明式裁剪照常。第二十次修订删 PT_STEPS_ENABLED 后
五步各配独立开关，本文件锁 crop 档。
"""

from __future__ import annotations

import json

import numpy as np

from src.jobs.pipeline import RetouchPipeline
from src.jobs.store import JobStore
from src.core.config import PatternToolSettings


def _settings(tmp_path, crop_enabled=True) -> PatternToolSettings:
    return PatternToolSettings(
        data_dir=str(tmp_path),
        _env_file=None,
        crop_enabled=crop_enabled,
        wm_precheck_enabled=False,   # 检测未配 → watermark skipped 零外呼
        fill_gen_enabled=False,      # 生成式关 → fill skipped 零外呼
        scale_enabled=False,         # 超分关 → 不选尺寸时 resize skipped
    )


def _heart_png_bytes() -> bytes:
    import cv2

    canvas = np.full((200, 200, 3), 250, np.uint8)
    cv2.circle(canvas, (100, 100), 60, (60, 180, 90), -1)
    ok, buf = cv2.imencode(".png", canvas)
    return buf.tobytes()


def test_crop_disabled_passthrough(tmp_path):
    """PT_CROP_ENABLED=false → 不塑形原图直通，crop skipped。"""
    import cv2

    settings = _settings(tmp_path, crop_enabled=False)
    pipeline = RetouchPipeline(settings, JobStore(settings))
    meta = {"shape": "heart", "data": {"x": 0, "y": 0, "width": 200, "height": 200},
            "frame": {"width": 200, "height": 200}}
    result_bytes, stage_results, _ = pipeline._run_steps(
        _heart_png_bytes(), json.dumps(meta), None
    )
    assert stage_results.get("crop_applied") is None  # 未执行（skipped 无 applied 键）
    img = cv2.imdecode(np.frombuffer(result_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    # 未塑形：原画布四角（外环画布外扩 18px 后平移到 (18,18)，第三十二次
    # 修订）应不透明——若塑形了心形外 alpha=0
    assert img[18, 18, 3] == 255, "关闭裁剪后原画布角应保持不透明（原图直通）"


def test_crop_enabled_shapes_normally(tmp_path):
    """PT_CROP_ENABLED=true（默认）→ 声明形状照常塑形（回归锚点）。"""
    import cv2

    settings = _settings(tmp_path, crop_enabled=True)
    pipeline = RetouchPipeline(settings, JobStore(settings))
    meta = {"shape": "heart", "data": {"x": 0, "y": 0, "width": 200, "height": 200},
            "frame": {"width": 200, "height": 200}}
    result_bytes, stage_results, _ = pipeline._run_steps(
        _heart_png_bytes(), json.dumps(meta), None
    )
    assert stage_results.get("crop_applied") == "done"
    img = cv2.imdecode(np.frombuffer(result_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img[0, 0, 3] == 0, "心形塑形后四角应透明"
