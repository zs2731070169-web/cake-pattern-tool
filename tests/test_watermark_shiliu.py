"""去水印石榴唯一供应商测试（2026-08-27 第十次修订——佐糖完全下线）。

stub 策略与 test_fill_gen 同构：monkeypatch 石榴客户端的
remove_watermark（绕过 httpx，不出网），断言：
①预检判有 → 石榴外呼一次，成功记 done(api) + 写缓存；
②石榴失败 → 原图零误伤记 failed + heavy-watermark；
③未配 key → 检出也不外呼，failed 原样交付；
④缓存命中 → 零外呼（第二次提交不再调客户端）。
佐糖路由用例已随 provider 开关删除（watermark_picwish.py 已删，git 历史可溯）。
"""

from __future__ import annotations

import cv2
import numpy as np

from src.core.config import PatternToolSettings
from src.steps.watermark.watermark import WatermarkStep


def _wm_settings(tmp_path) -> PatternToolSettings:
    """去水印外呼全开的 Settings（预检配置由 stub 绕过）。"""
    return PatternToolSettings(
        data_dir=str(tmp_path / "data"),
        wm_api_enabled=True,
        shiliu_api_key="test-shiliu-key",
        _env_file=None,
    )


def _watermarked_bgra(size: int = 300) -> np.ndarray:
    """白底 + 顶部深色文字带的合成图（去水印目标形态）。"""
    canvas = np.full((size, size, 4), (255, 255, 255, 255), np.uint8)
    cv2.putText(
        canvas, "WATERMARK", (10, 40), cv2.FONT_HERSHEY_SIMPLEX,
        1.0, (30, 30, 30, 255), 2,
    )
    return canvas


class _StubPrecheck:
    """预检替身：恒判有水印（驱动修复链路）。"""

    def is_configured(self) -> bool:
        return True

    def has_watermark(self, image_bgr: np.ndarray) -> bool:
        return True


class _StubRemover:
    """石榴客户端替身：记录调用，按预设返回。"""

    def __init__(self, result: np.ndarray | None):
        self.calls = 0
        self._result = result

    def is_configured(self) -> bool:
        return True

    def remove_watermark(self, image_bgr: np.ndarray) -> np.ndarray | None:
        self.calls += 1
        return self._result


def _wire(step: WatermarkStep, shiliu: _StubRemover) -> None:
    step._precheck = _StubPrecheck()
    step._shiliu = shiliu


def test_shiliu_success_done_api(tmp_path):
    """石榴修复成功：外呼一次，记 done(api)。"""
    settings = _wm_settings(tmp_path)
    step = WatermarkStep(settings)
    image = _watermarked_bgra()
    shiliu = _StubRemover(image[:, :, :3].copy())
    _wire(step, shiliu)

    result = step.run(image.copy())
    assert shiliu.calls == 1
    assert result.stage_value == "done(api)"


def test_shiliu_failure_degrades_zero_harm(tmp_path):
    """石榴失败（欠费/超时 None）→ 原图零误伤 + failed + heavy-watermark。"""
    settings = _wm_settings(tmp_path)
    step = WatermarkStep(settings)
    image = _watermarked_bgra()
    _wire(step, _StubRemover(None))

    result = step.run(image.copy())
    assert result.stage_value == "failed"
    assert result.quality_hint == "heavy-watermark"
    assert np.array_equal(result.image_bgr, image)  # 原样交付


def test_shiliu_unconfigured_no_external_call(tmp_path):
    """石榴未配 key → 检出水印也不外呼，failed 原样交付（配置门在客户端侧）。"""
    settings = PatternToolSettings(
        data_dir=str(tmp_path / "data"),
        wm_api_enabled=False,
        shiliu_api_key="",
        _env_file=None,
    )
    # 真实客户端配置门口径：未配 key 时 is_configured()=False
    from src.steps.watermark.shiliu import ShiliuWatermarkRemover
    assert not ShiliuWatermarkRemover(settings).is_configured()

    step = WatermarkStep(settings)
    image = _watermarked_bgra()

    class _UnconfiguredRemover(_StubRemover):
        """跟随真实配置门（未配 → False）的替身：若被误调用会计数暴露。"""

        def is_configured(self) -> bool:
            return False

    shiliu = _UnconfiguredRemover(image[:, :, :3].copy())
    _wire(step, shiliu)

    result = step.run(image.copy())
    assert shiliu.calls == 0  # 配置门拦截，零外呼
    assert result.stage_value == "failed"
    assert np.array_equal(result.image_bgr, image)  # 原样交付


def test_cache_hit_skips_shiliu_call(tmp_path):
    """同图二次提交：缓存命中，石榴零外呼（缓存键与供应商无关）。"""
    settings = _wm_settings(tmp_path)
    step = WatermarkStep(settings)
    image = _watermarked_bgra()
    shiliu = _StubRemover(image[:, :, :3].copy())
    _wire(step, shiliu)

    first = step.run(image.copy())
    assert first.stage_value == "done(api)"
    assert shiliu.calls == 1

    second = step.run(image.copy())
    assert second.stage_value == "done(api)"
    assert shiliu.calls == 1  # 缓存命中不再外呼
