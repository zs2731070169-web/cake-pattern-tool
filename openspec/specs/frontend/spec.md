# Frontend Specification

## Purpose

定义 C 端 Web 单页交互：上传、声明式裁剪、批次提交、轮询结果、分端交付。本工具定位为桌面 Web 端。

## Requirements

### Requirement: Dual-Column Desktop Layout

系统 SHALL 采用 CSS Grid 双栏布局：左栏上传与已选列表（约 40%），右栏处理结果（约 60%）同屏共存。

#### Scenario: Upload and result visible together

- GIVEN 客户打开页面
- WHEN 上传或处理进行中
- THEN 左栏显示上传区与已选缩略图九宫格
- AND 右栏常驻（未提交时显示引导文案）

### Requirement: Multi-Image Upload

系统 SHALL 支持选择 1–9 张图、Ctrl+V 粘贴图片直传、同批预览与逐图操作。

#### Scenario: Paste from clipboard

- GIVEN 客户在桌面浏览器
- WHEN 按 Ctrl+V 粘贴聊天窗口收到的图片
- THEN 图片加入待处理列表，无需先存盘

#### Scenario: Batch uniform size setting

- GIVEN 同批多图需相同打印尺寸
- WHEN 客户选择「统一尺寸」下拉
- THEN 应用到全部待处理图并重渲列表
- AND 逐图下拉同步显示

### Requirement: Declarative Crop Interaction

系统 SHALL 提供逐图可选裁剪交互（circle/square/rectangle/heart/star/free），使用 cropper.js；前端只记录 crop_meta，不裁像素。

#### Scenario: Optional per-image crop

- GIVEN 客户点击某张图进入裁剪
- WHEN 选择形状并调整框
- THEN 前端记录 shape+frame+box+data
- AND 预览含形状遮罩渲染（buildShapePath 与后端同源）

#### Scenario: Close crop modal

- GIVEN 裁剪弹层打开
- WHEN 按 ESC 或点击遮罩
- THEN 关闭弹层（桌面交互规范）

### Requirement: Job Submit and Poll

系统 SHALL 提交后调用 POST /api/jobs，并以约 10s 间隔轮询 GET /api/jobs/{job_id} 直至批次完成。

#### Scenario: Submit and poll

- GIVEN 客户点击「开始处理」
- WHEN 建批成功
- THEN 前端开始轮询，展示逐图 status 与 stage_results
- AND 提交后自动定位结果面板

### Requirement: Desktop Result Delivery

系统 SHALL 在桌面端提供单张下载、批量 ZIP 下载、单张「复制图片」到剪贴板（ClipboardItem 特性检测）。

#### Scenario: Single image download

- GIVEN 某图 completed
- WHEN 客户点击下载
- THEN 浏览器默认下载目录落盘 PNG

#### Scenario: Batch ZIP download

- GIVEN 批次多张 completed
- WHEN 客户点击批量下载
- THEN 打包 ZIP 供解压后拖入聊天窗口

#### Scenario: Copy single image

- GIVEN 桌面端且 ClipboardItem 可用
- WHEN 客户点击「复制图片」
- THEN 图片写入剪贴板，2s 后按钮反馈复原

### Requirement: Result Stage Display

系统 SHALL 展示各步 stage_results；failed 红显「执行失败」，skipped 显示跳过，done(interpolated) 红显插值兜底。

#### Scenario: Failed step indication

- GIVEN stage_results 某步为 failed
- WHEN 结果卡渲染
- THEN 该步红显「执行失败」
- AND 仍提供可下载结果（零误伤交付）
