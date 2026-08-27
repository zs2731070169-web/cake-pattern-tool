# Pipeline Orchestration Specification

## Purpose

定义修图管线的编排行为：五步顺序、棋盘格顺序门、原始域原则、批内隔离与记档语义。整体架构见 architecture spec；各算法步细节见对应 step spec。

## Requirements

### Requirement: Five-Step Sequential Execution

系统 SHALL 对每张图片按 watermark → fill → crop → outline → resize 顺序串行执行，各步独立 skipped，单图异常不阻断批内其他图。

#### Scenario: Standard execution order

- GIVEN 非棋盘格背景的原图进入管线
- WHEN RetouchPipeline 执行 `_run_steps`
- THEN 依次调用水印、填充、裁剪、描边、尺寸缩放五步
- AND 每步输出作为下一步输入

#### Scenario: Batch inner serial processing

- GIVEN 一个批次包含多张图片
- WHEN 管线处理该批次
- THEN 批内图片按 seq 顺序串行处理（非并行）
- AND 批次之间可并行

### Requirement: Checkerboard Reorder Gate

系统 SHALL 在管线开始前用 qwen-vl 判定原始图是否为棋盘格背景；若为 true，顺序换为 fill → watermark → crop → outline → resize。

#### Scenario: Checkerboard image reorder

- GIVEN 原始图被 qwen-vl 判定为主体以外背景为棋盘格
- WHEN 管线开始执行
- THEN 先执行填充再执行去水印
- AND stage_results 中 fill 与 watermark 均有记档

#### Scenario: Non-checkerboard keeps default order

- GIVEN 原始图非棋盘格背景（白底/照片等）
- WHEN 管线开始执行
- THEN 保持 watermark → fill 默认顺序

### Requirement: Original-Domain Processing

系统 SHALL 全程在原始图上进行判定与外呼；前端只声明 crop_meta（shape+frame+box+data），像素裁剪由 CropStep 在 fill 之后运行时执行。

#### Scenario: Same image different shapes share external calls

- GIVEN 同一张原图两次提交，仅 crop_meta.shape 不同
- WHEN 两次管线执行
- THEN 去水印与填充的外呼缓存键相同（原图内容 SHA-256）
- AND 不因形状差异重复外呼

### Requirement: Stage Results Semantics

系统 SHALL 将每步结果记入 stage_results，值域为 done / done(api) / done(interpolated) / skipped / failed。

#### Scenario: External API should run but failed

- GIVEN 该步判定需要执行（如有水印）
- WHEN 外部 API 欠费或超时
- THEN 记为 failed，前端红显「执行失败」
- AND 以原图零误伤继续后续步骤

#### Scenario: Step not needed

- GIVEN 该步判定不适用（如无水印、非棋盘格、非白底）
- WHEN 步骤执行完毕
- THEN 记为 skipped
- AND 不发起对应外部 API 外呼

### Requirement: Per-Image Timeout and Isolation

系统 SHALL 对单图 processing 设 180s 上限；单图 failed 不影响批内其他图；排队超过 10 分钟置 failed。

#### Scenario: Single image failure isolation

- GIVEN 批次中某张图处理抛出不可恢复异常
- WHEN 该图被 catch
- THEN 该图 status=failed，error_msg 脱敏
- AND 批内其余图片继续处理

#### Scenario: Service restart recovery

- GIVEN 服务重启时存在 status 为 processing 或 queued 的图片
- WHEN 应用 lifespan 启动完成
- THEN 这些图片自动置 failed，error_msg 为「服务重启中断，请重新提交」
