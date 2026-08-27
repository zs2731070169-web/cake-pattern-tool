# Watermark Removal Specification

## Purpose

定义去水印步骤：qwen-vl 语义预检为唯一检测器，佐糖 PicWish 高级版修复，结果缓存与零误伤降级。

## Requirements

### Requirement: Qwen-VL Precheck as Sole Detector

系统 SHALL 使用 qwen-vl-plus 视觉问答判定图片是否含水印，作为去水印的唯一检测器；OpenCV/像素检测方案不得重新引入。

#### Scenario: Watermark detected

- GIVEN PT_WM_PRECHECK 已配置且 qwen-vl 判定有水印
- WHEN WatermarkStep 执行
- THEN 进入佐糖修复路径
- AND 预检记档为需修复

#### Scenario: No watermark detected

- GIVEN qwen-vl 判定无水印
- WHEN WatermarkStep 执行
- THEN stage_results.watermark 记为 skipped
- AND 不发起佐糖 API 外呼

#### Scenario: Precheck unavailable or failed

- GIVEN 预检未配置或 qwen-vl 调用失败
- WHEN WatermarkStep 执行
- THEN 记为 skipped，原图零误伤传递
- AND 不盲修

### Requirement: PicWish Advanced Repair

系统 SHALL 在预检判有且缓存未命中时，调用佐糖 PicWish 高级版全屏去水印（提交-轮询-下载，同步 ≤110s）。

#### Scenario: Successful repair

- GIVEN 缓存未命中且 PT_WM_API 已配置
- WHEN 佐糖返回修复图
- THEN stage_results.watermark 记为 done(api)
- AND 结果缩回原幅后写入 data/cache/watermark/ 缓存

#### Scenario: Cache hit

- GIVEN 相同原图分析副本的 SHA-256 缓存已存在
- WHEN WatermarkStep 执行
- THEN 直接使用缓存，stage_results.watermark 记为 done(api)
- AND 零外呼零计费

### Requirement: Zero-Harm on Repair Failure

系统 SHALL 在佐糖修复失败、超时或未配置时，以原图零误伤交付，记 failed 或 skipped，并可选设置 quality_hint=heavy-watermark。

#### Scenario: PicWish repair failure

- GIVEN 预检判有水印但佐糖修复失败
- WHEN WatermarkStep 完成
- THEN stage_results.watermark 记为 failed
- AND 原图传递至后续步骤，不阻断交付
