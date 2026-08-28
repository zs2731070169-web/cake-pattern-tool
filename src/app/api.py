"""批次接口层：建批 / 状态查询 / 结果下载 / 免责与配置 四接口 + 限流闸。

2026-08-26 重构：路由统一挂在 APIRouter（prefix=/api）上，由 main.py 的
应用工厂 include——本模块不再创建 FastAPI 实例、不挂中间件、不挂静态目录，
只声明接口与校验逻辑。

2026-08-28 第二十三次修订：四接口均为**同步 def**——接口体内的 SQLite 读写、
文件落盘、图像解码/PNG 编码由 FastAPI 自动 run_in_threadpool 丢入 Starlette
anyio 线程池执行，uvicorn 事件循环只留请求解析与响应编排（async def 形态下
建批 9 张 3600×3600 图的秒级解码编码会连续卡住全站轮询）。同步体内不能
await：UploadFile 读取用 upload_file.file.read()（底层同一 SpooledTemporaryFile）。

技术方案 4.1 接口清单与错误码：
- POST /api/jobs（multipart 1-9 图）→ JobCreateResponse；
- GET /api/jobs/{job_id} → JobStatusResponse（轮询 2-3s，批粒度）；
- GET /api/jobs/{job_id}/images/{image_id}/result → image/png；
- GET /api/meta → 免责声明 + 形状选项 + 大小限制；
- 错误码只取 429/422/404/409/410/500。
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.app.deps import get_job_store, get_pipeline, get_settings
from src.core.config import PatternToolSettings
from src.jobs.pipeline import RetouchPipeline
from src.jobs.store import JobStore, _format_utc, utc_now
from src.steps.imaging import ImageDecodeError, probe_image_size

module_logger = logging.getLogger("pattern_tool.api")

# 免责声明文案（需求解读 7 章版权合规红线；启用远程 API 档时附第三方传输告知）
DISCLAIMER_TEXT = (
    "本工具仅供处理您拥有版权或已获授权的图片；去水印功能不得用于侵犯他人版权的图片。"
    "上传即表示您确认对图片的合法使用权，并同意图片在 24 小时后自动删除。"
)
REMOTE_API_DISCLAIMER_TEXT = (
    "启用远程 AI 处理时，图片将传输至第三方 AI 服务处理（仅图片数据，不含其他信息）。"
)

# 前端裁剪形状选项（需求解读 5.1：常规形状 5 项 + 自由矩形）
CROP_SHAPE_OPTIONS = ["circle", "square", "rectangle", "heart", "star", "free"]

# 允许的图片 MIME 类型（超限 422；内容真实性强校验由解码探测完成）
ALLOWED_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}

router = APIRouter(prefix="/api")


# ---- DTO（4.2 节，响应用 pydantic）----


class JobCreateResponse(BaseModel):
    """建批响应。"""

    job_id: str = Field(description="批次 ID，后续轮询与下载的唯一凭据")
    image_ids: list[str] = Field(description="图片 ID 列表，与上传顺序一致")


class ImageStatusDTO(BaseModel):
    """单图状态（轮询响应的 images 元素）。"""

    image_id: str = Field(description="图片 ID")
    seq: int = Field(description="图序号（1 起）")
    status: str = Field(description="queued / processing / completed / failed")
    stage_results: dict[str, str] = Field(description="watermark/fill/outline → done/skipped/fallback")
    quality_hint: str = Field(description="none / heavy-watermark（low-res 已撤 2026-08-27）")
    result_url: str | None = Field(default=None, description="completed 时有值（相对路径）")
    error_msg: str | None = Field(default=None, description="failed 时有值（脱敏话术）")
    started_at: str | None = Field(
        default=None,
        description="开始处理时间（UTC ISO；置 processing 时写。2026-08-28 第二十四次修订：前端仅执行中计时的锚点）",
    )
    finished_at: str | None = Field(
        default=None,
        description="完成/失败时间（UTC ISO）——与 started_at 之差即单图真实执行耗时",
    )


class JobStatusResponse(BaseModel):
    """批次状态响应（前端轮询）。"""

    job_id: str = Field(description="批次 ID")
    status: str = Field(description="processing / completed")
    images: list[ImageStatusDTO] = Field(description="逐图状态，顺序=上传顺序")
    server_time: str = Field(
        description="响应生成时刻（UTC ISO）——前端钟差校正：本机钟与服务端钟有偏差时按此校准计时起点",
    )


class MetaResponse(BaseModel):
    """免责与配置响应。"""

    disclaimer: str = Field(description="页脚常驻免责声明")
    remote_api_disclaimer: str = Field(description="远程 AI 档第三方传输告知")
    crop_shapes: list[str] = Field(description="裁剪形状选项")
    max_images: int = Field(description="单批张数上限")
    max_image_mb: int = Field(description="单张字节上限（MB）")
    max_image_pixels: int = Field(description="解码后单边像素上限")
    min_image_pixels: int = Field(description="解码后单边像素下限")
    reject_duplicate_images: bool = Field(description="是否启用同批内容查重")


# ---- 工具函数 ----


def get_client_ip(request: Request) -> str:
    """客户端 IP（Nginx 反代后取 X-Forwarded-For 首段，无则直连地址）。"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def validation_error(detail_message: str) -> HTTPException:
    """422：张数/类型/大小/像素不合规（中文原因）。"""
    return HTTPException(status_code=422, detail=detail_message)


def ensure_job_exists(job_id: str, store: JobStore):
    """批次存在性：404（不存在）/ 410（已过期清理）；store 由调用路由传入。"""
    job_record = store.get_job(job_id)
    if job_record is None:
        # 无法区分"从未存在"与"已被 TTL 清理"，统一 404——
        # 前端对 404 与 410 的引导相同（重新提交），不额外维护墓碑记录
        raise HTTPException(status_code=404, detail="批次不存在或已过期")
    return job_record


# ---- 接口 1：建批 ----


@router.post("/jobs", response_model=JobCreateResponse)
def create_job(
    request: Request,
    images: list[UploadFile] = File(description="1-9 张图片（PNG/JPG/WebP）"),
    crop_meta: str | None = File(default=None, description="逐图裁剪声明 JSON（仅记档）"),
    originals: list[UploadFile] | None = File(default=None, description="逐图原始未裁剪文件（去水印缓存域，4.4；可空/缺省）"),
    store: JobStore = Depends(get_job_store),
    pipeline: RetouchPipeline = Depends(get_pipeline),
    effective_settings: PatternToolSettings = Depends(get_settings),
) -> JobCreateResponse:
    client_ip = get_client_ip(request)

    # 限流闸：同 IP 进行中批次 ≤ 3，超限 429
    active_job_count = store.count_active_jobs_by_ip(client_ip)
    if active_job_count >= effective_settings.max_concurrent_jobs_per_ip:
        raise HTTPException(status_code=429, detail="处理的图片较多，请稍后再试")

    # 张数校验（1-9）
    if len(images) < 1:
        raise validation_error("请至少上传 1 张图片")
    if len(images) > effective_settings.max_images_per_job:
        raise validation_error(f"单次最多 {effective_settings.max_images_per_job} 张图片")

    # 逐图校验（任何一张不合规整批 422，不产生部分批次）
    # UploadFile 流式一次性读取：先全量读入内存再校验/落盘
    saved_relative_paths: list[str] = []
    job_record = None
    try:
        uploaded_image_chunks: list[bytes] = []
        seen_content_hashes: set[str] = set()  # 同批内容查重（SHA-256）
        for upload_index, upload_file in enumerate(images, start=1):
            # MIME 类型白名单
            if upload_file.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
                raise validation_error(
                    f"第 {upload_index} 张图片格式不支持（仅 PNG/JPG/WebP）"
                )
            image_bytes = upload_file.file.read()
            uploaded_image_chunks.append(image_bytes)
            # 字节上限（15MB 硬上限）
            if len(image_bytes) > effective_settings.max_image_bytes:
                raise validation_error(
                    f"第 {upload_index} 张图片超过 {effective_settings.max_image_bytes // (1024 * 1024)}MB 上限"
                )
            # 字节下限（拦空图/占位图）
            if len(image_bytes) < effective_settings.min_image_bytes:
                raise validation_error(
                    f"第 {upload_index} 张图片过小（低于"
                    f" {effective_settings.min_image_bytes // 1024}KB，请上传完整图片）"
                )
            # 同批内容查重：同一张图重复上传整批 422（指纹=SHA-256，文件名不同也算重）
            if effective_settings.reject_duplicate_images:
                content_hash = hashlib.sha256(image_bytes).hexdigest()
                if content_hash in seen_content_hashes:
                    raise validation_error(f"第 {upload_index} 张图片与之前的图片重复，请勿重复上传")
                seen_content_hashes.add(content_hash)
            # 像素上限（解码前 header 探测，3600×3600）
            try:
                image_width, image_height = probe_image_size(image_bytes)
            except ImageDecodeError as probe_error:
                raise validation_error(f"第 {upload_index} 张图片：{probe_error}") from probe_error
            if image_width > effective_settings.max_image_pixels or image_height > effective_settings.max_image_pixels:
                raise validation_error(
                    f"第 {upload_index} 张图片像素超限"
                    f"（{image_width}×{image_height}，上限"
                    f" {effective_settings.max_image_pixels}×{effective_settings.max_image_pixels}）"
                )
            # 像素下限（拦缩略图/图标）
            if image_width < effective_settings.min_image_pixels or image_height < effective_settings.min_image_pixels:
                raise validation_error(
                    f"第 {upload_index} 张图片分辨率过低"
                    f"（{image_width}×{image_height}，最小"
                    f" {effective_settings.min_image_pixels}×{effective_settings.min_image_pixels}）"
                )

        # 校验全通过后建批与落盘（库记录与文件同批生成，统一转 PNG）
        from src.steps.imaging import decode_to_ndarray, encode_png  # noqa: PLC0415 建批热路径外延迟导入

        job_record, image_records = store.create_job(
            client_ip=client_ip,
            image_count=len(images),
            input_paths=[f"jobs/pending/in_{seq}.png" for seq in range(1, len(images) + 1)],
        )
        job_directory = effective_settings.resolve_data_dir() / "jobs" / job_record.job_id
        job_directory.mkdir(parents=True, exist_ok=True)
        for seq, image_bytes in enumerate(uploaded_image_chunks, start=1):
            decoded_bgr = decode_to_ndarray(image_bytes, effective_settings.max_image_pixels)
            (job_directory / f"in_{seq}.png").write_bytes(encode_png(decoded_bgr))
            saved_relative_paths.append(f"jobs/{job_record.job_id}/in_{seq}.png")
        # 回填真实路径（建批时用占位路径，落盘成功后校正）
        for image_record, relative_path in zip(image_records, saved_relative_paths):
            store.update_image(image_record.image_id, input_path=relative_path)

        # 原始未裁剪文件落盘（去水印缓存域，4.4）：与 images 一一对应（可缺省）。
        # 前端裁剪过的图带上原图 → 去水印在原始域执行，同图不同裁剪共享缓存免重复计费。
        if originals:
            for seq, original_file in enumerate(originals[:len(images)], start=1):
                original_bytes = original_file.file.read()
                if not original_bytes:
                    continue
                try:
                    decoded_original = decode_to_ndarray(original_bytes, effective_settings.max_image_pixels)
                    (job_directory / f"orig_{seq}.png").write_bytes(encode_png(decoded_original))
                except Exception as original_error:  # 原始域异常不阻塞（回退裁剪版域）
                    module_logger.warning("original file %d invalid: %s", seq, original_error)

        # crop_meta 记档并入 stage_results 头部；shape 驱动描边/填充形状。
        # 每图必有形状（2026-08-25 需求变更；2026-08-27 默认值修订）：缺声明
        # 的图补默认 rectangle（矩形整图——不裁切，原默认 circle 撤销）；
        # 畸形 crop_meta 按全空处理（每图都补默认），同样不阻塞建批。
        import json

        try:
            parsed_crop_meta = json.loads(crop_meta) if crop_meta else {}
            if not isinstance(parsed_crop_meta, dict):
                parsed_crop_meta = {}
        except (ValueError, TypeError):
            parsed_crop_meta = {}
        for image_record in image_records:
            seq_crop_meta = parsed_crop_meta.get(str(image_record.seq)) or {
                "shape": "rectangle", "default": True,
            }
            merged_stages = {
                "crop": json.dumps(seq_crop_meta, ensure_ascii=False),
                **image_record.stage_results,
            }
            store.update_image(image_record.image_id, stage_results=merged_stages)

    except HTTPException:
        raise
    except Exception as create_error:
        module_logger.exception("job create failed")
        # 落盘失败整批 500 并回滚记录（图 A 异常分支：删文件 + 删两表行）
        if job_record is not None:
            import shutil

            cleanup_directory = effective_settings.resolve_data_dir() / "jobs" / job_record.job_id
            if cleanup_directory.exists():
                shutil.rmtree(cleanup_directory, ignore_errors=True)
            store.delete_job_rows(job_record.job_id)
        raise HTTPException(status_code=500, detail="服务暂时不可用，请稍后重试") from create_error

    # 受理成功 → 触发后台管线（立即返回，前端轮询）
    pipeline.submit_job(job_record.job_id)
    module_logger.info(
        "job created job=%s ip=%s images=%d", job_record.job_id, client_ip, len(images)
    )
    return JobCreateResponse(
        job_id=job_record.job_id,
        image_ids=[record.image_id for record in image_records],
    )


# ---- 接口 2：批次状态 ----


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(
    job_id: str,
    store: JobStore = Depends(get_job_store),
) -> JobStatusResponse:
    job_record = ensure_job_exists(job_id, store)
    image_records = store.get_job_images(job_id)

    image_dtos: list[ImageStatusDTO] = []
    for image_record in image_records:
        # stage_results 里 crop_applied 之外的记档键不下发（内部记档，4.2 契约）；
        # resize/crop_applied 是分辨率格与裁剪执行态的展示依据（2026-08-26 进白名单）
        public_stages = {
            key: value for key, value in image_record.stage_results.items()
            if key in ("watermark", "fill", "outline", "crop", "resize", "crop_applied")
        }
        image_dtos.append(
            ImageStatusDTO(
                image_id=image_record.image_id,
                seq=image_record.seq,
                status=image_record.status,
                stage_results=public_stages,
                quality_hint=image_record.quality_hint,
                result_url=(
                    f"/api/jobs/{job_id}/images/{image_record.image_id}/result"
                    if image_record.status == "completed"
                    else None
                ),
                error_msg=image_record.error_msg,
                started_at=_format_utc(image_record.started_at) if image_record.started_at else None,
                finished_at=_format_utc(image_record.finished_at) if image_record.finished_at else None,
            )
        )
    return JobStatusResponse(
        job_id=job_record.job_id,
        status=job_record.status,
        images=image_dtos,
        server_time=_format_utc(utc_now()),
    )


# ---- 接口 3：结果下载 ----


@router.get("/jobs/{job_id}/images/{image_id}/result")
def download_result(
    job_id: str,
    image_id: str,
    store: JobStore = Depends(get_job_store),
    effective_settings: PatternToolSettings = Depends(get_settings),
) -> FileResponse:
    ensure_job_exists(job_id, store)
    image_record = store.get_image(image_id)
    if image_record is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    if image_record.status != "completed":
        raise HTTPException(status_code=409, detail="图片尚未处理完成")
    if not image_record.result_path:
        # 终态与路径的防御性一致性检查（正常流程 completed 必有路径）
        raise HTTPException(status_code=404, detail="结果文件不存在或已清理")
    result_absolute_path = effective_settings.resolve_data_dir() / image_record.result_path
    if not result_absolute_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在或已清理")
    return FileResponse(result_absolute_path, media_type="image/png", filename=f"pattern_{image_record.seq}.png")


# ---- 接口 4：免责与配置 ----


@router.get("/meta", response_model=MetaResponse)
def get_meta(
    effective_settings: PatternToolSettings = Depends(get_settings),
) -> MetaResponse:
    return MetaResponse(
        disclaimer=DISCLAIMER_TEXT,
        # 远程处理声明开关跟随现役生成式配置
        remote_api_disclaimer=REMOTE_API_DISCLAIMER_TEXT if effective_settings.fill_gen_enabled else "",
        crop_shapes=CROP_SHAPE_OPTIONS,
        max_images=effective_settings.max_images_per_job,
        max_image_mb=effective_settings.max_image_bytes // (1024 * 1024),
        max_image_pixels=effective_settings.max_image_pixels,
        min_image_pixels=effective_settings.min_image_pixels,
        reject_duplicate_images=effective_settings.reject_duplicate_images,
    )
