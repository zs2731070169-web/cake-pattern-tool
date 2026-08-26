"""图片处理线程池全局单例（core 统一配置，2026-08-26 从 http.py 拆出）。

上限 = max_concurrent_processing（0 = 自动 2×核）；uvicorn worker=1
（技术方案 6 节）保证进程级真单例。批内并行子池由管线按批短生命周期
创建（不入单例——随批建随批收）。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.core.executor")

_image_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def get_image_executor(settings: PatternToolSettings) -> ThreadPoolExecutor | None:
    """图片处理主池全局单例（首次调用按配置初始化）。"""
    global _image_executor
    with _executor_lock:
        if _image_executor is None:
            max_workers = settings.max_concurrent_processing or (2 * (os.cpu_count() or 2))
            _image_executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="retouch-image"
            )
            module_logger.debug("core image executor init (workers=%d)", max_workers)
        return _image_executor


def shutdown_executors() -> None:
    """优雅关闭（测试与退出用；不等待残留任务，幂等）。"""
    global _image_executor
    with _executor_lock:
        if _image_executor is not None:
            _image_executor.shutdown(wait=False, cancel_futures=True)
            _image_executor = None
