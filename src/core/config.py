"""全局配置：pydantic-settings 从项目根 .env 加载，键前缀 PT_。

技术方案 7.1：任何模块不得绕过 Settings 自行读 os.environ 或写死配置；
优先级为 真实环境变量 > .env > 字段默认值。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（src/core/ 的上两级），.env 与 data/ 都相对它定位
PROJECT_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


class PatternToolSettings(BaseSettings):
    """pattern-tool 全量配置项（与 .env.example 键一一对应）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT_DIR / ".env"),
        env_prefix="PT_",
        extra="ignore",
    )

    # ---- 服务 ----
    port: int = 8200  # 监听端口（uvicorn :8200）
    data_dir: str = "data"  # 数据库与批次图片根目录（相对项目根）
    log_level: str = "INFO"  # 控制台日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
    log_file_level: str = "DEBUG"  # 文件日志级别（data/logs/app.log 持久化通道，排障主径全量落盘）

    @field_validator("log_level", "log_file_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """日志级别规范化：大小写不敏感 + 拒绝非法值（fail-fast，不带错配跑）。"""
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法日志级别: {value}")
        return normalized

    # ---- 上传校验 ----
    max_images_per_job: int = 9  # 单批图片张数上限（1-9）
    max_image_bytes: int = 15 * 1024 * 1024  # 单张字节上限（15MB）
    min_image_bytes: int = 1024  # 单张字节下限（1KB，拦空文件/残片；过小拦截靠像素下限）
    max_image_pixels: int = 3600  # 解码后单边像素上限（3600px）
    min_image_pixels: int = 60  # 解码后单边像素下限（60px 拦图标/碎片；2026-08-26 从 200 下调——小图上传是用户意志，打印质量由 quality_hint=low-res 提示兜底）
    reject_duplicate_images: bool = True  # 同批内容级查重（SHA-256 重复即 422）

    # ---- 限流 ----
    max_concurrent_jobs_per_ip: int = 9  # 同 IP 进行中批次上限，超限 429（2026-08-26 3→9：同用户多任务并行不排队，只有更晚的批次才可能限流）

    # ---- 管线 ----
    image_process_timeout_seconds: int = 180  # 单图处理时长上限（只计 processing 段；佐糖 110s + 生成式换底 40s + 描边 ~10s 最坏和——佐糖回退后恢复 180，2026-08-26）
    image_queue_timeout_seconds: int = 600  # 排队时长上限，超时置 failed 提示错峰
    max_concurrent_processing: int = 0  # 全局同时 processing 图上限（0 = 自动 2×CPU 核数）

    # ---- TTL ----
    job_ttl_hours: int = 24  # 批次保留时长（小时），过期清理文件与记录
    ttl_scan_interval_hours: int = 1  # 清理器扫描间隔（小时）

    # ---- 描边步（图像处理方案 3.5：1-2mm 浅灰线，MVP 定 1.5mm）----
    outline_width_mm: float = 1.5  # 描边线宽（毫米）
    outline_gray_level: int = 190  # 描边灰线明度（RGB 190,190,190）
    print_dpi: int = 300  # 打印 DPI 基准（low-res 判定与 mm→px 换算依据）
    outline_matting_enabled: bool = True  # 图案分割用 rembg alpha 蒙版（不可用自动回退灰度阈值法）
    outline_matting_model: str = "u2netp"  # rembg 模型名（u2netp 4.5MB；效果不足可换 u2net/isnet）
    outline_matting_alpha_threshold: int = 128  # alpha 二值化阈值（≥ 此值为图案）

    # ---- 填充步（v17：isnet 分割与拓扑填白（Path B）已退役，本地仅剩 Path A 透明合成；
    #      旧字段 fill_matting_model / fill_blank_ratio_threshold /
    #      fill_background_variance_threshold 一并移除，历史配置如残留 .env 会被 extra=ignore 忽略）----

    # ---- 填充步 Path C：qwen-image-2.0 生成式换白底（v16，假 alpha/彩色照片背景）----
    fill_gen_enabled: bool = False  # 是否启用生成式路径（关闭时仅本地 Path A/B，行为与 v15 一致）
    fill_gen_key: str = ""  # 阿里云百炼 DashScope API Key，绝不进前端/日志/错误信息
    fill_gen_model: str = "qwen-image-2.0"  # 模型名（加速版；精细度不足可换 qwen-image-2.0-pro）
    fill_gen_timeout_seconds: int = 40  # 生成+下载等待上限（秒），到限快速降级 Path B 不拖垮全图
    fill_gen_trigger_ratio: float = 0.5  # 触发门：形状边界带"宽白系以外"占比阈值（≥ 此值判背景本地填不了才外呼）

    # （OpenCV 水印检测已退役 2026-08-25——qwen-vl 唯一检测器；旧键
    # PT_WM_SIMPLE_AREA_RATIO / PT_WM_DETECT_CONFIDENCE / PT_WM_DIFF_THRESHOLD /
    # PT_WM_PLATFORM_KEYWORDS 残留配置会被 extra=ignore 忽略）

    # ---- 去水印修复：石榴智能高级版唯一供应商（2026-08-27 第十次修订——佐糖
    #      完全下线，wm_provider 开关删除；成本对比见 docs/cost/）----
    wm_api_enabled: bool = False  # 是否启用修复外呼（关 = 检出也不修，skipped 原图）
    shiliu_api_key: str = ""  # 石榴智能 APIKEY（header 名字面量 APIKEY；去水印+超分共用积分池），绝不进前端/日志/错误信息
    shiliu_timeout_seconds: int = 60  # 石榴去水印异步提交+轮询预算（秒）——async_fetch 3s 间隔轮询至上限

    # ---- 裁剪步（2026-08-28 第二十次修订补配置——五步齐备各步独立开关）----
    crop_enabled: bool = True  # 是否执行声明式裁剪（关 = 不裁框不塑形原图直通，skipped 记档）

    # ---- 超分放大：供应商开关（2026-08-28 第十八次修订——佐糖充值复活，
    #      picwish|shiliu 可切；石榴历版目检不符合预期，超分主力回佐糖）----
    picwish_api_key: str = ""  # 佐糖 X-API-KEY（scale-pro 变清晰；与去水印键独立——去水印已切石榴）
    picwish_timeout_seconds: int = 110  # 佐糖任务等待上限（秒，提交-轮询-下载全程）
    scale_enabled: bool = False  # 是否启用超分外呼（关 = 放大记 failed 不交付）
    scale_timeout_seconds: int = 30  # 石榴超分预算（秒）；佐糖走 picwish_timeout_seconds

    # ---- 去水印段 2：qwen-vl 语义预检（4.4 v3 三段链路）----
    wm_precheck_enabled: bool = False  # OpenCV 零检出图走 VL 预检（判有才调佐糖）
    wm_precheck_key: str = ""  # DashScope API Key（qwen-vl-plus 文本计费，~¥0.003/次）
    wm_precheck_model: str = "qwen-vl-plus"  # VL 模型名（可配——后期更换模型只改环境变量，2026-08-26）

    def resolve_data_dir(self) -> Path:
        """数据根目录绝对路径（相对路径锚定项目根）。"""
        data_dir_path = Path(self.data_dir)
        if not data_dir_path.is_absolute():
            data_dir_path = PROJECT_ROOT_DIR / data_dir_path
        return data_dir_path


# 全局单例槽（2026-08-26 重构：创建职责归本模块 get_settings() 惰性完成，
# 不再 import 时实例化；src/app/deps.py 只做 FastAPI 依赖注入转发。
# uvicorn worker=1，技术方案 6 节，进程内共享一份）
settings: PatternToolSettings | None = None


def get_settings() -> PatternToolSettings:
    """配置单例（惰性创建归 config 模块自己：首次调用建并落全局槽）。"""
    global settings
    if settings is None:
        settings = PatternToolSettings()
    return settings
