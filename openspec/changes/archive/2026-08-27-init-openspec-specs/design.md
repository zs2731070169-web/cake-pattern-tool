## Intent

将既有修图工具的行为口径从单一技术方案文档拆分为 OpenSpec 多域 spec，architecture 为入口，各算法步独立 spec，便于后续增量变更与归档。

## Approach

1. **规格域划分**：按 docs/plan 第 2.1 分层与五步管线拆分为 10 个子域
2. **architecture 入口**：覆盖单体部署、模块分层、请求生命周期、线程/HTTP 模型、非功能约束、明确 Non-goals
3. **子域 spec**：每个域用 Requirement + GIVEN/WHEN/THEN Scenario 描述行为，与现有代码口径一致
4. **文档同步**：config.yaml 写入规格域索引；README 增加 spec 表

## Non-goals

- 不修改 `src/`、`web/`、`db/schema.sql` 任何实现
- 不引入新依赖或部署变更
- 不改变 API 契约或算法行为

## 规格与 docs/plan 对齐

| 子域 | docs/plan 章节 |
|------|----------------|
| architecture | §2 总体架构、§6 异常与非功能、§7.2 模块映射 |
| pipeline | §2.1 修图管线、§5.2 图 B |
| watermark/fill/crop/outline/resize | §2.1 各步说明、§4.4 外部 API |
| job-api | §4 接口与契约 |
| frontend | §5.1 图 A、§7.1 前端选型 |
| storage | §2.2 数据归属、§3 ER 图 |
