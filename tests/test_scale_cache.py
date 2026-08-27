"""超分结果缓存测试（第十二次修订）。

stub 策略与 test_watermark_shiliu 同构：monkeypatch PicwishScalePro.
upscale（不出网，第十八次修订佐糖语义——服务端定倍），断言：
①同图同档二次提交 → 第二次零外呼（缓存命中）；
②同图换档（width 不同）→ 新键重新外呼（不串档）；
③外呼失败 → 不写缓存，重提交再试（仍有外呼）；
④缓存值为 3 通道颜色图，命中路径 alpha 由调用侧贴回（输出 4 通道）。
"""

from __future__ import annotations

import cv2
import numpy as np

from src.core.config import PatternToolSettings
from src.steps.resize.resize import ResizeStep


class _StubScaler:
    """超分客户端替身：按预设返回（成功图/None），记录调用。"""

    def __init__(self, result: np.ndarray | None):
        self.calls = 0
        self._result = result

    def is_configured(self) -> bool:
        return True

    def upscale(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """佐糖语义：服务端定倍（stub 固定返回预设幅面，步内对齐目标）。"""
        self.calls += 1
        return self._result


def _scale_settings(tmp_path) -> PatternToolSettings:
    return PatternToolSettings(
        data_dir=str(tmp_path / "data"),
        scale_enabled=True,
        picwish_api_key="test-key",
        _env_file=None,
    )


def _small_bgra(size: int = 200) -> np.ndarray:
    """小图（放大触发形态：短边 200 < 任何打印档目标）。"""
    canvas = np.full((size, size, 4), (200, 80, 80, 255), np.uint8)
    cv2.circle(canvas, (size // 2, size // 2), size // 3, (60, 200, 60, 255), -1)
    return canvas


def _wire(step: ResizeStep, stub: _StubScaler) -> None:
    import src.steps.resize.picwish_scale as picwish_scale_module

    original_cls = picwish_scale_module.PicwishScalePro
    picwish_scale_module.PicwishScalePro = lambda settings: stub
    return original_cls  # 测试进程内 monkeypatch（同 test_fill_gen 口径）


def test_same_size_second_submit_hits_cache(tmp_path, monkeypatch):
    """同图同档二次提交：第二次零外呼（缓存命中，零积分）。"""
    import src.steps.resize.picwish_scale as m

    settings = _scale_settings(tmp_path)
    step = ResizeStep(settings)
    image = _small_bgra()
    big = cv2.resize(image[:, :, :3], (800, 800), interpolation=cv2.INTER_LANCZOS4)
    stub = _StubScaler(big)
    monkeypatch.setattr(m, "PicwishScalePro", lambda s: stub)

    first = step.run(image.copy(), size_cm=8.0)  # 8寸 → 目标 2244，放大路径
    assert first.stage_value == "done"
    assert stub.calls == 1

    second = step.run(image.copy(), size_cm=8.0)
    assert second.stage_value == "done"
    assert stub.calls == 1  # 缓存命中，零外呼


def test_different_size_new_cache_key(tmp_path, monkeypatch):
    """同图换档（8寸→12寸）：不同 width = 不同键，重新外呼各写各缓存。"""
    import src.steps.resize.picwish_scale as m

    settings = _scale_settings(tmp_path)
    step = ResizeStep(settings)
    image = _small_bgra()
    big = cv2.resize(image[:, :, :3], (3600, 3600), interpolation=cv2.INTER_LANCZOS4)
    stub = _StubScaler(big)
    monkeypatch.setattr(m, "PicwishScalePro", lambda s: stub)

    step.run(image.copy(), size_cm=8.0)
    step.run(image.copy(), size_cm=12.0)  # 不同目标宽 → 新键
    assert stub.calls == 2


def test_failure_not_cached_retry_allowed(tmp_path, monkeypatch):
    """外呼失败不写缓存：重提交仍有外呼（有机会重试真超分）。"""
    import src.steps.resize.picwish_scale as m

    settings = _scale_settings(tmp_path)
    step = ResizeStep(settings)
    image = _small_bgra()
    stub = _StubScaler(None)  # 模拟欠费/超时
    monkeypatch.setattr(m, "PicwishScalePro", lambda s: stub)

    first = step.run(image.copy(), size_cm=8.0)
    assert first.stage_value == "failed"  # 第十七次修订：失败记 failed 不插值

    second = step.run(image.copy(), size_cm=8.0)
    assert stub.calls == 2  # 失败未缓存：重提交再试


def test_cache_hit_reattaches_alpha(tmp_path, monkeypatch):
    """缓存值只存颜色（3 通道）：命中路径输出 4 通道（alpha 贴回）。"""
    import src.steps.resize.picwish_scale as m

    settings = _scale_settings(tmp_path)
    step = ResizeStep(settings)
    image = _small_bgra()
    big = cv2.resize(image[:, :, :3], (800, 800), interpolation=cv2.INTER_LANCZOS4)
    stub = _StubScaler(big)
    monkeypatch.setattr(m, "PicwishScalePro", lambda s: stub)

    step.run(image.copy(), size_cm=8.0)  # 写缓存
    result = step.run(image.copy(), size_cm=8.0)  # 命中路径
    assert result.image_bgr.shape[2] == 4
    assert np.array_equal(result.image_bgr[:, :, 3], np.full((result.image_bgr.shape[0], result.image_bgr.shape[1]), 255, np.uint8))
