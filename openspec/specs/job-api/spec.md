# Job API Specification

## Purpose

定义批次 HTTP 接口、输入校验、IP 限流、状态机与错误码契约。

## Requirements

### Requirement: Job Creation Endpoint

系统 SHALL 提供 `POST /api/jobs`，接收 multipart（images[] 1–9 张 + crop_meta JSON），返回 job_id 与 image_ids[]。

#### Scenario: Successful job creation

- GIVEN 客户提交 1–9 张合规图片及 crop_meta
- WHEN POST /api/jobs 校验通过
- THEN 创建 PROCESS_JOB 与 N 条 JOB_IMAGE（status=queued）
- AND 原图落盘为 in_{seq}.png，返回 JobCreateResponse

#### Scenario: Image count exceeded

- GIVEN 客户提交超过 9 张图
- WHEN POST /api/jobs
- THEN 返回 422，中文原因说明张数不合规

### Requirement: Input Validation

系统 SHALL 校验：PNG/JPG/WebP；≤15MB/张；解码后单边 200–3600px；同批 SHA-256 查重。

#### Scenario: Invalid file type

- GIVEN 上传非 PNG/JPG/WebP 文件
- WHEN 校验执行
- THEN 返回 422 中文原因

#### Scenario: Duplicate image in batch

- GIVEN 同批两张图 SHA-256 相同
- WHEN 校验执行
- THEN 返回 422，reject_duplicate_images 生效

### Requirement: Rate Limiting

系统 SHALL 限制同 IP 进行中批次 ≤3（或配置上限），超出返回 429。

#### Scenario: IP concurrent limit exceeded

- GIVEN 同 IP 已有 3 个 processing 批次
- WHEN 提交第 4 个批次
- THEN 返回 429

### Requirement: Job Status Polling

系统 SHALL 提供 `GET /api/jobs/{job_id}` 返回批次与各图状态、stage_results、quality_hint、result_url。

#### Scenario: Poll processing job

- GIVEN 批次处理中
- WHEN GET /api/jobs/{job_id}
- THEN 返回各图 status（queued/processing/completed/failed）
- AND stage_results 白名单含 watermark/fill/crop/outline/resize/crop_applied

#### Scenario: Image state machine monotonic

- GIVEN 图片状态变迁
- WHEN 轮询采样
- THEN 仅允许 queued → processing → completed/failed，终态不回退

### Requirement: Result Download

系统 SHALL 提供 `GET /api/jobs/{job_id}/images/{image_id}/result` 返回结果 PNG。

#### Scenario: Completed image download

- GIVEN 图片 status=completed
- WHEN GET result
- THEN 返回 200 image/png 文件流

#### Scenario: Incomplete image download

- GIVEN 图片尚未 completed
- WHEN GET result
- THEN 返回 409

#### Scenario: Expired or missing job

- GIVEN 批次已 TTL 清理或 job_id 不存在
- WHEN GET result 或 GET /api/jobs/{job_id}
- THEN 返回 404

### Requirement: Meta Endpoint

系统 SHALL 提供 `GET /api/meta` 返回免责声明、形状选项、大小限制等前端预校验配置。

#### Scenario: Meta response fields

- GIVEN 前端加载页面
- WHEN GET /api/meta
- THEN 返回 disclaimer、remote_api_disclaimer、crop_shapes[]、max_images、max_image_mb 等字段
