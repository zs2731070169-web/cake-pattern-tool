# System Architecture Specification

## ADDED Requirements

### Requirement: Monolithic Single-Process Deployment

系统 SHALL 以 FastAPI 单体服务部署（默认 :8200），同进程挂载静态前端、批次 API 与修图管线；不引入消息队列、对象存储或多服务拆分。

#### Scenario: Single uvicorn worker

- GIVEN 生产或开发环境启动服务
- WHEN uvicorn 加载应用
- THEN worker 数固定为 1（SQLite 单写者硬前提）

### Requirement: Spec Domain Organization

系统 SHALL 将行为规格划分为 architecture、pipeline、watermark、fill、crop、outline、resize、job-api、frontend、storage 十个子域。

#### Scenario: Cross-domain change

- GIVEN 变更同时影响 HTTP 接口与管线编排
- WHEN 创建 OpenSpec 提案
- THEN 同时更新 job-api 与 pipeline spec 增量
