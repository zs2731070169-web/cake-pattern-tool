"""修图管线：批次内逐图串行执行三步，各步独立跳过，回写状态与提示。

技术方案 5.2 图 B：
- 状态机 queued → processing → completed / failed（单向）；
- 批内隔离：单图失败不影响其他图；
- 超时：单图 60s 只计 processing 段；排队超 10 分钟置 failed；
- 全局并发：服务级信号量，同时 processing ≤ 2×CPU 核数；
- quality_hint：heavy-watermark / suggest-larger-source 汇总（auto-hd 自动
  提升档 2026-08-28 第二十二次修订删除——尺寸缩放严格跟随用户声明）。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from src.core.config import PatternToolSettings
from src.jobs.store import JobImageRecord, JobStore, utc_now
from src.steps.fill.filling import FillStep
from src.steps.imaging import ImageDecodeError, decode_to_ndarray, encode_png
from src.steps.outline import OutlineStep
from src.steps.watermark import WatermarkStep

module_logger = logging.getLogger("pattern_tool.pipeline")

# 客户失败话术（脱敏口径：不含服务端路径/堆栈，技术方案 4.3）
GENERIC_FAILURE_MESSAGE = "处理失败，请更换图片重试"
QUEUE_TIMEOUT_MESSAGE = "排队人数较多，请稍后错峰重试"

class RetouchPipeline:
    """逐图编排：去水印 → 填充 → 描边 → 回写。"""

    def __init__(self, settings: PatternToolSettings, store: JobStore) -> None:
        self._settings = settings
        self._store = store
        self._watermark_step = WatermarkStep(settings)
        self._fill_step = FillStep(settings)
        self._outline_step = OutlineStep(settings)

        # 全局并发上限：同时 processing 图 ≤ 2×核数（0 表示自动，6 节容量防线）
        max_concurrency = settings.max_concurrent_processing or (2 * (os.cpu_count() or 2))
        self._processing_semaphore = threading.Semaphore(max_concurrency)

        # 图片处理主池：core 全局单例（2026-08-26 重构——多管线实例共享，
        # 配额统一；批内并行子池仍按批短生命周期创建不入单例）
        from src.core.executor import get_image_executor

        self._image_executor = get_image_executor(settings)

    # ---- 对外入口 ----

    def submit_job(self, job_id: str) -> None:
        """受理一个批次：逐图投递执行（立即返回，不阻塞 API 响应）。"""
        self._image_executor.submit(self._run_job, job_id)

    def shutdown(self) -> None:
        """优雅关闭（测试与退出用；主池是 core 单例不在此关，见 main.py lifespan）。"""
        pass

    # ---- 内部编排 ----

    def _run_job(self, job_id: str) -> None:
        """批次入口：批内图片串行处理（2026-08-26 21:58 用户定案撤回批内并行——
        并行使悬挂图状态回写与 recovery/TTL 产生竞态（test_startup_recovery
        偶发 'processing' != 'failed'），多任务优化方案将重新设计）。
        批次间仍并行（每个 job 一个 executor 任务），全局闸 Semaphore 不变。
        """
        try:
            images = self._store.get_job_images(job_id)
            for image_record in images:
                self._process_single_image(image_record)
            self._store.complete_job_if_done(job_id)
        except Exception as job_error:  # 批次级兜底：不遗留 processing 悬挂
            module_logger.exception("job %s orchestration failed", job_id)
            remaining = self._store.get_job_images(job_id)
            for image_record in remaining:
                if image_record.status in ("queued", "processing"):
                    self._mark_failed(image_record.image_id, GENERIC_FAILURE_MESSAGE)

    def _process_single_image(self, image_record: JobImageRecord) -> None:
        """单图全流程：排队 → 处理（60s 软超时）→ 终态回写。"""
        acquired = self._processing_semaphore.acquire(timeout=self._settings.image_queue_timeout_seconds)
        if not acquired:
            # 排队超 10 分钟：置 failed 提示错峰（排队段超时口径，6 节）
            self._mark_failed(image_record.image_id, QUEUE_TIMEOUT_MESSAGE)
            return
        try:
            self._store.update_image(image_record.image_id, status="processing")
            process_start_timestamp = utc_now()

            image_bytes = self._read_input_file(image_record.input_path)
            # 原始未裁剪域（去水印缓存域，4.4）：前端裁剪过的图带 orig_{seq}.png
            original_bytes = self._read_optional_file(
                self._original_relative_path(image_record)
            )
            result_bytes, stage_results, quality_hint = self._run_steps_with_timeout(
                image_bytes, image_record.stage_results.get("crop"), original_bytes
            )

            result_relative_path = self._write_result_file(
                image_record.job_id, image_record.seq, result_bytes
            )
            # 放大失败整图 failed 不交付（2026-08-28 第十七次修订，用户定案
            # "失败了就失败了"）：插值废图不产出——超分失败说明该图放大后
            # 不可用，客户提示换更清晰原图比拿废图强（与去水印 failed 同构）
            if stage_results.get("resize") == "failed":
                self._mark_failed(
                    image_record.image_id,
                    "图片放大后清晰度不足，请提供更清晰的原图重试",
                )
                return
            elapsed_seconds = (utc_now() - process_start_timestamp).total_seconds()
            if elapsed_seconds > self._settings.image_process_timeout_seconds:
                # 超时图不入成品（软超时兜底：步骤已完成但整体超预算也按失败）
                self._mark_failed(image_record.image_id, "处理超时，请稍后重试")
                return

            self._store.update_image(
                image_record.image_id,
                status="completed",
                stage_results=stage_results,
                quality_hint=quality_hint,
                result_path=result_relative_path,
                finished_at=utc_now(),
            )
            module_logger.info(
                "image done job=%s image=%s seq=%s stages=%s hint=%s elapsed=%.1fs",
                image_record.job_id, image_record.image_id, image_record.seq,
                stage_results, quality_hint, elapsed_seconds,
            )
        except ImageDecodeError as decode_error:
            self._mark_failed(image_record.image_id, str(decode_error))
        except Exception:
            module_logger.exception(
                "image failed job=%s image=%s", image_record.job_id, image_record.image_id
            )
            self._mark_failed(image_record.image_id, GENERIC_FAILURE_MESSAGE)
        finally:
            self._processing_semaphore.release()

    def _run_steps_with_timeout(
        self, image_bytes: bytes, crop_meta_json: str | None = None, original_bytes: bytes | None = None
    ) -> tuple[bytes, dict[str, str], str]:
        """三步串行执行（带 120s 软超时）。

        单图超时按 failed 处理（图 B 异常分支）——实现上用独立工作线程跑三步，
        主线程限时等待；超时后工作线程的后续回写由状态机拦截（failed 终态不回退）。
        """
        step_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retouch-steps")
        try:
            future = step_executor.submit(self._run_steps, image_bytes, crop_meta_json, original_bytes)
            return future.result(timeout=self._settings.image_process_timeout_seconds)
        except FutureTimeoutError:
            raise TimeoutError("单图处理超时") from None
        finally:
            # 不等待残留线程（Python 无法强杀；其结果被状态机丢弃）
            step_executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _original_relative_path(image_record: JobImageRecord) -> str:
        """原始域文件相对路径（orig_{seq}.png，与 in 同目录；无则空串）。"""
        return image_record.input_path.replace(f"in_{image_record.seq}.png", f"orig_{image_record.seq}.png")

    def _read_optional_file(self, relative_path: str) -> bytes | None:
        """读可选文件（不存在返回 None，不抛出——原始域缺省回退裁剪版域）。"""
        try:
            return (self._settings.resolve_data_dir() / relative_path).read_bytes()
        except OSError:
            return None

    def _run_steps(
        self, image_bytes: bytes, crop_meta_json: str | None = None, original_bytes: bytes | None = None
    ) -> tuple[bytes, dict[str, str], str]:
        """四步实际执行（在步骤工作线程内跑，避免阻塞主线程超时判定）。

        **全程原始域（2026-08-26 定案）**：有 originals 时管线处理原图
        （裁剪版仅前端预览）——去水印/填充的判定/外呼/缓存都在原图内容上，
        同图不同形状天然同键，模型零重复外呼；真正的裁剪由 CropStep 在
        填充之后运行时执行（前端只声明 shape+框）。无 originals 回退裁剪版。
        """
        crop_meta = self._parse_crop_meta(crop_meta_json)
        # 原始域判定（2026-08-26 声明式定案）：新前端主图即原图（不再本地
        # 裁剪），crop_meta.data 框坐标即相对主图，直接可用；originals 是
        # 旧前端兼容（裁剪版主图 + orig 原图），orig 可用时优先原图。
        # 两者皆无 → 主图按原图对待（crop_meta 仍生效——shape 总是可靠的）。
        working_bytes = image_bytes
        crop_meta_for_crop = crop_meta
        if original_bytes:
            try:
                decode_to_ndarray(original_bytes, self._settings.max_image_pixels)  # 可解码性验证
                working_bytes = original_bytes
            except Exception:
                working_bytes = image_bytes
        image_bgr = decode_to_ndarray(working_bytes, self._settings.max_image_pixels)
        crop_shape = crop_meta.get("shape") if crop_meta else None

        stage_results: dict[str, str] = {}
        quality_hint = "none"
        # （2026-08-28 第二十次修订：PT_STEPS_ENABLED 步骤总闸已删——与各步
        # 独立开关冗余且有"只改记档不拦截执行"前科。管线恒跑五步，每步
        # 跑不跑由自身业务条件决定：没水印→skipped、非棋盘格→skipped、
        # 未配 key→降级；单步调试用各步开关单开等价达成。）

        # ---- 双判定前移 + 合并调用（2026-08-26 定案）----
        # 在**原始图**上并行两问 qwen-vl：①有无水印 ②棋盘格背景。双 true 时
        # 一次 qwen-image 合并生成（去水印+格子换白一次出图）——去水印生成会
        # 重绘棋盘格背景致填充判定失效（03:30 实测翻车），合并消除该冲突。
        # stage_results 双键分开记档（watermark=done(api) + fill=白色背景）——
        # 步骤契约不合并；成本算 watermark 侧。失败回退两步各自原路径。
        # ---- 棋盘格顺序门（2026-08-26 方案A'：两步职责纯净，只换顺序）----
        # 原始图问 VL 棋盘格：true → 填充先行（格子→纯白；白底不怕后续
        # 去水印的二次生成——本来就要它纯白）再去残留水印；false → 正常
        # 顺序（正常顺序下去水印会重绘棋盘格致填充判定失效，03:30 翻车）。
        wm_first = not self._is_checkerboard_image(image_bgr)
        ordered_steps = ["watermark", "fill"] if wm_first else ["fill", "watermark"]

        for step_name in ordered_steps:
            if step_name == "watermark":
                # 去水印——管线已在原始域，修复结果即原图域整幅直传下一步
                # （不按 crop 偏移裁窗映射：旧架构遗留会致往返缩放画质损失）。
                watermark_result = self._watermark_step.run(image_bgr)
                image_bgr = watermark_result.image_bgr
                stage_results["watermark"] = watermark_result.stage_value
                if watermark_result.quality_hint == "heavy-watermark":
                    quality_hint = "heavy-watermark"
            else:
                # 填充（形状无关——判定/外呼/缓存全在整图域，2026-08-25 定案；
                # 原始域下同图不同形状同键，模型只调一次）
                fill_result = self._fill_step.run(image_bgr)
                image_bgr = fill_result.image_bgr
                stage_results["fill"] = fill_result.stage_value

        # 步 3：裁剪（CropStep，2026-08-26 独立 step）——按声明运行时执行：
        # 框裁外接区 + 形状掩膜塑形。原始域下框坐标有效；裁剪版域退化塑形。
        # crop_enabled=false（2026-08-28 第二十次修订）→ 不裁框不塑形原图
        # 直通，skipped 记档（下游形状描边/缩放形状重画随声明缺失自然退化）。
        from src.steps.crop import CropStep, CropStepResult

        if self._settings.crop_enabled:
            crop_result = CropStep().run(image_bgr, crop_meta_for_crop)
            image_bgr = crop_result.image_bgr
        else:
            module_logger.info("crop: 配置关闭（PT_CROP_ENABLED=false）→ 原图直通")
            crop_result = CropStepResult(image_bgr, "skipped")
        # 记档约定：crop 字段始终记声明 JSON（含 shape 供前端回显/排查），
        # 执行与否看 resize 等下游是否收到塑形图；无声明才记 "skipped"。
        # （2026-08-26 14:49 起 CropStep 对无框合法声明也执行塑形——
        # "每图必有形状"兑现，形状外真 transparent）
        stage_results["crop"] = crop_meta_json or "skipped"
        if crop_result.stage_value != "skipped":
            stage_results["crop_applied"] = crop_result.stage_value

        # 步 4：尺寸缩放（2026-08-28 第十九次修订前置——原步 5 提前：
        # 描边必须在目标幅直画，避免佐糖定倍+尾程非常规倍率链拉伸灰线
        # 导致锯齿与线宽物理漂移；见下方描边步注释）。按 crop_meta.size.cm
        # 声明缩放到 @300DPI 目标短边；放大走佐糖 scale-pro 变清晰（非裸插值）。
        size_cm = None
        if crop_meta and isinstance(crop_meta.get("size"), dict):
            try:
                size_cm = float(crop_meta["size"].get("cm"))
            except (TypeError, ValueError):
                size_cm = None
        from src.steps.resize import ResizeStep

        resize_shape = crop_shape if crop_shape in ("circle", "heart", "star") else None
        resize_result = ResizeStep(self._settings).run(image_bgr, size_cm, resize_shape)
        image_bgr = resize_result.image_bgr
        stage_results["resize"] = resize_result.stage_value

        # 自动变清晰提升（2026-08-26 16:22 用户定案：不再提示"换高清图"，
        # 小图自动变清晰到默认分辨率）。未显式选尺寸且短边低于默认分辨率的
        # 90% 时，自动按 8 寸（19cm→2244px@300DPI）档提升——选档理由：
        # 常见蛋糕尺寸中档，3425 上限的 65%，小图硬拉上限无增益反费 API；
        # 已显式选尺寸的图上面 ResizeStep 已处理，不重复。提升不设 low-res
        # 提示（用户意志：自动处理好，不用提醒）。
        # （2026-08-28 第二十二次修订：auto-hd 自动变清晰已删——与前端
        # "不设置（原图大小）"承诺矛盾（用户没选尺寸却被放大），且与佐糖
        # 缩放启用逻辑不一致：缩放外呼只由用户显式选尺寸触发，不设置 =
        # 严格原幅直通。2026-08-26 16:22 的自动提升定案就此撤销。）

        # 尾程提示聚合（2026-08-28）：resize 判定源图偏小（佐糖出幅 < 目标，
        # 尾程稀释细节）→ 透传给前端显示"建议换更大图"（不阻塞交付）。
        if resize_result.quality_hint == "suggest-larger-source":
            quality_hint = "suggest-larger-source"

        # 步 5：描边（2026-08-28 第十九次修订——管线最末步，目标幅直画：
        # 线宽 px = mm×DPI/25.4 在交付幅精确落地，零拉伸零振铃；旧顺序
        # [描边@源幅→缩放] 在佐糖服务端定倍 ×5.7+尾程 ×1.53 非常规倍率链下
        # 灰线被拉出 LANCZOS 振铃锯齿 + 物理线宽漂 4 倍（2px→9.7px=0.82mm，
        # 配置 0.2mm），心形真图实锤。白底判定与 rembg 分割随之在高幅跑，
        # 分割质量更好，代价 rembg 大图耗时 +1-2s）。
        outline_result = self._outline_step.run(image_bgr, crop_shape)
        image_bgr = outline_result.image_bgr
        stage_results["outline"] = outline_result.stage_value

        return encode_png(image_bgr), stage_results, quality_hint

    def _is_checkerboard_image(self, image_bgr: np.ndarray) -> bool:
        """棋盘格顺序门（2026-08-26 方案A'）：原始图问 VL 背景，true 则
        管线顺序换为填充→去水印。两步职责纯净；判定失败按 False 走正常
        顺序（去水印→填充）。"""
        from src.steps.fill.gate_vl import CheckerboardGate

        try:
            gate = CheckerboardGate(self._settings)
            if not gate.is_configured():
                return False
            verdict = gate.has_checkerboard_background(image_bgr)
            module_logger.debug("checkerboard order gate: %s", verdict)
            return verdict
        except Exception as gate_error:
            module_logger.warning("checkerboard gate error: %s", gate_error)
            return False

    @staticmethod
    def _parse_crop_meta(crop_meta_json: str | None) -> dict | None:
        """裁剪声明 JSON → dict（畸形按 None，不阻塞处理）。"""
        if not crop_meta_json:
            return None
        try:
            import json

            parsed = json.loads(crop_meta_json)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None

    # ---- 文件 IO ----

    def _read_input_file(self, input_relative_path: str) -> bytes:
        """读原图字节（相对 data/ 路径 → 绝对路径）。"""
        absolute_path = self._settings.resolve_data_dir() / input_relative_path
        return absolute_path.read_bytes()

    def _write_result_file(self, job_id: str, seq: int, result_bytes: bytes) -> str:
        """结果 PNG 落盘 data/jobs/{job_id}/out_{seq}.png，返回相对路径。"""
        result_relative_path = f"jobs/{job_id}/out_{seq}.png"
        absolute_path = self._settings.resolve_data_dir() / result_relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(result_bytes)
        return result_relative_path

    def _mark_failed(self, image_id: str, error_message: str) -> None:
        """置 failed 并回填脱敏 error_msg；同时确保批次聚合状态推进。"""
        current_record = self._store.get_image(image_id)
        if current_record is None or current_record.status in ("completed", "failed"):
            return  # 终态不回退（状态机单向，6 节）
        self._store.update_image(
            image_id, status="failed", error_msg=error_message, finished_at=utc_now()
        )
