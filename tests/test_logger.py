"""logger.py 双通道日志配置测试（2026-08-27 第八次修订）。

断言点：utf-8 中文落盘、级别分通道过滤、幂等不叠 handler、轮转备份、
文件失败降级不炸。fixture 复用 conftest.test_settings（tmp_path 天然隔离）；
用例间 root handler 状态由 configure_logging 幂等重挂兜底，无需手动还原。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from src.core.config import PatternToolSettings
from src.core.logger import (
    _HANDLER_TAG,
    LOG_FILE_BACKUP_COUNT,
    configure_logging,
)


def _own_handlers() -> list[logging.Handler]:
    """当前 root 上本模块挂的 handler（带标记的）。"""
    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False)]


def test_file_channel_writes_utf8_chinese(test_settings: PatternToolSettings):
    """文件通道：INFO 中文经 utf-8 落 data/logs/app.log（不乱码）。"""
    configure_logging(test_settings)
    logging.getLogger("pattern_tool.test").info("VL 预检判定=有水印")

    log_file = test_settings.resolve_data_dir() / "logs" / "app.log"
    assert log_file.exists()
    file_content = log_file.read_text(encoding="utf-8")
    assert "VL 预检判定=有水印" in file_content
    assert "pattern_tool.test" in file_content


def test_console_channel_filters_debug(test_settings: PatternToolSettings):
    """级别分通道：root=DEBUG 下 debug() 落文件但不进控制台（默认 INFO）。"""
    configure_logging(test_settings)
    tagged = _own_handlers()
    console_handlers = [h for h in tagged if not isinstance(h, RotatingFileHandler)]
    file_handlers = [h for h in tagged if isinstance(h, RotatingFileHandler)]

    assert logging.getLogger().level == logging.DEBUG  # root 取两通道较低者
    assert [h.level for h in console_handlers] == [logging.INFO]
    assert [h.level for h in file_handlers] == [logging.DEBUG]


def test_configure_logging_idempotent(tmp_path: Path):
    """幂等：换目录重复 configure 不叠 handler，且写入只落新目录。"""
    first_settings = PatternToolSettings(
        data_dir=str(tmp_path / "first" / "data"), _env_file=None
    )
    second_settings = PatternToolSettings(
        data_dir=str(tmp_path / "second" / "data"), _env_file=None
    )

    configure_logging(first_settings)
    configure_logging(second_settings)
    assert len(_own_handlers()) == 2  # 恰好 console + file，无叠加

    logging.getLogger("pattern_tool.test").info("idempotent probe")
    assert (second_settings.resolve_data_dir() / "logs" / "app.log").exists()
    first_log = first_settings.resolve_data_dir() / "logs" / "app.log"
    if first_log.exists():  # 摘除的旧 handler 已 close，不该再收到新写入
        assert "idempotent probe" not in first_log.read_text(encoding="utf-8")


def test_log_rotation_creates_backup(
    test_settings: PatternToolSettings, monkeypatch: pytest.MonkeyPatch
):
    """轮转：超 maxBytes 滚出 app.log.1，备份数不超 backupCount。"""
    import src.core.logger as logger_module

    monkeypatch.setattr(logger_module, "LOG_FILE_MAX_BYTES", 200)
    configure_logging(test_settings)

    probe_logger = logging.getLogger("pattern_tool.test")
    for sequence_number in range(50):
        probe_logger.info("rotation fill line %03d %s", sequence_number, "x" * 60)

    rotated_backups = sorted(
        p.name for p in (test_settings.resolve_data_dir() / "logs").glob("app.log.*")
    )
    assert "app.log.1" in rotated_backups
    assert len(rotated_backups) <= LOG_FILE_BACKUP_COUNT


def test_file_handler_failure_degrades(
    test_settings: PatternToolSettings, monkeypatch: pytest.MonkeyPatch
):
    """降级：文件 handler 构造抛 OSError 不炸启动，控制台通道仍在。"""
    import src.core.logger as logger_module

    def _broken_file_handler(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(logger_module, "RotatingFileHandler", _broken_file_handler)
    configure_logging(test_settings)  # 不抛异常即通过

    tagged = _own_handlers()
    assert [h for h in tagged if not isinstance(h, RotatingFileHandler)]  # console 存活


def test_lifespan_startup_auto_configures(api_client, test_settings: PatternToolSettings):
    """单例口径（2026-08-27 定案）：lifespan 启动段自动创建日志配置——
    入口不在 main.py，TestClient 起服即已指向本测试的 tmp data_dir。"""
    file_handlers = [
        h for h in _own_handlers() if isinstance(h, RotatingFileHandler)
    ]
    assert file_handlers, "lifespan 启动后应已挂文件通道"
    assert str(test_settings.resolve_data_dir()) in str(
        file_handlers[0].baseFilename
    )
