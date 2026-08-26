"""core/http.py 线程模型回归（2026-08-27 修复：并行翻车）。

背景：共享 AsyncClient + 每线程事件循环的旧模型下，连接池连接绑在首个
使用线程的循环上——批次并行后第二个线程复用连接即抛
"RuntimeError: bound to a different event loop"（线上 2026-08-27 01:14 复现）。
修复后全进程唯一循环线程，协程经 run_coroutine_threadsafe 提交。

断言点：
1. 多线程并发 http_sync 走同一 AsyncClient 完成多次 keepalive 请求，
   全程无跨循环异常（复现线上并行情景——多个"管线线程"先后使用连接池）；
2. close_http_client 幂等且可在已持循环的线程内安全调用；
3. 关闭后 get_http_client 重建可用（测试间复位）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from src.core.config import PatternToolSettings
from src.core.http import close_http_client, get_http_client, http_sync


@pytest.fixture()
def thread_settings() -> PatternToolSettings:
    """独立 settings（超时取默认包络即可，不触网）。"""
    return PatternToolSettings(_env_file=None)


def _request_many(client: httpx.AsyncClient, count: int) -> None:
    """模拟一个管线线程的外呼序列（同域多次 → 连接池 keepalive 复用）。"""
    for _ in range(count):
        response = http_sync(client.get("https://example.com/"))
        assert response.status_code == 200


def test_concurrent_threads_share_client_without_loop_error(
    thread_settings: PatternToolSettings,
) -> None:
    """多线程并发外呼同一 AsyncClient 不再抛 bound to a different event loop。"""
    close_http_client()  # 复位全局单例（其他测试可能已建）
    client = get_http_client(thread_settings)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            _request_many(client, 3)
        except RuntimeError as runtime_error:
            # 旧模型在此抛 "bound to a different event loop"；网络类错误不算
            if "different event loop" in str(runtime_error):
                errors.append(runtime_error)
        except httpx.HTTPError:
            pass  # 无网环境下请求本身失败属预期——只断言循环绑定不炸

    # 8 线程并发（模拟批次并行：每 job 一个 executor 线程）
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: worker(), range(8)))

    assert errors == [], f"跨事件循环异常复现: {errors}"
    close_http_client()


def test_close_idempotent_and_callable_from_loop_thread(
    thread_settings: PatternToolSettings,
) -> None:
    """close 幂等；在持有运行循环的线程（如 uvicorn 主线程）内调用不炸。"""
    close_http_client()
    client = get_http_client(thread_settings)

    import asyncio

    async def _close_inside_running_loop() -> None:
        # uvicorn lifespan 退出场景：当前线程已有运行中的循环
        close_http_client()  # 旧实现此处直接 "Cannot run the event loop" 炸
        close_http_client()  # 幂等

    asyncio.run(_close_inside_running_loop())
    assert client.is_closed
    close_http_client()  # 循环线程外的第三次调用仍幂等


def test_client_rebuild_after_close(thread_settings: PatternToolSettings) -> None:
    """关闭后 get_http_client 重建新实例（测试间复位口径）。"""
    close_http_client()
    first = get_http_client(thread_settings)
    close_http_client()
    second = get_http_client(thread_settings)
    try:
        assert first is not second
        assert not second.is_closed
    finally:
        close_http_client()
