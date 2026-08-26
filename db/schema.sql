-- pattern-tool 建表脚本（技术方案第 3 章 ER 图与 3.3 字段字典的唯一真源）
-- JobStore 初始化时读取本文件 executescript（幂等：IF NOT EXISTS，已存在则跳过）。
-- 改表口径：先改本文件 → 再动 store.py 读写代码 → 补测试（文档→代码→测试）。

-- 修图批次表：一次上传 1-9 图为一批（process_jobs）
CREATE TABLE IF NOT EXISTS process_jobs (
    job_id      TEXT PRIMARY KEY,          -- 批次 ID（uuid4，建批时生成）
    client_ip   TEXT NOT NULL,             -- 客户端 IP（限流与审计用）
    image_count INTEGER NOT NULL,          -- 本批图片张数（1-9）
    status      TEXT NOT NULL,             -- 批次状态：processing / completed
    created_at  TEXT NOT NULL,             -- 建批时间（UTC ISO）
    expires_at  TEXT NOT NULL              -- 过期时间（created_at + TTL，清理依据）
);
CREATE INDEX IF NOT EXISTS idx_process_jobs_expires_at
    ON process_jobs (expires_at);

-- 批次图片表：批内逐图记录（job_images）
CREATE TABLE IF NOT EXISTS job_images (
    image_id      TEXT PRIMARY KEY,        -- 图片 ID（uuid4，建批时生成）
    job_id        TEXT NOT NULL REFERENCES process_jobs(job_id),  -- 所属批次
    seq           INTEGER NOT NULL,        -- 批内序号（1 起，前端展示顺序）
    input_path    TEXT NOT NULL,           -- 客户原图相对路径（in_{seq}.png）
    result_path   TEXT,                    -- 结果路径（completed 回填 out_{seq}.png）
    status        TEXT NOT NULL,           -- queued / processing / completed / failed
    stage_results TEXT NOT NULL DEFAULT '{}',  -- 各步结果 JSON（watermark/fill/crop/outline/resize/crop_applied）
    quality_hint  TEXT NOT NULL DEFAULT 'none', -- none / heavy-watermark（low-res 已撤 2026-08-27）
    error_msg     TEXT,                    -- 失败原因（仅 failed，脱敏话术）
    finished_at   TEXT                     -- 完成时间
);
CREATE INDEX IF NOT EXISTS idx_job_images_job_id
    ON job_images (job_id);
