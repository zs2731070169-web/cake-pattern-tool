"""实证脚本：9 张 3600×3600 建批期间，/api/meta 并发可用性对比。

用法：.venv/bin/python scripts/demo_loop_probe.py <port>
- 线程 A：POST /api/jobs 提交 9 张 3600×3600 图 + 9 张 originals（外呼全关，纯本地解码/编码/落盘/落库）
- 线程 B：建批期间每 200ms 打一次 GET /api/meta，记录每次耗时
- 输出：meta 最大/平均耗时 vs 建批总耗时——直接量化「事件循环是否被卡住」
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import requests

BASE = f"http://127.0.0.1:{sys.argv[1]}"
POLL_INTERVAL = 0.2


def build_3600_png(seed: int) -> bytes:
    """3600×3600 照片形态图（平滑主体+稀疏颗粒纹理）：字节 <15MB 上限，
    但解码+重编码仍是建批路径的真实最坏负载（纹理显著拖慢 PNG 编码）。"""
    canvas = np.full((3600, 3600, 3), 255, dtype=np.uint8)
    cv2.circle(canvas, (1800, 1800), 1200, (80, 170, 220), -1)
    rng = np.random.default_rng(seed)
    canvas[::4, ::4] = rng.integers(240, 256, (900, 900, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", canvas)
    assert ok
    data = buf.tobytes()
    assert len(data) < 15 * 1024 * 1024, len(data)
    return data


def main() -> None:
    print("合成 9 张 3600×3600 纹理图（最坏编码形态）…")
    images = [(f"img_{i}.png", build_3600_png(i), "image/png") for i in range(9)]

    meta_latencies: list[float] = []
    stop = threading.Event()

    def probe_meta() -> None:
        session = requests.Session()
        while not stop.is_set():
            t0 = time.perf_counter()
            resp = session.get(f"{BASE}/api/meta")
            elapsed = time.perf_counter() - t0
            assert resp.status_code == 200
            meta_latencies.append(elapsed)
            time.sleep(POLL_INTERVAL)

    prober = threading.Thread(target=probe_meta, daemon=True)
    prober.start()
    time.sleep(0.3)  # 让探针先就位

    t0 = time.perf_counter()
    files = [("images", item) for item in images] + [("originals", item) for item in images]
    resp = requests.post(f"{BASE}/api/jobs", files=files, timeout=300)
    create_seconds = time.perf_counter() - t0
    stop.set()
    prober.join(timeout=5)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    print(f"\n建批响应: 200, job={body['job_id'][:8]}…, images={len(body['image_ids'])}")
    print(f"建批总耗时（客户端视角）: {create_seconds:.2f}s")
    if meta_latencies:
        worst = max(meta_latencies)
        avg = sum(meta_latencies) / len(meta_latencies)
        print(f"建批期间 /api/meta 探针: {len(meta_latencies)} 次, 平均 {avg * 1000:.1f}ms, 最大 {worst * 1000:.1f}ms")
        threshold = 1.0
        verdict = "✅ 事件循环未被卡住（def 化后接口体在线程池，其他请求自由应答）" if worst < threshold else (
            "❌ 事件循环被卡住（若建批期间 meta 出现秒级延迟即循环阻塞特征）"
        )
        print(f"判定: {verdict}")
    else:
        print("（建批太快，探针未采样到）")


if __name__ == "__main__":
    main()
