"""应用生命周期：core 单例（数据库连接 / 线程池 / HTTP 客户端）的初始化与关闭。

2026-08-26 从 main.py 拆出独立模块——init/close 全部经 FastAPI lifespan 机制
触发，main.py 只做入口装配（create_app(lifespan=...)），不再持有生命周期细节。

职责边界：
- 创建归各自 core 模块（config.get_settings / database.get_db_connection /
  executor.get_image_executor / http.get_http_client 惰性建）；
- 本模块只决定"何时"——启动 yield 前依次预热，退出 yield 后逆序释放；
- 日志配置单例在启动段自动创建（core/logger.py，2026-08-27 第八次修订）；
- TTL 清理线程与管线的启停同在此收口（与库连接同生命周期）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.core.config import PatternToolSettings, get_settings
from src.core.logger import configure_logging
from src.jobs.ttl import TTLCleaner

module_logger = logging.getLogger("pattern_tool.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：预热数据库连接 + 图片处理线程池 + TTL 恢复扫描；
    退出：逆序停清理线程 → 关管线 → 关线程池 → 关库连接 → 关 HTTP 客户端。"""
    settings: PatternToolSettings = getattr(app.state, "settings", None) or get_settings()

    # 日志单例自动创建（2026-08-27 第八次修订）：启动时配好控制台/文件双通道
    # （入口在此不在 main.py；幂等，测试逐 test 重建 app 时替换旧 handler）。
    configure_logging(settings)

    # ---- 启动段（yield 前）：初始化 ----
    from src.core.database import get_db_connection
    from src.core.executor import get_image_executor

    # 图片处理线程池（core/executor.py 创建；worker 数按 settings）
    get_image_executor(settings)
    # 主线程预热一条库连接（core/database.py 每线程每库；schema 已随
    # create_app 的 JobStore 构造就绪）
    get_db_connection(settings)
    module_logger.info("core singletons ready (executor pool / db connection)")

    # TTL 清理器：启动先跑一次恢复扫描（悬挂 processing 置 failed），再周期清扫
    cleaner = TTLCleaner(settings, app.state.store)
    cleaner.start()

    try:
        yield
    finally:
        # ---- 退出段（yield 后）：逆序关闭 ----
        cleaner.stop()
        app.state.pipeline.shutdown()

        from src.core.database import close_db_connections
        from src.core.executor import shutdown_executors
        from src.core.http import close_http_client

        shutdown_executors()   # 图片处理线程池（不等待残留任务）
        close_db_connections()  # 当前线程全部库连接
        close_http_client()     # 全局 HTTP 连接池
        module_logger.info("core singletons released (executor / db / http)")
