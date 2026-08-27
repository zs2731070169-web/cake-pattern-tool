# Resize Specification

## Purpose

定义尺寸缩放步骤：按打印尺寸档（4/6/8/10/12 寸 + 自定义 cm）缩放到 @300DPI 目标短边；缩小本地处理，放大优先佐糖 scale-pro 超分。

## Requirements

### Requirement: Print Size Target Calculation

系统 SHALL 根据 crop_meta.size.cm 计算目标短边像素：cm / 2.54 × 300；未选尺寸或非法值（5–100cm 外）skipped。

#### Scenario: Size declared

- GIVEN 用户选择 8 寸（约 20.3cm）
- WHEN ResizeStep 执行
- THEN 目标短边 = 20.3 / 2.54 × 300 ≈ 2400px
- AND 等比缩放长短边

#### Scenario: No size selected

- GIVEN crop_meta.size 为空或 null
- WHEN ResizeStep 执行
- THEN stage_results.resize 记为 skipped
- AND 原幅直传

#### Scenario: Already at target size

- GIVEN 当前短边等于目标短边
- WHEN ResizeStep 执行
- THEN 记为 skipped，不做缩放

### Requirement: Downscale with INTER_AREA

系统 SHALL 在短边大于目标时使用 INTER_AREA 本地等比缩小，零外呼。

#### Scenario: Downscale to target

- GIVEN 源图短边大于目标短边
- WHEN ResizeStep 执行
- THEN 使用 INTER_AREA 缩小
- AND stage_results.resize 记为 done

### Requirement: Upscale with PicWish Scale-Pro

系统 SHALL 在短边小于目标时，优先调用佐糖 scale-pro 高级变清晰（≤110s，key 复用 PT_WM_API_KEY）；超分结果仍不足目标再 INTER_CUBIC 补尾程。

#### Scenario: Successful upscale

- GIVEN 源图短边小于目标且 PT_WM_API 已配置
- WHEN 佐糖 scale-pro 返回高清图
- THEN alpha 最近邻回填保形状掩膜
- AND stage_results.resize 记为 done 或 done(api)

#### Scenario: Upscale failure fallback

- GIVEN 佐糖超分失败或未配置
- WHEN ResizeStep 执行放大
- THEN 降级 Lanczos/INTER_CUBIC 插值放大
- AND stage_results.resize 记为 done(interpolated)，不提示 low-res（已撤）

### Requirement: Resize After Outline

系统 SHALL 将 ResizeStep 置于管线最末（描边之后），使线宽像素随缩放等比、打印物理毫米恒定。

#### Scenario: Outline before resize

- GIVEN 管线执行至 ResizeStep
- WHEN 缩放发生
- THEN 输入为已描边的 CropStep 输出
- AND 描边物理宽度保持约 1.5mm 不变
