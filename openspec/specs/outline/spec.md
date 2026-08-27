# Outline Specification

## Purpose

定义描边步骤：形状边带白底判定，沿声明形状边界内缩绘制 1.5mm@300DPI 灰线打印裁切参考线。

## Requirements

### Requirement: White Background Gate

系统 SHALL 在形状内边带（边界内缩 0–40px ∩ 不透明区）判定背景白度；非白底 skipped，不描边。

#### Scenario: White background detected

- GIVEN rembg 掩膜剔除图案后，边带亮度中位 ≥235 且 ≥90% 像素 ≥230
- WHEN OutlineStep 执行
- THEN 进入描边绘制
- AND stage_results.outline 记为 done

#### Scenario: Non-white background

- GIVEN 边带背景不满足白底阈值
- WHEN OutlineStep 执行
- THEN stage_results.outline 记为 skipped
- AND 图案边界已可见，无需补线

#### Scenario: Rembg unavailable fallback

- GIVEN rembg 不可用
- WHEN 白底判定执行
- THEN 退化为亮度上四分位簇阈值法
- AND 不因 rembg 缺失而抛错阻断

### Requirement: Shape Boundary Gray Line

系统 SHALL 沿声明形状边界内缩一个线宽，在 alpha=255 完全不透明区绘制灰线（默认 1.5mm@300DPI，RGB 约 190,190,190）。

#### Scenario: Draw outline on white background

- GIVEN 白底判定通过
- WHEN 描边绘制
- THEN 线带内缩一个线宽，只落 alpha=255 区
- AND 图案本体零侵占，线带连续

#### Scenario: Rectangle corner handling

- GIVEN 形状为 rectangle 或 square
- WHEN 描边绘制
- THEN 边框带端点同幅内缩，防止四角凸块

### Requirement: Every Image Has Shape Context

系统 SHALL 保证描边始终有形状边界可依；无显式裁剪时默认 rectangle 整图边框。

#### Scenario: Default rectangle whole image

- GIVEN 客户未显式裁剪
- WHEN OutlineStep 执行
- THEN 沿整图矩形边框描线（若白底满足）
