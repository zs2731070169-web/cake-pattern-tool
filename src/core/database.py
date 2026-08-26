"""SQLite 连接管理全局单例（core 统一配置，2026-08-26 从 jobs/store.py 拆出）。

线程模型：每线程每库一条连接（threading.local 存 {db_path: connection}
字典）+ row_factory=Row + WAL pragma + busy_timeout=30s。管理器单例
而非"单连接"单例——测试场景（逐 tmp_path 新库）与多库并存由路径键
天然隔离，无需 reset（2026-08-26 实测：单一 _db_path + reset 方案在
"core executor 线程持旧库连接 + WAL 文件锁"下死锁）。
uvicorn worker=1（技术方案 6 节）；生产单库下行为即全局单连接池。
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from src.core.config import PatternToolSettings

module_logger = logging.getLogger("pattern_tool.core.database")

_local = threading.local()


def get_db_connection(settings: PatternToolSettings) -> sqlite3.Connection:
    """当前线程对 settings 所指库的连接（按路径隔离；首次访问时初始化）。

    WAL + busy_timeout + Row factory 统一在此设置；写事务由调用方
    （JobStore._write_lock）串行化。
    """
    db_path = settings.resolve_data_dir() / "pattern_tool.db"
    connections = getattr(_local, "connections", None)
    if connections is None:
        connections = {}
        _local.connections = connections
    connection = connections.get(str(db_path))
    if connection is not None:
        return connection
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connections[str(db_path)] = connection
    return connection


def close_db_connections() -> None:
    """关闭当前线程全部连接（测试与退出用；其他线程随线程消亡）。"""
    connections = getattr(_local, "connections", None) or {}
    for connection in connections.values():
        try:
            connection.close()
        except sqlite3.Error as close_error:
            module_logger.warning("core db close failed: %s", close_error)
    _local.connections = {}
