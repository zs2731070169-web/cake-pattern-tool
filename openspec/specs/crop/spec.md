# Crop Specification

## Purpose

定义裁剪步骤：前端声明式 crop_meta，后端 CropStep 在 fill 之后运行时执行框裁与形状掩膜塑形。

## Requirements

### Requirement: Declarative Crop Meta from Frontend

系统 SHALL 接收每张图的 crop_meta（shape + frame + box + data:{x,y,width,height}），前端不做像素裁剪。

#### Scenario: Explicit crop declaration

- GIVEN 客户在前端选择形状并调整裁剪框
- WHEN 提交批次
- THEN crop_meta 随 multipart 一并提交
- AND 后端保存声明供 CropStep 使用

#### Scenario: Default shape when not cropped

- GIVEN 客户未显式裁剪某张图
- WHEN 提交批次
- THEN 自动补默认 rectangle 整图声明
- AND 不裁切、形状外保持不透明，描边沿整图边框

### Requirement: Runtime Crop and Shape Masking

系统 SHALL 在 CropStep 按 crop_meta.data 裁出外接框，再按 shape 应用掩膜（形状外 alpha=0）。

#### Scenario: Circle heart or star shape

- GIVEN crop_meta.shape 为 circle、heart 或 star
- WHEN CropStep 执行
- THEN 裁出外接框后应用对应形状掩膜
- AND 形状外像素 alpha 置 0

#### Scenario: Rectangle or square shape

- GIVEN crop_meta.shape 为 rectangle 或 square
- WHEN CropStep 执行
- THEN 裁框即为最终形状，无额外掩膜

### Requirement: Shape Geometry Consistency

系统 SHALL 使 CropStep 形状掩膜与 `outline.py::crop_shape_region_mask` 及 `web/app.js::buildShapePath` 几何公式同源。

#### Scenario: Heart and star precise ratios

- GIVEN 形状为 heart 或 star
- WHEN 掩膜生成
- THEN 使用已定案比例（心 32:28.9、星内半径 0.44r 等）
- AND 与前端预览所见即所得
