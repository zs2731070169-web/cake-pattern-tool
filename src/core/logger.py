"""结构化日志配置：控制台 + data/logs/app.log 轮转双通道（2026-08-27 第八次修订）。

单例口径：全进程一份配置由本模块独占管理，配置入口不在 main.py——
**lifespan 启动时自动创建**（src/app/lifespan.py 启动段调用，取
app.state.settings，测试注入 tmp / 生产全局），重复调用是"替换旧 handler"
而非"叠加"（幂等）。启动段之前的导入期日志（http/executor 单例 init 等
2-3 行）在 root 未配置时按默认丢弃，属已知取舍——业务日志全部在启动后。

级别分通道（"根据不同级别输出"的实现口径）：
- 控制台 settings.log_level（默认 INFO，6 节观测口径刷屏最小化）；
- 文件 settings.log_file_level（默认 DEBUG——去水印/填充生成式链路排障主径全量落盘）。

设计要点：
- 幂等替换：重复调用先摘除本模块旧 handler 再重挂——不摘除会 handler 叠加且
  写向旧目录；不用 basicConfig（root 已有 handler 时它是 no-op，掩盖配置失败）；
- handler 打标：摘除时只认 _HANDLER_TAG 标记的，不误伤 pytest/uvicorn 挂的；
- root 级别取两通道较低者：logger effective level 会先于 handler 拦截，
  root 不开 DEBUG 则 pattern_tool.* 的 debug() 到不了文件 handler；
- 文件创建失败（权限/只读盘 OSError）降级仅控制台，不炸启动（日志不能反噬服务）；
- RotatingFileHandler 自带模块级锁，管线线程池/TTL 线程/后台事件循环线程并发写安全。

不收编的 logger（决策存档）：uvicorn / uvicorn.access 自带 handler 且
propagate=False，留 uvicorn 自己管（收编需动其 handler 链，超出最小闭环）。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from src.core.config import PatternToolSettings, get_settings

module_logger = logging.getLogger("pattern_tool.core.logger")

# 单文件 10MB，备份 5 份，封顶 ~50MB（防磁盘无限增长——TTL 不清理日志）
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5

# 幂等摘除标记：只摘自己挂的 handler，不碰第三方（pytest caplog 等）
_HANDLER_TAG = "_pattern_tool_handler"

# 两通道同源格式：threadName 是排障刚需（四类写日志线程——uvicorn 事件循环/
# 管线线程池 worker/TTL 守护线程/后台事件循环线程，肉眼分线程全靠它）
_LOG_FORMAT = "%(asctime)s [%(threadName)s] %(levelname)s %(name)s %(message)s"

# root 开 DEBUG 后防三方 DEBUG 刷爆文件通道（业务语义日志已由各 step 打点覆盖；
# python_multipart 实测每次上传打 ~30 条表单解析 DEBUG，高频必噪）
_THIRD_PARTY_QUIET_LOGGERS = ("httpx", "rembg", "onnxruntime", "PIL", "python_multipart")


def configure_logging(settings: PatternToolSettings | None = None) -> None:
    """配置 root logger 双通道（幂等替换，重复调用不叠加 handler）。

    settings 缺省取 config.get_settings() 全局单例——启动链路无需显式传参，
    日志作为单例自动就绪；测试传 tmp settings 定向重配。
    """
    effective_settings = settings or get_settings()
    root_logger = logging.getLogger()

    # ---- 幂等：摘除本模块上一轮挂的 handler（测试逐 test 重建 app 场景）----
    for existing_handler in list(root_logger.handlers):
        if getattr(existing_handler, _HANDLER_TAG, False):
            root_logger.removeHandler(existing_handler)
            existing_handler.close()

    # ---- root 级别 = 两通道较低者（取较高者会拦掉低级别日志到不了任一 handler）----
    console_level = getattr(logging, effective_settings.log_level, logging.INFO)
    file_level = getattr(logging, effective_settings.log_file_level, logging.DEBUG)
    root_logger.setLevel(min(console_level, file_level))

    log_formatter = logging.Formatter(_LOG_FORMAT)

    # ---- 通道一：控制台（服务前台观测，默认 INFO）----
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(log_formatter)
    setattr(console_handler, _HANDLER_TAG, True)
    root_logger.addHandler(console_handler)

    # ---- 通道二：文件持久化（data/logs/app.log，默认 DEBUG 全量落盘）----
    try:
        logs_directory = effective_settings.resolve_data_dir() / "logs"
        logs_directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_directory / "app.log",
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",  # 业务日志含中文（如"VL 预检判定=有水印"）
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(log_formatter)
        setattr(file_handler, _HANDLER_TAG, True)
        root_logger.addHandler(file_handler)
    except OSError as directory_error:
        # 降级不炸启动：日志落盘失败只损失观测能力，服务本身必须能起
        console_handler.setLevel(min(console_level, logging.WARNING))
        module_logger.warning(
            "log file init failed, console-only mode: %s", directory_error
        )

    # ---- 三方降噪：INFO/DEBUG 层面的请求噪音不进任何通道 ----
    for third_party_name in _THIRD_PARTY_QUIET_LOGGERS:
        logging.getLogger(third_party_name).setLevel(logging.WARNING)

