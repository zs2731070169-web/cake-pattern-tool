"""第三方 API 并发闸回归（第三十八次修订）。

背景：2026-08-29 22:55 九图批（24cm 档全走佐糖超分）批内并行下 9 路提交
瞬间并发，前 3 成功后连续 4 个 HTTP 429——provider_slot 按供应商限在途
任务数，超额排队。锁三个性质：并发上限、0=直通、客户端真实路径生效。
"""

from __future__ import annotations

import threading
import time

import numpy as np

from src.core.api_throttle import provider_slot
from src.core.config import PatternToolSettings


def _run_threads(worker, count: int) -> None:
    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_provider_slot_caps_concurrency(test_settings: PatternToolSettings):
    """闸内并发恒 ≤ limit：8 线程抢 limit=3 的闸，峰值恰为 3、全部完成。"""
    lock = threading.Lock()
    state = {"current": 0, "peak": 0, "done": 0}

    def worker() -> None:
        with provider_slot("unit-test-cap", 3):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.05)  # 持闸窗口——保证并发可观测
            with lock:
                state["current"] -= 1
                state["done"] += 1

    _run_threads(worker, 8)
    assert state["done"] == 8, "全部任务应完成（无死锁/漏释放）"
    assert state["peak"] == 3, f"并发峰值应=limit 3，实际 {state['peak']}"
    # 闸完全释放（后续可再进）
    with provider_slot("unit-test-cap", 3):
        pass


def test_provider_slot_zero_is_passthrough(test_settings: PatternToolSettings):
    """limit=0 不限流：6 线程同持闸（无 provider 前缀隔离干扰）。"""
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def worker() -> None:
        with provider_slot("unit-test-zero", 0):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.03)
            with lock:
                state["current"] -= 1

    _run_threads(worker, 6)
    assert state["peak"] > 3, f"limit=0 应直通（并发 >3），实际峰值 {state['peak']}"


def test_picwish_upscale_throttled(test_settings: PatternToolSettings):
    """佐糖真实路径：upscale 周期受闸——6 线程并发 upscale 峰值 ≤ 2。"""
    from src.steps.resize.picwish_scale import PicwishScalePro

    test_settings.picwish_max_concurrent = 2
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    release_event = threading.Event()

    client = PicwishScalePro(test_settings)

    def fake_submit(image_bgr):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        return "task-throttle-test"

    def fake_poll(task_id):
        # 持闸直到主线程观测到峰值，避免任务过早完成导致观测窗口塌缩
        release_event.wait(timeout=5)
        with lock:
            state["current"] -= 1
        return {"state": 1, "image": None}  # image=None → upscale 返 None（观测不依赖结果）

    client._submit = fake_submit  # type: ignore[assignment]
    client._poll = fake_poll  # type: ignore[assignment]
    client._download = lambda url: np.zeros((4, 4, 3), np.uint8)  # type: ignore[assignment]

    # poll 先 sleep(_POLL_INTERVAL_SECONDS=2) 再进 fake_poll——峰值观测窗口足够
    def worker() -> None:
        client.upscale(np.zeros((4, 4, 3), np.uint8))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(4)  # 6 线程均已过 submit（各自第一轮 poll 前）
    release_event.set()
    for t in threads:
        t.join()
    assert state["peak"] <= 2, f"佐糖并发峰值应 ≤2（闸生效），实际 {state['peak']}"


def test_throttle_settings_defaults(test_settings: PatternToolSettings):
    """三供应商默认上限（3/4/2）——README/.env.example 文档口径锁。"""
    assert test_settings.picwish_max_concurrent == 3
    assert test_settings.dashscope_max_concurrent == 4
    assert test_settings.shiliu_max_concurrent == 2
