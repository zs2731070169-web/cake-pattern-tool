# Background Fill Specification

## Purpose

定义背景填充步骤：qwen-vl 棋盘格背景判定，qwen-image-2.0 生成式换白底，生成成功即原样交付，不做本地像素后处理。

## Requirements

### Requirement: Checkerboard Background Detection

系统 SHALL 使用 qwen-vl 问答判定「主体以外的背景是否是棋盘格（查看器透明指示格）」；非棋盘格（白底/米白/照片）直接 skipped。

#### Scenario: Checkerboard background

- GIVEN qwen-vl 判定背景为棋盘格
- WHEN FillStep 执行
- THEN 进入生成式换白路径
- AND 本地像素判据（白度/色簇/熵）不参与决策

#### Scenario: Non-checkerboard background

- GIVEN 背景为白底、米白或照片
- WHEN FillStep 执行
- THEN stage_results.fill 记为 skipped
- AND 零 qwen-image 外呼

#### Scenario: VL detection failure

- GIVEN qwen-vl 棋盘格判定失败
- WHEN FillStep 执行
- THEN 按 false 处理，skipped 原图零误伤

### Requirement: Qwen-Image Generative White Background

系统 SHALL 在棋盘格判定为 true 且 PT_FILL_GEN 已配置时，调用 qwen-image-2.0 同步换白底（≤40s，base64 上传，透明区铺白）。

#### Scenario: Successful generation

- GIVEN 配置门开启且主体未贴满整图
- WHEN qwen-image 生成成功
- THEN stage_results.fill 记为 done 或 done(api)
- AND 模型输出原样整幅交付，不做本地验证门或色阶归一

#### Scenario: Cache hit

- GIVEN 分析图 SHA-256 在 data/cache/fill_gen/ 已命中
- WHEN FillStep 执行
- THEN 直接使用缓存结果
- AND 零外呼零计费

### Requirement: Zero-Harm on Generation Failure

系统 SHALL 在 qwen-image 失败或超时时，以原图零误伤传递，记 failed 或 skipped，不阻断管线。

#### Scenario: API failure

- GIVEN qwen-image 调用失败或超时
- WHEN FillStep 完成
- THEN 原图传递至 CropStep
- AND stage_results.fill 记为 failed 或 skipped（已尝试）
