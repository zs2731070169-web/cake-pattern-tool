"""全局 HTTP 工具与共享事件循环线程（core 统一配置，2026-08-26 重构）。

线程模型（2026-08-27 定案，修复并行翻车）：
- 管线主体跑在 ThreadPoolExecutor 线程里（api 层 async 事件循环之外），
  步骤客户端是同步调用——本模块提供 http_sync 适配面：异步传输层
  （连接池复用）+ 同步调用（步骤代码无需 async 化）。
- **全进程唯一后台事件循环线程**承接全部协程：http_sync 经
  run_coroutine_threadsafe 提交到该线程。此前"每线程各建 loop"的旧模型
  在批次并行下翻车——共享 AsyncClient 连接池里的连接（含 anyio Event）
  绑定在首个使用线程的循环上，第二个线程的循环复用该连接即抛
  "RuntimeError: bound to a different event loop"。单循环线程从根上
  消除跨循环复用；lifespan 关闭也因此可从任意线程安全调用。
"""

from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.core.http")

# ---- 全局单例（进程级；worker=1 前提下无竞争） ----
_async_client: httpx.AsyncClient | None = None
_client_lock = threading.Lock()

# 后台事件循环线程（全进程唯一；首次 http_sync/close 时懒启动）
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()
_loop: asyncio.AbstractEventLoop | None = None
_loop_start_lock = threading.Lock()


def get_http_client(settings: PatternToolSettings) -> httpx.AsyncClient:
    """全局共享 AsyncClient（连接池复用；首次调用按配置初始化）。

    超时取各调用场景的最大预算（生成式 40s/石榴去水印 60s/超分 30s 的
    包络）——细分超时由调用方自行收紧，传输层只兜底。客户端本身线程
    安全（协程全在循环线程上执行）。
    """
    global _async_client
    with _client_lock:
        if _async_client is None or _async_client.is_closed:
            max_read = max(float(settings.fill_gen_timeout_seconds),
                          float(settings.shiliu_timeout_seconds),
                          float(settings.scale_timeout_seconds), 30.0)
            _async_client = httpx.AsyncClient(
                timeout=httpx.Timeout(read=max_read, connect=5.0, write=30.0, pool=5.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
            module_logger.debug("core http client init (read_timeout=%.0fs)", max_read)
        return _async_client


def close_http_client() -> None:
    """优雅关闭（测试与退出用；幂等；任意线程可调）。

    aclose 协程提交到循环线程执行（不在调用方线程 run_until_complete——
    uvicorn 主线程自带运行中的循环，那样会炸）。
    """
    global _async_client, _loop_thread, _loop
    with _client_lock:
        client = _async_client
        _async_client = None
    if client is not None and not client.is_closed:
        try:
            http_sync(client.aclose())
        except Exception as close_error:
            module_logger.warning("core http client close failed: %s", close_error)
    # 停循环线程：先退出循环再 join（线程可能尚未启动——首次即 close）
    with _loop_start_lock:
        loop, thread = _loop, _loop_thread
        _loop, _loop_thread, = None, None
        _loop_ready.clear()
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=5)


def _loop_thread_main(ready_event: threading.Event) -> None:
    """循环线程主体：跑事件循环直至被 stop（协程的唯一执行域）。

    finally 用局部引用 close（2026-08-28 修复：close_http_client 停循环前
    会先把全局 _loop 置 None，线程退出时读全局已是 None → AttributeError）。
    """
    global _loop
    loop = asyncio.new_event_loop()
    _loop = loop
    try:
        asyncio.set_event_loop(loop)
        ready_event.set()
        loop.run_forever()
    finally:
        loop.close()


def _ensure_loop_thread() -> asyncio.AbstractEventLoop:
    """取（必要时启动）全局循环线程的事件循环。"""
    global _loop_thread
    if _loop is not None and _loop_thread is not None and _loop_thread.is_alive():
        return _loop
    with _loop_start_lock:
        if _loop is None or _loop_thread is None or not _loop_thread.is_alive():
            _loop_ready.clear()
            _loop_thread = threading.Thread(
                target=_loop_thread_main, args=(_loop_ready,),
                name="core-http-loop", daemon=True,
            )
            _loop_thread.start()
            _loop_ready.wait(timeout=10)
            module_logger.debug("core http loop thread started")
        return _loop  # type: ignore[return-value]


def http_sync(coro):
    """同步调用面：把协程提交到全局循环线程并等待结果（步骤客户端用这个适配）。

    用法：http_sync(client.get(url, headers=...)) → httpx.Response
    并发安全：多个管线线程同时提交，各自阻塞等自己的 future，协程全在
    单循环线程上串行调度——连接池连接永远只在同一个循环上被使用。
    """
    loop = _ensure_loop_thread()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
