"""应用入口：uvicorn 启动（worker=1，技术方案 6 节 SQLite 单写者前提）。

用法：.venv/bin/python -m uvicorn src.app.main:app --host 0.0.0.0 --port 8200
（生产经 Nginx 反代 HTTPS，见技术方案 7.1 反代三条口径）

装配职责（2026-08-26 重构定稿）：
- main.py：应用创建 + 中间件 + 路由 include + 静态前端挂载；
- api.py：APIRouter 接口声明（无 app 无中间件）；
- deps.py：FastAPI 依赖注入；
- lifespan.py：core 单例初始化/关闭（生命周期机制）。

日志不在本模块配置（2026-08-27 第八次修订）：src/core/logger.py 的双通道
（控制台 + data/logs/app.log 轮转）由 lifespan 启动段自动创建，见该模块。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.app.api import router
from src.app.lifespan import lifespan
from src.core.config import PROJECT_ROOT_DIR, PatternToolSettings
from src.jobs.pipeline import RetouchPipeline
from src.jobs.store import JobStore


def create_app(
    app_settings: PatternToolSettings | None = None,
) -> FastAPI:
    """构建 FastAPI 应用（工厂模式：测试注入独立 settings）。"""
    from src.core.config import get_settings as get_core_settings

    effective_settings = app_settings or get_core_settings()

    app = FastAPI(
        title="pattern-tool",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # 同源部署（StaticFiles 前端），CORS 仅放开本地联调
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:{}".format(effective_settings.port), "http://127.0.0.1:{}".format(effective_settings.port)],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API 路由统一挂载（api.py 的 APIRouter，prefix=/api 在其内声明）
    app.include_router(router)

    # 业务对象建好挂 state：deps 依赖链与 lifespan（TTL/管线收口）由此取
    store = JobStore(effective_settings)
    app.state.store = store
    app.state.pipeline = RetouchPipeline(effective_settings, store)
    app.state.settings = effective_settings

    # 静态前端（同进程单页）——mount 在 "/"，API 路由已先注册不受影响
    web_directory = PROJECT_ROOT_DIR / "web"
    if web_directory.exists():
        app.mount("/", StaticFiles(directory=str(web_directory), html=True), name="web")
    return app


app = create_app()
