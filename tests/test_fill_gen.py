"""填充步 Path C 生成式换白底测试（v16，7.3 验收新增项）。

stub 策略：monkeypatch QwenBackgroundReplacer.replace_background（绕过
httpx，测判定门/降级链/验证门/缓存语义）；协议与尺寸适配直调客户端
内部方法断言，不出网。
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.core.config import PatternToolSettings
from src.steps.fill.qwenbg import QwenBackgroundReplacer
from src.steps.fill.filling import FillStep
from tests.helpers import build_pattern_png_bytes, submit_single_image, wait_until_job_completed


class _StubReplacer:
    """替身客户端：记录调用次数，按预设返回（图/None/异常）。"""

    def __init__(self, result: np.ndarray | None = None, error: Exception | None = None):
        self.calls = 0
        self._result = result
        self._error = error

    def is_configured(self) -> bool:
        return True

    def replace_background(self, image_bgr: np.ndarray) -> np.ndarray | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def _gen_settings(tmp_path, **overrides) -> PatternToolSettings:
    """fill_gen 全开的 Settings（临时 data 目录；其余默认）。"""
    values = {
        "data_dir": str(tmp_path / "data"),
        "fill_gen_enabled": True,
        "fill_gen_key": "test-key",
    }
    values.update(overrides)
    return PatternToolSettings(_env_file=None, **values)


def _dark_bg_canvas(size: int = 400) -> np.ndarray:
    """深色背景 + 居中彩色主体（假 alpha：背景 baked 进 RGB）的合成图。

    主体占内部区 ≥15%（主体门 GEN_SUBJECT_MAX_RATIO=0.85 的镜像：内部
    非宽白占比 ≤0.85 才外呼）——真实场景比例（图案居中、四周留背景）。
    """
    canvas = np.full((size, size, 3), (30, 30, 120), np.uint8)  # 深蓝底（非宽白系）
    subject_radius = int(size * 0.42)  # 主体 ~0.42²≈0.176 内部区占比的圆盘
    cv2.circle(canvas, (size // 2, size // 2), subject_radius, (60, 200, 60), -1)
    return canvas


def _white_bg_version(canvas: np.ndarray) -> np.ndarray:
    """同构图的换白底版（主体逐像素同、背景纯白）——门 A/B 全过的 stub 返回值。"""
    size = canvas.shape[0]
    mask = np.zeros((size, size), np.uint8)
    cv2.circle(mask, (size // 2, size // 2), int(size * 0.42), 255, -1)
    white = canvas.copy()
    white[:, :, :3] = np.where(mask[..., None] > 0, canvas, 255)
    return white


def _white_bg_version_of(canvas: np.ndarray, subject_color: tuple) -> np.ndarray:
    """任意底色 canvas 的换白底版：非主体色像素全部置白（主体按颜色识别）。"""
    target = np.array(subject_color, np.uint8)
    white = canvas.copy()
    is_subject = np.all(canvas == target, axis=2)
    white[:, :, :3] = np.where(is_subject[..., None], canvas, 255)
    return white


def _dirty_white_canvas(size: int = 400) -> np.ndarray:
    """米白脏底假 alpha 图（22:12 翻车案例同构）：alpha 全 255、
    背景 244-253 欠纯白暖白、主体居中彩色圆盘。"""
    canvas = np.full((size, size, 3), 244, np.uint8)  # 欠纯白暖白底
    cv2.circle(canvas, (size // 2, size // 2), int(size * 0.42), (60, 200, 60), -1)
    return canvas





def _install_checkerboard_stub(monkeypatch: pytest.MonkeyPatch, verdict: bool) -> None:
    """注入 VL 棋盘格判定替身（绕过网络；v18.5 判定门）。"""
    from src.steps.fill.gate_vl import CheckerboardGate

    monkeypatch.setattr(
        CheckerboardGate, "has_checkerboard_background",
        lambda self, image_bgr: verdict,
    )


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: _StubReplacer) -> None:
    """FillStep.__init__ 后注入 stub 实例属性（类属性会被实例赋值遮蔽）。"""

    original_init = FillStep.__init__

    def _patched_init(self, settings):
        original_init(self, settings)
        self._qwen_bg = stub

    monkeypatch.setattr(FillStep, "__init__", _patched_init)


# ---- 触发门 ----


def _checkerboard_canvas(size: int = 400, cell: int = 16) -> np.ndarray:
    """棋盘格底假 alpha 图（素材站导出形态：透明区被拍平成查看器指示格）。"""
    canvas = np.zeros((size, size, 3), np.uint8)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            shade = 255 if ((x // cell + y // cell) % 2 == 0) else 204
            canvas[y:y + cell, x:x + cell] = shade
    cv2.circle(canvas, (size // 2, size // 2), int(size * 0.42), (60, 200, 60), -1)
    return canvas


def test_gen_trigger_checkerboard(tmp_path, monkeypatch):
    """v18.5：棋盘格背景（VL 判 true）→ 触发生成式外呼（stub 被调 1 次，
    验证门过采纳）。"""
    stub = _StubReplacer(result=_white_bg_version(_checkerboard_canvas()))
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, True)
    result = FillStep(_gen_settings(tmp_path)).run(_checkerboard_canvas())
    assert stub.calls == 1
    assert result.stage_value == "白色背景"


def test_gen_not_triggered_white_bg(tmp_path, monkeypatch):
    """v18.5：白底图（VL 判 false）不外呼——本地像素白度判据已退役。"""
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, False)
    canvas = build_pattern_png_bytes()  # 白底图案图
    import io

    from PIL import Image

    image = np.asarray(Image.open(io.BytesIO(canvas)).convert("RGB"))[:, :, ::-1].copy()
    result = FillStep(_gen_settings(tmp_path)).run(image)
    assert stub.calls == 0
    assert result.stage_value == "skipped"


def test_gen_beige_solid_skipped(tmp_path, monkeypatch):
    """v18.5：米色纯底（VL 判 false——非棋盘格）→ 跳过填充不外呼
    （像素判据退役：米白/纯色底不再是填白对象，只有棋盘格才是）。"""
    canvas = np.full((400, 400, 3), (170, 180, 200), np.uint8)
    cv2.circle(canvas, (200, 220), 90, (60, 140, 60), -1)
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, False)
    result = FillStep(_gen_settings(tmp_path)).run(canvas)
    assert stub.calls == 0
    assert result.stage_value == "skipped"


def test_gen_dirty_white_solid_skipped(tmp_path, monkeypatch):
    """v18.5：米白脏底纯色（VL 判 false）→ 跳过（像素欠白判据退役）。"""
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, False)
    result = FillStep(_gen_settings(tmp_path)).run(_dirty_white_canvas())
    assert stub.calls == 0
    assert result.stage_value == "skipped"


def test_gen_photo_skipped(tmp_path, monkeypatch):
    """v18.5：照片（VL 判 false）→ 跳过零外呼（2026-08-26 车照片误外呼
    教训：像素判据全线退役，改 VL 判定）。"""
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, False)
    photo = np.zeros((400, 600, 3), np.uint8)
    for row in range(400):
        photo[row, :, :] = (int(120 + 80 * row / 400), int(140 + 60 * row / 400), 200)
    cv2.rectangle(photo, (100, 250), (400, 390), (40, 60, 50), -1)
    cv2.circle(photo, (300, 160), 70, (30, 30, 30), -1)
    result = FillStep(_gen_settings(tmp_path)).run(photo)
    assert stub.calls == 0
    assert result.stage_value == "skipped"


def test_gen_true_alpha_transparent_skipped(tmp_path, monkeypatch):
    """v18.5：真 alpha 透明底（后端收到透明通道非棋盘格，VL 判 false）→
    跳过（CPU 合成已移除；带棋盘格的是预览拍平截图形态，另一用例覆盖）。"""
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, False)
    rgba = np.zeros((400, 400, 4), np.uint8)
    rgba[:, :, :3] = 255
    rgba[:, :, 3] = 0
    yy, xx = np.mgrid[:400, :400]
    rgba[((yy - 200) ** 2 + (xx - 200) ** 2 <= 180 ** 2), 3] = 255
    bgr = rgba[:, :, :3].copy()
    cv2.circle(bgr, (200, 200), 130, (60, 200, 60), -1)
    rgba[:, :, :3] = bgr
    result = FillStep(_gen_settings(tmp_path)).run(rgba)
    assert stub.calls == 0
    assert result.stage_value == "skipped"


def test_gen_full_subject_single_color_outcalls_once(tmp_path, monkeypatch):
    """v18.5 已知边界：主体铺满且纯色（非白带=单一主体色簇 1.0）会通过主体门
    外呼一次——模型返回近似原图，验证门/缓存兜底无伤害。像素"贴满"判据对
    纯色主体天然失效（content_mask 全图污染的老问题），接受此边界。"""
    stub = _StubReplacer(result=None)
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, True)
    canvas = _checkerboard_canvas()
    canvas[:, :, :] = (60, 200, 60)  # 主体铺满全图（纯色）
    result = FillStep(_gen_settings(tmp_path)).run(canvas)
    assert result.stage_value == "failed"  # stub 返回 None → 降级（2026-08-27 记档口径：触发但失败=failed，前端红显）


# ---- 降级链 ----


def test_gen_api_error_degrades(tmp_path, monkeypatch):
    """API 异常 → None → 原图降级（done(degraded)/skipped），不抛异常。"""
    stub = _StubReplacer(error=ValueError("api down"))
    _install_stub(monkeypatch, stub)
    _install_checkerboard_stub(monkeypatch, True)
    result = FillStep(_gen_settings(tmp_path)).run(_checkerboard_canvas())
    assert stub.calls == 1
    assert result.stage_value == "failed"  # 2026-08-27 记档口径：触发但失败=failed


def test_gen_paper_white_delivered_verbatim(tmp_path, monkeypatch):
    """v18.6.1 已撤（2026-08-26 15:20 用户定案——色阶归一在主体交界产生
    颜色断层）：模型返回什么交付什么，纸白问题靠提示词约束。"""
    _install_checkerboard_stub(monkeypatch, True)
    canvas = _checkerboard_canvas()
    paper_white = canvas.copy()
    paper_white[:, :, :3] = np.where(
        (paper_white[:, :, :3].mean(axis=2) < 244)[..., None], paper_white[:, :, :3], 249
    )
    stub = _StubReplacer(result=paper_white)
    _install_stub(monkeypatch, stub)
    result = FillStep(_gen_settings(tmp_path)).run(canvas)
    assert result.stage_value == "白色背景"
    # 模型输出原样交付（零像素级后处理）
    assert tuple(int(v) for v in result.image_bgr[5, 5][:3]) == (249, 249, 249)
    assert tuple(int(v) for v in result.image_bgr[200, 200][:3]) == (60, 200, 60)


# ---- 采纳路径 ----


def test_gen_adopted_compose(tmp_path, monkeypatch):
    """棋盘格图 + stub 换白结果 → 白色背景，整幅交付（形状无关）。"""
    _install_checkerboard_stub(monkeypatch, True)
    canvas = _checkerboard_canvas()
    stub = _StubReplacer(result=_white_bg_version(canvas))
    _install_stub(monkeypatch, stub)
    from src.steps.imaging import ensure_bgra

    result = FillStep(_gen_settings(tmp_path)).run(ensure_bgra(canvas))
    out = result.image_bgr
    assert result.stage_value == "白色背景"
    # 主体像素 ≈ 原图
    assert tuple(int(v) for v in out[200, 200][:3]) == (60, 200, 60)
    # 背景已白（整图域，画布角也是白——形状裁剪由管线描边前环节负责）
    assert tuple(int(v) for v in out[30, 200][:3]) == (255, 255, 255)
    assert tuple(int(v) for v in out[5, 5][:3]) == (255, 255, 255)
    assert int(out[5, 5][3]) == 255  # 无形状信息，整幅不透明


def test_gen_cache_hit_no_second_call(tmp_path, monkeypatch):
    """同图两次 run 只外呼一次（缓存命中第二次零外呼）。"""
    _install_checkerboard_stub(monkeypatch, True)
    canvas = _checkerboard_canvas()
    stub = _StubReplacer(result=_white_bg_version(canvas))
    _install_stub(monkeypatch, stub)
    step = FillStep(_gen_settings(tmp_path))
    first = step.run(canvas)
    second = step.run(canvas)  # 第二次 stub 仍在但应被缓存短路
    assert stub.calls == 1
    assert first.stage_value == "白色背景"
    assert second.stage_value == "白色背景"


# ---- 协议与尺寸适配（直调客户端内部方法，不出网）----


def test_gen_request_size_aligned():
    """size 参数：跟随宽高比（≤2048 不放大）、16 对齐、clamp [512, 2048]（API 硬下限）。"""
    assert QwenBackgroundReplacer._request_size(400, 400) == "512*512"
    assert QwenBackgroundReplacer._request_size(2000, 1000) == "2000*992"  # 16 对齐不放大
    assert QwenBackgroundReplacer._request_size(4000, 2000) == "2048*1024"  # 超限缩到 2048
    out_w, out_h = QwenBackgroundReplacer._request_size(3600, 2400).split("*")
    assert int(out_w) % 16 == 0 and int(out_h) % 16 == 0
    assert 512 <= int(out_w) <= 2048 and 512 <= int(out_h) <= 2048


def test_gen_encode_upload_downscale(tmp_path):
    """3600 输入降采样 ≤2048；data URI 前缀合法。"""
    settings = _gen_settings(tmp_path)
    client = QwenBackgroundReplacer(settings)
    big = np.full((3600, 3600, 3), (60, 200, 60), np.uint8)
    data_uri, width, height = client._encode_upload(big)
    assert max(width, height) <= 2048
    assert data_uri.startswith("data:image/")
    assert len(data_uri) <= 8 * 1024 * 1024


def test_gen_request_payload_shape(tmp_path):
    """payload 骨架：model/prompt_extend=False/watermark=False/negative_prompt 非空。"""
    settings = _gen_settings(tmp_path)
    client = QwenBackgroundReplacer(settings)

    captured = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": {
                    "choices": [
                        {"message": {"content": [{"image": "https://example.com/r.png"}]}}
                    ]
                },
                "usage": {"image_count": 1},
            }

    def _fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse()

    # core 重构后（2026-08-26）：_generate 走 http_sync(self._http.post(...))——
    # 替换 _http 为伪对象 + http_sync 为恒等（协程直接当响应用，不跑事件循环）
    import src.steps.fill.qwenbg as qwenbg_module

    original_sync = qwenbg_module.http_sync
    qwenbg_module.http_sync = lambda coro: coro
    client._http = type("FakeHttp", (), {"post": staticmethod(_fake_post)})()
    try:
        import time

        url = client._generate("data:image/png;base64,xxx", "1024*1024", time.monotonic() + 10)
        assert url == "https://example.com/r.png"
    finally:
        qwenbg_module.http_sync = original_sync
    payload = captured["json"]
    assert payload["model"] == settings.fill_gen_model
    assert payload["parameters"]["prompt_extend"] is False
    assert payload["parameters"]["watermark"] is False
    assert payload["parameters"]["negative_prompt"]
    assert payload["parameters"]["size"] == "1024*1024"


# ---- 端到端（未配 key 零外呼零报错）----


def test_fill_gen_unconfigured_end_to_end(api_client: TestClient):
    """未配 key（conftest 默认）→ 深底图 completed、fill skipped、零网络。"""
    canvas = np.full((400, 400, 3), (30, 30, 120), np.uint8)
    cv2.circle(canvas, (200, 200), 100, (60, 200, 60), -1)
    success, buffer = cv2.imencode(".png", canvas)
    assert success
    job_id = submit_single_image(api_client, buffer.tobytes())
    job_status = wait_until_job_completed(api_client, job_id)
    image_status = job_status["images"][0]
    assert image_status["status"] == "completed"
    assert image_status["stage_results"]["fill"] == "skipped"


# ---- 尺寸缩放步（ResizeStep，2026-08-26）----


def test_resize_skipped_no_size(tmp_path):
    """未选尺寸 → skipped 原幅（默认零回归）。"""
    from src.steps.resize import ResizeStep

    canvas = np.full((2000, 2000, 3), 250, np.uint8)
    result = ResizeStep(_gen_settings(tmp_path)).run(canvas, None)
    assert result.stage_value == "skipped"
    assert result.image_bgr.shape[:2] == (2000, 2000)


def test_resize_downscale_local(tmp_path):
    """短边 > 目标 → 本地 INTER_AREA 等比缩小（2000px 图选 4寸 1063）。"""
    from src.steps.resize import ResizeStep

    canvas = np.full((2000, 2000, 3), 250, np.uint8)
    cv2.circle(canvas, (1000, 1000), 500, (60, 180, 90), -1)
    result = ResizeStep(_gen_settings(tmp_path)).run(canvas, 9.0)
    assert result.stage_value == "done"
    assert min(result.image_bgr.shape[:2]) == 1063


def test_resize_invalid_size_skipped(tmp_path):
    """非法尺寸（超 5-33 区间，上限=iX6880 A3+ 定标）→ skipped 原幅。"""
    from src.steps.resize import ResizeStep

    canvas = np.full((500, 500, 3), 250, np.uint8)
    for bad in (0.5, 34.0, 300.0):
        result = ResizeStep(_gen_settings(tmp_path)).run(canvas, bad)
        assert result.stage_value == "skipped"


# ---- 放大路径（2026-08-27 第十一次修订：石榴大图变高清 width 直出）----


def test_resize_upscale_prefers_super_resolution(tmp_path, monkeypatch):
    """放大单外呼石榴大图变高清：mock width 直出 → 目标尺寸直达记 done。"""
    from src.steps.resize import ResizeStep, picwish_scale

    settings = _gen_settings(tmp_path, scale_enabled=True, picwish_api_key="test-key")

    def _fake_upscale(self, image_bgr):
        # 服务端定倍语义：出 4 倍幅（500 方图 → 2000），出幅≥目标的档由步内缩回
        return cv2.resize(image_bgr, (image_bgr.shape[1] * 4, image_bgr.shape[0] * 4),
                          interpolation=cv2.INTER_CUBIC)

    monkeypatch.setattr(picwish_scale.PicwishScalePro, "upscale", _fake_upscale)
    monkeypatch.setattr(
        picwish_scale.PicwishScalePro, "is_configured", lambda self: True
    )

    canvas = np.full((500, 500, 3), 250, np.uint8)
    cv2.circle(canvas, (250, 250), 150, (60, 180, 90), -1)
    # 12寸=29cm → 目标短边 3425：×4 stub 出 2000 → 尾程 1.7x 触发换大图建议
    result = ResizeStep(settings).run(canvas, 29.0)
    assert result.stage_value == "done"
    assert min(result.image_bgr.shape[:2]) == 3425
    assert result.quality_hint == "suggest-larger-source"  # 尾程 1.7x>1.15 提示（2026-08-28）


def test_resize_upscale_failure_marks_failed(tmp_path, monkeypatch):
    """石榴超分失败 → 记 failed 不交付插值废图（2026-08-28 第十七次修订，
    用户定案"失败了就失败了"：9.5 倍拉伸锐度 ~3 是废图，交付废图不是保交付）。"""
    from src.steps.resize import ResizeStep, picwish_scale

    settings = _gen_settings(tmp_path, scale_enabled=True, picwish_api_key="test-key")
    monkeypatch.setattr(
        picwish_scale.PicwishScalePro, "is_configured", lambda self: True
    )
    monkeypatch.setattr(
        picwish_scale.PicwishScalePro, "upscale",
        lambda self, image_bgr: None,
    )

    canvas = np.full((500, 500, 3), 250, np.uint8)
    result = ResizeStep(settings).run(canvas, 29.0)
    assert result.stage_value == "failed"
    assert result.image_bgr.shape[:2] == canvas.shape[:2]  # 原幅回传（不插值放大）


def test_resize_upscale_not_configured_marks_failed(tmp_path):
    """石榴超分未配置（scale_enabled 关）→ 记 failed（同失败口径——
    超分没跑就是没跑，第十七次修订起不再插值交付）。"""
    from src.steps.resize import ResizeStep

    settings = _gen_settings(tmp_path)
    assert not settings.scale_enabled  # 前置：该夹具默认未启用超分
    canvas = np.full((500, 500, 3), 250, np.uint8)
    result = ResizeStep(settings).run(canvas, 29.0)
    assert result.stage_value == "failed"
    assert result.image_bgr.shape[:2] == canvas.shape[:2]  # 原幅回传


def test_resize_tail_stretch_sets_suggest_hint(tmp_path, monkeypatch):
    """尾程提示（2026-08-28）：佐糖出幅 < 目标（尾程放大 >1.15x）→ 
    quality_hint=suggest-larger-source（前端黄标建议换大图）。"""
    from src.steps.resize import ResizeStep, picwish_scale

    settings = _gen_settings(tmp_path, scale_enabled=True, picwish_api_key="test-key")

    def _small_out(self, image_bgr):
        # 服务端定倍 stub：只出 1.5 倍（模拟小出幅 → 12寸档 3425 需大尾程）
        return cv2.resize(image_bgr, (image_bgr.shape[1]*3//2, image_bgr.shape[0]*3//2),
                          interpolation=cv2.INTER_CUBIC)

    monkeypatch.setattr(picwish_scale.PicwishScalePro, "upscale", _small_out)
    monkeypatch.setattr(picwish_scale.PicwishScalePro, "is_configured", lambda self: True)

    canvas = np.full((500, 500, 3), 250, np.uint8)
    result = ResizeStep(settings).run(canvas, 29.0)  # 12寸 3425：出幅 750 → 尾程 4.6x
    assert result.stage_value == "done"
    assert result.quality_hint == "suggest-larger-source"


def test_resize_no_tail_no_hint(tmp_path, monkeypatch):
    """出幅 ≥ 目标（无尾程）→ 不提示（quality_hint=none）。"""
    from src.steps.resize import ResizeStep, picwish_scale

    settings = _gen_settings(tmp_path, scale_enabled=True, picwish_api_key="test-key")

    def _big_out(self, image_bgr):
        # 出 8 倍幅 ≥ 3425 → INTER_AREA 缩回，零尾程
        return cv2.resize(image_bgr, (image_bgr.shape[1]*8, image_bgr.shape[0]*8),
                          interpolation=cv2.INTER_CUBIC)

    monkeypatch.setattr(picwish_scale.PicwishScalePro, "upscale", _big_out)
    monkeypatch.setattr(picwish_scale.PicwishScalePro, "is_configured", lambda self: True)

    canvas = np.full((500, 500, 3), 250, np.uint8)
    result = ResizeStep(settings).run(canvas, 29.0)
    assert result.stage_value == "done"
    assert result.quality_hint == "none"
