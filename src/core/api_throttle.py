"""第三方 API 并发闸（第三十八次修订）——按供应商限流的任务队列。

背景（2026-08-29 22:55 九图批实锤）：第二十六次修订批内并行下，9 张图
同时到 resize 步、9 路佐糖 scale-pro 提交瞬间并发——供应商并发/频率上限
直接 429（前 3 个成功后连续 4 个 Too Many Requests，0.1~0.5s 即拒），
4 图按第十七次修订语义 failed。同构风险面：入口双 VL（每图 2 问）、
石榴去水印、qwen-image 填充。

设计口径：
- **任务周期粒度**持闸：提交-轮询-下载整体排队（"队列"语义——限制的是
  在途第三方任务数，不只是单次 HTTP）；管线线程在闸前阻塞等位。
- 用 `threading.Semaphore`（不用 asyncio.Semaphore）：一个任务周期跨
  多个 http_sync 协程（submit/poll×N/download 各自独立提交到循环线程），
  asyncio 信号量无法跨协程安全持有/释放。
- 按供应商命名隔离：佐糖/百炼/石榴各有独立上限，互不挤占。
- 首个使用者的 limit 注册后生效（同名必须配同一 limit——limit 全部来自
  Settings 字段，进程内天然一致）。
- 闸等待 >0.5s 记 INFO（运维可见排队情况）；limit<=0 直通不限流。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

module_logger = logging.getLogger("pattern_tool.core.api_throttle")

_provider_slots: dict[str, threading.Semaphore] = {}
_registry_lock = threading.Lock()


@contextmanager
def provider_slot(provider: str, limit: int):
    """供应商并发闸：同 provider 最多 limit 个任务周期在途，超额排队。

    用法（客户端任务周期方法体外套一层）：
        with provider_slot("picwish", settings.picwish_max_concurrent):
            ... 提交-轮询-下载全程 ...
    limit<=0 = 不限流（直通）；闸等待超过 0.5s 记 INFO 一条排队观测。
    """
    if limit is None or int(limit) <= 0:
        yield
        return
    with _registry_lock:
        semaphore = _provider_slots.setdefault(
            provider, threading.Semaphore(int(limit))
        )
    wait_started = time.monotonic()
    semaphore.acquire()
    try:
        waited = time.monotonic() - wait_started
        if waited > 0.5:
            module_logger.info(
                "api-throttle %s: 排队 %.1fs 后获得并发位（limit=%d）",
                provider, waited, int(limit),
            )
        yield
    finally:
        semaphore.release()
