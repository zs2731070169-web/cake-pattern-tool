"""pytest 公共夹具：独立临时 data 目录与测试应用实例（不污染真实数据）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 锚定项目根（src 以包形式导入）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.app.main import create_app  # noqa: E402
from src.core.config import PatternToolSettings  # noqa: E402
from src.core.logger import configure_logging  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_logging(test_settings: PatternToolSettings):
    """逐 test 把日志双通道兜底重配到 tmp data_dir（autouse）。

    日志单例在 lifespan 启动段自动创建（用 api_client 的用例已在 TestClient
    启动时指向 tmp）；本夹具兜住不用 api_client 的用例与收集后残留，确保
    任何测试日志不写进真实 data/logs/。configure_logging 幂等替换，无叠加。
    """
    configure_logging(test_settings)
    yield


@pytest.fixture()
def test_settings(tmp_path: Path) -> PatternToolSettings:
    """指向临时目录的 Settings（env_file 不存在时全用字段默认值）。

    连接按库路径隔离（core.database 路径键字典，2026-08-26——无需 reset）。
    """
    return PatternToolSettings(data_dir=str(tmp_path / "data"), _env_file=None)


@pytest.fixture()
def api_client(test_settings: PatternToolSettings):
    """测试客户端（ASGI 内直连，不起端口）。"""
    application = create_app(app_settings=test_settings)
    with TestClient(application) as client:
        yield client
    application.state.pipeline.shutdown()
