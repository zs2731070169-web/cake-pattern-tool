"""FastAPI 依赖注入层：路由层经 Depends 从这里取单例，不负责创建。

2026-08-26 重构职责切分：
- 创建归各自模块——config.py（get_settings 惰性建）、database.py
  （get_db_connection 每线程每库）、executor.py（get_image_executor 惰性建）；
- 初始化/关闭归 lifespan.py（FastAPI 生命周期机制）；
- 本模块只把这些单例包装成 FastAPI 依赖（Depends 供给 + 测试可 override）。

依赖链：get_settings → get_job_store → get_pipeline（override 级联生效）。
settings 的解析顺序：app.state.settings（create_app 注入的应用实例配置，
测试传独立 settings 时依赖链整体跟随）→ core.config.get_settings() 全局单例。
"""

from __future__ import annotations

import logging

from fastapi import Depends, Request

from src.core.config import PatternToolSettings, get_settings as _get_core_settings
from src.jobs.pipeline import RetouchPipeline
from src.jobs.store import JobStore

module_logger = logging.getLogger("pattern_tool.app.deps")


# ---- 依赖函数（路由层 Depends 取用；测试用 dependency_overrides 覆盖）----

def get_settings(request: Request) -> PatternToolSettings:
    """配置：app.state.settings 优先（工厂注入的实例配置），回退全局单例。"""
    app_settings = getattr(request.app.state, "settings", None)
    if app_settings is not None:
        return app_settings
    return _get_core_settings()


def get_job_store(
    request: Request, settings: PatternToolSettings = Depends(get_settings)
) -> JobStore:
    """SQLite 读写门面：app.state.store 优先（create_app 建好，写锁/连接池同源），
    无 app 上下文时按 settings 新建（连接池由 core/database.py 单例供给）。"""
    store = getattr(request.app.state, "store", None)
    if store is not None:
        return store
    return JobStore(settings)


def get_pipeline(
    request: Request, store: JobStore = Depends(get_job_store)
) -> RetouchPipeline:
    """修图管线：app.state.pipeline 优先（与 TTL/生命周期同源），回退新建。"""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline
    return RetouchPipeline(_settings_of(store), store)


def _settings_of(store: JobStore) -> PatternToolSettings:
    """从 store 取其构造 settings（私有字段兜底读取）。"""
    return getattr(store, "_settings", None) or _get_core_settings()
