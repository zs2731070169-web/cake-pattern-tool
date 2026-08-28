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
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from src.core.config import PatternToolSettings
from src.jobs.store import JobImageRecord, JobStore, utc_now
from src.steps.fill.filling import FillStep
from src.steps.imaging import ImageDecodeError, decode_to_ndarray, encode_png, ensure_bgra
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

        # 图片处理主池：core 全局单例（2026-08-26 重构——多管线实例共享，
        # 配额统一）。并发上限 = executor max_workers = max_concurrent_processing
        # （0=自动 2×CPU，6 节容量防线；2026-08-28 第二十六次修订删
        # _processing_semaphore——旧"信号量阻塞等位"占住 worker 干等排队，
        # 排队超时改投递时刻 deadline 检查，并发闸由 worker 数直接承担）
        from src.core.executor import get_image_executor

        self._image_executor = get_image_executor(settings)

    # ---- 对外入口 ----

    def submit_job(self, job_id: str) -> None:
        """受理一个批次：逐图投递执行（立即返回，不阻塞 API 响应）。

        2026-08-28 第二十六次修订：批内图并行——每图一个 executor 任务，
        撤 2026-08-26 串行定案（当年翻车根因是无条件完成回写竞态，
        try_complete_image 条件更新已在 DB 层闭环，非并行本身之罪）。
        enqueue 时间随任务入参传递，排队超时在任务启动时判定。
        """
        try:
            images = self._store.get_job_images(job_id)
        except Exception:
            module_logger.exception("job %s submit failed: cannot read images", job_id)
            return
        for image_record in images:
            self._image_executor.submit(self._run_single_image, image_record, utc_now())

    def shutdown(self) -> None:
        """优雅关闭（测试与退出用；主池是 core 单例不在此关，见 main.py lifespan）。"""
        pass

    # ---- 内部编排 ----

    def _run_single_image(self, image_record: JobImageRecord, enqueued_at) -> None:
        """单图任务（批内并行，每图独立失败隔离 + 终态后聚合批次状态）。"""
        try:
            queued_seconds = (utc_now() - enqueued_at).total_seconds()
            if queued_seconds > self._settings.image_queue_timeout_seconds:
                # 排队超 10 分钟：置 failed 提示错峰（6 节排队段超时口径；
                # 旧"信号量 acquire(timeout) 阻塞等位"的 deadline 等价迁移）
                self._mark_failed(image_record.image_id, QUEUE_TIMEOUT_MESSAGE)
                return
            self._process_single_image(image_record)
        except ImageDecodeError as decode_error:
            self._mark_failed(image_record.image_id, str(decode_error))
        except Exception:
            module_logger.exception(
                "image failed job=%s image=%s", image_record.job_id, image_record.image_id
            )
            self._mark_failed(image_record.image_id, GENERIC_FAILURE_MESSAGE)
        finally:
            # 每图终态各触发一次聚合判定：COUNT=0 才置批 completed——
            # 并发下幂等（UPDATE WHERE status='processing' + rowcount）
            self._store.complete_job_if_done(image_record.job_id)

    def _process_single_image(self, image_record: JobImageRecord) -> None:
        """单图全流程：置 processing → 五步（180s 软超时）→ 终态回写。"""
        process_start_timestamp = utc_now()
        # started_at 随 processing 一并落库（2026-08-28 第二十四次修订）：
        # 前端"仅执行中计时"的服务端真值锚点——排队等待天然排除在外
        self._store.update_image(
            image_record.image_id, status="processing", started_at=process_start_timestamp
        )

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

        # 条件完成回写（第二十六次修订）：WHERE status='processing'——
        # 与 recovery/TTL 置的 failed 竞态输掉时整图丢弃（out 文件成孤儿
        # 由 TTL 24h 清理），终态单向在 DB 层闭环
        completed = self._store.try_complete_image(
            image_record.image_id,
            stage_results=stage_results,
            quality_hint=quality_hint,
            result_path=result_relative_path,
            finished_at=utc_now(),
        )
        if completed:
            module_logger.info(
                "image done job=%s image=%s seq=%s stages=%s hint=%s elapsed=%.1fs",
                image_record.job_id, image_record.image_id, image_record.seq,
                stage_results, quality_hint, elapsed_seconds,
            )
        else:
            module_logger.warning(
                "image complete lost race job=%s image=%s（终态已被并发写入，结果丢弃）",
                image_record.job_id, image_record.image_id,
            )

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

        # ---- 入口双 VL 并行 + 判定复用（2026-08-28 第二十六次修订）----
        # 两个链头问答都以原始图白底合成分析图为输入且互不依赖：棋盘格判定
        # （定顺序门）与水印预检（定是否修复）在随图建的 2-worker 短命池
        # 并发发起——旧路径串行问 2-3 次（顺序门一次 + FillStep 内部同问题
        # 一次 + 预检一次），单图省 0.8-20s 串行段 + 1-2 次外呼费用。
        # 判定复用路由：
        # - 正常顺序（非棋盘格）：预检结果直传 WatermarkStep（入口与本步同在
        #   原始图域，域精确）；棋盘格判定直传 FillStep（水印修复只重绘水印
        #   区不引入棋盘格，近似成立）。
        # - fill-first（棋盘格 true）：入口棋盘格判定直传 FillStep（同图域
        #   精确）；入口预检结果**丢弃**（换白生成重绘背景，原图域答案对
        #   生成图不成立），WatermarkStep 照旧自问。
        # 失败语义逐条保留：棋盘格失败按 False 走正常顺序、预检失败按 None
        # 走 skipped 零误伤（步骤内部各自兜底）。
        entry_checkerboard, entry_precheck = self._ask_entry_verdicts(image_bgr)
        wm_first = entry_checkerboard is not True  # None（未配/失败）→ 正常顺序
        ordered_steps = ["watermark", "fill"] if wm_first else ["fill", "watermark"]

        for step_name in ordered_steps:
            if step_name == "watermark":
                # 去水印——管线已在原始域，修复结果即原图域整幅直传下一步
                # （不按 crop 偏移裁窗映射：旧架构遗留会致往返缩放画质损失）。
                watermark_result = self._watermark_step.run(
                    image_bgr,
                    precheck_verdict=entry_precheck if wm_first else None,
                )
                image_bgr = watermark_result.image_bgr
                stage_results["watermark"] = watermark_result.stage_value
                if watermark_result.quality_hint == "heavy-watermark":
                    quality_hint = "heavy-watermark"
            else:
                # 填充（形状无关——判定/外呼/缓存全在整图域，2026-08-25 定案；
                # 原始域下同图不同形状同键，模型只调一次）
                fill_result = self._fill_step.run(
                    image_bgr, checkerboard_verdict=entry_checkerboard
                )
                image_bgr = fill_result.image_bgr
                stage_results["fill"] = fill_result.stage_value

        # 步 3：尺寸缩放（2026-08-28 第二十七次修订：缩放提前到裁剪之前）——
        # 送佐糖的是"去水印+填充后"的原始域图（裁剪前），超分缓存键（图内容
        # SHA-256+目标宽）不随形状/裁剪框变化：同图二次提交跨形状命中，零重复
        # 外呼（旧顺序裁剪先行，缓存键随形状变化——同图 4 次提交 3 次外呼的
        # 超分版）。描边仍在缩放后目标幅直画（第十九次修订口径不变）。
        size_cm = None
        if crop_meta and isinstance(crop_meta.get("size"), dict):
            try:
                size_cm = float(crop_meta["size"].get("cm"))
            except (TypeError, ValueError):
                size_cm = None
        from src.steps.resize import ResizeStep

        resize_result = ResizeStep(self._settings).run(image_bgr, size_cm)
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

        # 步 4：裁剪（CropStep，2026-08-26 独立 step；第二十七次修订移到
        # 缩放之后）——在放大图上运行时执行：框坐标按 frame→当前幅等比映射
        # （框语义=原图上的区域选择，随幅等比缩放，crop.py 原有映射逻辑天然
        # 兼容），形状掩膜高幅塑形（解析几何任意分辨率无损）。形状掩膜重画
        # 自本修订起全部由裁剪步承担（旧 resize 的 shape_value 重画参数退役
        # ——缩放时还不知道裁剪结果）。
        # crop_enabled=false（2026-08-28 第二十次修订）→ 不裁框不塑形原图
        # 直通，skipped 记档（下游形状描边随声明缺失自然退化）。
        from src.steps.crop import CropStep, CropStepResult

        if self._settings.crop_enabled:
            crop_result = CropStep().run(image_bgr, crop_meta_for_crop)
            image_bgr = crop_result.image_bgr
        else:
            module_logger.info("crop: 配置关闭（PT_CROP_ENABLED=false）→ 原图直通")
            crop_result = CropStepResult(image_bgr, "skipped")
        # 记档约定：crop 字段始终记声明 JSON（含 shape 供前端回显/排查），
        # 执行与否看下游收到的图；无声明才记 "skipped"。
        # （2026-08-26 14:49 起 CropStep 对无框合法声明也执行塑形——
        # "每图必有形状"兑现，形状外真 transparent）
        stage_results["crop"] = crop_meta_json or "skipped"
        if crop_result.stage_value != "skipped":
            stage_results["crop_applied"] = crop_result.stage_value

        # 步 5：描边（2026-08-28 第十九次修订——管线最末步，目标幅直画：
        # 线宽 px = mm×DPI/25.4 在交付幅精确落地，零拉伸零振铃；旧顺序
        # [描边@源幅→缩放] 在佐糖服务端定倍 ×5.7+尾程 ×1.53 非常规倍率链下
        # 灰线被拉出 LANCZOS 振铃锯齿 + 物理线宽漂 4 倍（2px→9.7px=0.82mm，
        # 配置 0.2mm），心形真图实锤。白底判定与 rembg 分割随之在高幅跑，
        # 分割质量更好，代价 rembg 大图耗时 +1-2s）。
        # crop 关闭（第二十次修订"不裁框不塑形原图直通"）时描边不施加形状——
        # 外环画法（第三十二次修订）会按形状雕透明，违背直通契约；回退 rectangle
        outline_shape = crop_shape if self._settings.crop_enabled else None
        outline_result = self._outline_step.run(image_bgr, outline_shape)
        image_bgr = outline_result.image_bgr
        stage_results["outline"] = outline_result.stage_value

        return encode_png(image_bgr), stage_results, quality_hint

    def _ask_entry_verdicts(self, image_bgr: np.ndarray) -> tuple[bool | None, bool | None]:
        """入口双 VL 并行问（2026-08-28 第二十六次修订）。

        同一张白底合成分析图上并发两问：棋盘格判定（顺序门 + FillStep 复用）
        与水印预检（WatermarkStep 复用）。返回 (checkerboard, precheck)：
        - 未配置 key → (None, None)（两步各自按 skipped 降级，零外呼）；
        - 单问失败 → 该问按 None/False 语义降级（棋盘格失败=False 走正常
          顺序由调用侧 is not True 处理；预检失败=None 按无水印零误伤）。
        http_sync 阻塞调用线程——两问必须两个 OS 线程各持一发（随图建的
        2-worker 短命池，finally 收口不入单例，与 retouch-steps 壳同纪律）。
        """
        from src.steps.fill.gate_vl import CheckerboardGate
        from src.steps.imaging import flatten_to_white
        from src.steps.watermark.precheck import WatermarkPrecheck

        analysis_bgr = flatten_to_white(ensure_bgra(image_bgr))  # 问域统一：FillStep 标定 4/4 的域
        gate = CheckerboardGate(self._settings)
        precheck = WatermarkPrecheck(self._settings)
        if not gate.is_configured():
            return None, None  # 两问共用 wm_precheck 配置（key 同源）

        def ask_checkerboard() -> bool | None:
            try:
                return gate.has_checkerboard_background(analysis_bgr)
            except Exception as gate_error:
                module_logger.warning("checkerboard gate error: %s", gate_error)
                return False  # 失败按非棋盘格走正常顺序（零误伤语义）

        def ask_precheck() -> bool | None:
            try:
                return precheck.has_watermark(analysis_bgr)
            except Exception as precheck_error:
                module_logger.warning("entry precheck error: %s", precheck_error)
                return None  # 失败按无水印（步骤侧 skipped 零误伤）

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="vl-gate") as pool:
            checkerboard_future = pool.submit(ask_checkerboard)
            precheck_future = pool.submit(ask_precheck)
            return checkerboard_future.result(), precheck_future.result()

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
