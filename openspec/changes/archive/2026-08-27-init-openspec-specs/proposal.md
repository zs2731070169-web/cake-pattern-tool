## Why

项目已完成实现但缺少 OpenSpec 规格驱动工作流与按核心功能划分的行为规格。需要将 `docs/plan/修图工具最小技术方案.md` 中的架构与功能口径沉淀为可变更、可归档的 OpenSpec spec，支撑后续 `/opsx:propose` 增量演进。

## What Changes

- 初始化 OpenSpec（config.yaml、Cursor 命令与 skills）
- 新增 `AGENTS.md` 统一 AI 工程约定（合并原 CLAUDE.md）
- 按核心功能创建 10 个子域 spec：architecture、pipeline、watermark、fill、crop、outline、resize、job-api、frontend、storage
- 更新 README 规格索引

**Non-goals**：不修改任何业务代码；不改动已实现行为。

## Capabilities

### New Capabilities

- `architecture`: 整体架构（单体部署、分层模块、请求链路、并发模型、非功能约束）
- `pipeline`: 管线编排（顺序门、原始域、记档语义、批内隔离）
- `watermark`: 去水印（qwen-vl 预检 + 佐糖修复 + 缓存）
- `fill`: 背景填充（棋盘格判定 + qwen-image 换白）
- `crop`: 声明式裁剪（CropStep 运行时塑形）
- `outline`: 描边（白底判定 + 形状边界灰线）
- `resize`: 尺寸缩放（打印档 + 佐糖超分）
- `job-api`: 批次 HTTP 接口与限流
- `frontend`: 前端交互与分端交付
- `storage`: SQLite、文件存储、缓存、TTL

### Modified Capabilities

（无——本次为规格文档初始化，行为与现有代码一致）

## Impact

- 文档：`openspec/`、`AGENTS.md`、`README.md`
- 代码：无改动
- 对齐依据：`docs/plan/修图工具最小技术方案.md` 第 2–6 章
