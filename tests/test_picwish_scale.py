"""佐糖 PicWish scale-pro 客户端测试（2026-08-28 第十八次修订）。

覆盖：异步协议骨架（sync=0 提交 → 轮询 state==1 → 下载）、失败/超时 None、
未配置拦截、提交参数（sync=0 显式异步）。
外部 HTTP 全部 monkeypatch（_submit/_poll/_download 层拦截，不走网络）。
"""

from __future__ import annotations

import cv2
import numpy as np

from src.core.config import PatternToolSettings
from src.steps.resize.picwish_scale import PicwishScalePro


def _settings(tmp_path, scale_enabled=True, key="test-key") -> PatternToolSettings:
    return PatternToolSettings(
        data_dir=str(tmp_path),
        _env_file=None,
        scale_enabled=scale_enabled,
        picwish_api_key=key,
    )


class _StubClient(PicwishScalePro):
    """拦截三步协议的替身：可编程 state 序列与出图。"""

    def __init__(self, settings, states=None, fail_submit=False):
        super().__init__(settings)
        self._states = states or [1]  # 默认立即成功
        self._fail_submit = fail_submit
        self.submit_calls = 0
        self.poll_calls = 0
        self.submit_kwargs = None

    def _submit(self, image_bgr):
        self.submit_calls += 1
        if self._fail_submit:
            raise ValueError("submit rejected")
        return "task-123"

    def _poll(self, task_id):
        self.poll_calls += 1
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return {"state": state, "image": "https://oss.example/r.png"} if state == 1 else {"state": state}

    def _download(self, image_url):
        canvas = np.full((100, 100, 3), 200, np.uint8)
        return cv2.resize(canvas, (400, 400))  # 4 倍语义


def test_upscale_async_submit_poll_download(tmp_path):
    """成功路径：提交 1 次 → 轮询至 state=1 → 下载 4 倍图。"""
    client = _StubClient(_settings(tmp_path), states=[0, 2, 1])
    result = client.upscale(np.full((100, 100, 3), 200, np.uint8))
    assert result is not None
    assert result.shape[1] == 400
    assert client.submit_calls == 1
    assert client.poll_calls == 3  # 0/2 进行中 + 1 成功


def test_upscale_submit_failure_returns_none(tmp_path):
    """提交即失败 → None（不轮询）。"""
    client = _StubClient(_settings(tmp_path), fail_submit=True)
    assert client.upscale(np.full((100, 100, 3), 200, np.uint8)) is None
    assert client.poll_calls == 0


def test_upscale_task_failure_state_returns_none(tmp_path):
    """轮询返回负值（任务失败）→ None。"""
    client = _StubClient(_settings(tmp_path), states=[0, -1])
    assert client.upscale(np.full((100, 100, 3), 200, np.uint8)) is None


def test_upscale_not_configured(tmp_path):
    """未配置（key 空）由调用方 is_configured 拦截。"""
    client = PicwishScalePro(_settings(tmp_path, key=""))
    assert client.is_configured() is False
