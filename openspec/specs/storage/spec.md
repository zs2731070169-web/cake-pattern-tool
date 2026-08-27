# Storage and TTL Specification

## Purpose

定义 SQLite 数据模型、文件存储布局、API 结果缓存与 TTL 过期清理。

## Requirements

### Requirement: SQLite Two-Table Model

系统 SHALL 使用 SQLite（WAL 模式）存储 process_jobs 与 job_images 两表；建表 DDL 外置 `db/schema.sql`，启动时幂等执行。

#### Scenario: Schema initialization

- GIVEN 应用首次启动或 schema 更新
- WHEN JobStore 初始化
- THEN 读取 db/schema.sql 并 executescript（IF NOT EXISTS）
- AND 缺文件或读失败 fail-fast

#### Scenario: Process job fields

- GIVEN 建批成功
- WHEN 写入 PROCESS_JOB
- THEN 记录 job_id、client_ip、image_count、status、created_at、expires_at（+24h）

#### Scenario: Job image fields

- GIVEN 批次含 N 张图
- WHEN 写入 JOB_IMAGE
- THEN 记录 image_id、job_id、seq、input_path、status、stage_results JSON、quality_hint
- AND 图片字节只存文件路径，不进数据库

### Requirement: File System Layout

系统 SHALL 将原图与结果存于 `data/jobs/{job_id}/in_{seq}.png` 与 `out_{seq}.png`；统一转 PNG 含透明通道。

#### Scenario: Input file persistence

- GIVEN POST /api/jobs 校验通过
- WHEN 原图落盘
- THEN 路径为 data/jobs/{job_id}/in_{seq}.png
- AND completed 后 result_path 回填 out_{seq}.png

### Requirement: API Result Cache

系统 SHALL 在去水印与填充步骤将外呼结果缓存至 `data/cache/{watermark|fill_gen}/{hash前2位}/{hash}.png`，键为分析图 SHA-256。

#### Scenario: New cache directory registration

- GIVEN 新增外部 API 缓存目录
- WHEN 实现缓存
- THEN 必须登记到 src/jobs/ttl.py::_CACHE_DIR_NAMES
- AND 随 TTL 24h 清理

#### Scenario: Cache stores color only

- GIVEN 缓存写入
- WHEN 读取缓存
- THEN 缓存只存颜色图
- AND 透明通道由管线按原图 alpha 重新合并

### Requirement: TTL Cleanup

系统 SHALL 每小时（启动先跑一次）清理 expires_at 过期批次的数据库行、data/jobs/ 目录及 cache 过期文件。

#### Scenario: Expired job cleanup

- GIVEN process_jobs.expires_at < now
- WHEN TTLCleaner 执行
- THEN 删除两表相关行与 data/jobs/{job_id}/ 目录
- AND 访问该 job 返回 404

#### Scenario: Cache file cleanup

- GIVEN cache 文件超过 24h 口径
- WHEN TTLCleaner 执行
- THEN 删除 data/cache/watermark 与 fill_gen 下过期文件

### Requirement: Privacy and Data Retention

系统 SHALL 匿名处理、24h TTL 自动删除客户图片；error_msg 脱敏；API 密钥不进前端/日志/错误信息。

#### Scenario: Error message sanitization

- GIVEN 图片处理 failed
- WHEN error_msg 写入数据库并返回前端
- THEN 不含文件路径、堆栈或 API key
