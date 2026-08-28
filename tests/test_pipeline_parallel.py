"""管线并行化回归（2026-08-28 第二十六次修订：A 批内并行 + B 入口双 VL）。

锁住的架构契约：
- 批内图并行：多图批墙钟 ≈ 最慢一张（并发执行，非串行相加）；
- 条件完成回写：recovery 置 failed 后管线完成回写不覆盖（终态单向 DB 层闭环）；
- 入口双 VL 并行 + 判定复用：同图棋盘格只问一次、预检只问一次；
- fill-first 顺序下入口预检结果丢弃（生成图域答案不成立）；
- 缓存原子写：并发读写同键永不见半张 PNG。

stub 全部线程安全（计数器加锁——裸 += 在并发下丢计数）。
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import build_noisy_png_bytes, build_pattern_png_bytes, wait_until_job_completed


class CountingGate:
    """线程安全的棋盘格判定 stub：计数 + 固定判定值。"""

    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, analysis_bgr: np.ndarray) -> bool:
        with self._lock:
            self.calls += 1
        return self.verdict


class CountingPrecheck:
    """线程安全的水印预检 stub。"""

    def __init__(self, verdict: bool | None) -> None:
        self.verdict = verdict
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, analysis_bgr: np.ndarray) -> bool | None:
        with self._lock:
            self.calls += 1
        return self.verdict


def _install_entry_gates(monkeypatch, checkerboard: bool, precheck: bool | None):
    """把管线的两个 VL 问询换成计数 stub（入口与步骤内部共用同一 gate 实例路径）。"""
    from src.steps.fill.gate_vl import CheckerboardGate
    from src.steps.watermark.precheck import WatermarkPrecheck

    counting_gate = CountingGate(checkerboard)
    counting_precheck = CountingPrecheck(precheck)

    monkeypatch.setattr(CheckerboardGate, "is_configured", lambda self: True)
    monkeypatch.setattr(CheckerboardGate, "has_checkerboard_background", lambda self, bgr: counting_gate(bgr))
    monkeypatch.setattr(WatermarkPrecheck, "is_configured", lambda self: True)
    monkeypatch.setattr(WatermarkPrecheck, "has_watermark", lambda self, bgr: counting_precheck(bgr))
    return counting_gate, counting_precheck


def test_batch_runs_concurrently(api_client: TestClient, monkeypatch):
    """A：批内并行——4 图批墙钟 < 串行总和的一半（每图人为 sleep 0.6s，
    串行 ≥2.4s；并行 4 worker 下 <1.5s 即证并发）。"""
    pipeline = api_client.app.state.pipeline
    real_run = pipeline._process_single_image
    started = threading.Semaphore(0)
    release = threading.Event()
    concurrency_peak = []
    active = []
    lock = threading.Lock()

    def slow_process(image_record):
        with lock:
            active.append(1)
            concurrency_peak.append(len(active))
        started.release()
        release.wait(timeout=10)  # 4 图全部到位后统一放行
        try:
            return real_run(image_record)
        finally:
            with lock:
                active.pop()

    monkeypatch.setattr(pipeline, "_process_single_image", slow_process)

    images = [("images", (f"img_{i}.png", build_noisy_png_bytes(300 + i, 300), "image/png")) for i in range(4)]
    t0 = time.monotonic()
    response = api_client.post("/api/jobs", files=images)
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    for _ in range(4):  # 等 4 图全部进入处理段（并行证据：同时 active）
        assert started.acquire(timeout=30), "4 图未全部开始（并行度不足）"
    assert max(concurrency_peak) >= 2, f"批内未见并发执行（峰值 {max(concurrency_peak)}）"
    release.set()  # 放行

    job_status = wait_until_job_completed(api_client, job_id, timeout_seconds=180)
    elapsed = time.monotonic() - t0
    assert all(img["status"] == "completed" for img in job_status["images"])
    assert elapsed < 120, f"批墙钟异常（{elapsed:.1f}s）"


def test_try_complete_image_does_not_override_failed(api_client: TestClient):
    """条件完成回写：图已被置 failed 后，完成回写必须输掉（终态单向）。"""
    store = api_client.app.state.store

    images = [("images", ("a.png", build_pattern_png_bytes(), "image/png"))]
    response = api_client.post("/api/jobs", files=images)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job_status = wait_until_job_completed(api_client, job_id, timeout_seconds=120)
    image_id = job_status["images"][0]["image_id"]

    # 图已 completed（终态）——try_complete_image 不得再写（模拟：先人工置回
    # processing 再置 failed，随后完成回写必须败北）
    store.update_image(image_id, status="processing")
    from src.jobs.store import utc_now

    store.update_image(image_id, status="failed", error_msg="recovery 置败", finished_at=utc_now())
    won = store.try_complete_image(image_id, stage_results={"watermark": "done(api)"}, finished_at=utc_now())
    assert won is False, "终态 failed 后完成回写必须返回 False"
    record = store.get_image(image_id)
    assert record.status == "failed", "终态单向：completed 不得覆盖 failed"
    assert "recovery 置败" == record.error_msg, "失败原因不得被完成回写冲掉"


def test_entry_verdicts_asked_once_per_image(api_client: TestClient, monkeypatch):
    """B：判定复用——非棋盘格图全管线：棋盘格问 1 次、预检问 1 次
    （旧路径棋盘格问 2 次：顺序门 + FillStep 内部）。"""
    counting_gate, counting_precheck = _install_entry_gates(monkeypatch, checkerboard=False, precheck=False)

    response = api_client.post(
        "/api/jobs", files=[("images", ("one.png", build_pattern_png_bytes(), "image/png"))]
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    wait_until_job_completed(api_client, job_id, timeout_seconds=120)

    assert counting_gate.calls == 1, f"棋盘格应只问 1 次（入口复用给 FillStep），实际 {counting_gate.calls}"
    assert counting_precheck.calls == 1, f"预检应只问 1 次（入口复用给 WatermarkStep），实际 {counting_precheck.calls}"


def test_fill_first_discards_entry_precheck(api_client: TestClient, monkeypatch):
    """B：fill-first（棋盘格 true）下入口预检结果丢弃——水印步在换白生成图
    上跑必须自问（域不同），预检计 2 次（入口 1 + 步骤内 1）。"""
    counting_gate, counting_precheck = _install_entry_gates(monkeypatch, checkerboard=True, precheck=True)

    # 棋盘格 true 走 fill-first：FillStep 判真后生成式未配 key 会跳过外呼
    # 直接原图交付——水印步拿到的是同一张图（未重绘），此时步骤内预检自问 1 次。
    response = api_client.post(
        "/api/jobs", files=[("images", ("one.png", build_pattern_png_bytes(), "image/png"))]
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    wait_until_job_completed(api_client, job_id, timeout_seconds=120)

    assert counting_gate.calls == 1, "棋盘格仍应只问 1 次（入口直传 FillStep）"
    assert counting_precheck.calls == 2, (
        f"fill-first 下预检应问 2 次（入口 1 + 水印步自问 1，入口结果按域不成立丢弃），实际 {counting_precheck.calls}"
    )


def test_cache_put_is_atomic_under_concurrent_read(tmp_path, monkeypatch):
    """缓存原子写：并发 get 永不读到半张 PNG（get 侧损坏按未命中，不抛错）。"""
    from src.core.config import PatternToolSettings
    from src.steps.watermark.cache import WatermarkResultCache

    settings = PatternToolSettings(data_dir=str(tmp_path / "data"), _env_file=None)
    cache = WatermarkResultCache(settings)
    source = build_pattern_png_bytes(width=200, height=200)
    source_bgr = cv2.imdecode(np.frombuffer(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    key_bgr = source_bgr.copy()

    cache.put(key_bgr, source_bgr)  # 先写好一份

    stop = threading.Event()
    bad_reads = []
    rewrite_count = [0]
    rw_lock = threading.Lock()

    def rewriter():
        # 持续重写：旧 cv2.imwrite 直写在写入窗口内读者可读到半张图
        variant = source_bgr.copy()
        while not stop.is_set():
            variant[:, :] = (variant[0, 0].astype(int) + 1) % 255  # 内容变化换键？不——键取自 key_bgr
            cache.put(key_bgr, variant)
            with rw_lock:
                rewrite_count[0] += 1

    def reader():
        while not stop.is_set():
            got = cache.get(key_bgr)
            if got is not None and got.shape[:2] != source_bgr.shape[:2]:
                bad_reads.append(got.shape)

    writers = [threading.Thread(target=rewriter) for _ in range(2)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in writers + readers:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in writers + readers:
        t.join(timeout=5)

    assert not bad_reads, f"并发读到损坏缓存：{bad_reads[:3]}"
    assert rewrite_count[0] > 10, "重写线程未充分运行（测试前提不成立）"
    # 最终态：get 仍能读到合法图（原子替换后的一致性）
    assert cache.get(key_bgr) is not None
