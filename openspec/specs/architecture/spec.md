# System Architecture Specification

## Purpose

定义 cake-pattern-tool 整体系统架构：单体部署模型、分层模块、端到端请求链路、并发与外部边界。各算法步与接口细节见子域 spec（pipeline / watermark / fill / crop / outline / resize / job-api / frontend / storage）。

## Requirements

### Requirement: Monolithic Single-Process Deployment

系统 SHALL 以 FastAPI 单体服务部署（默认 :8200），同进程挂载静态前端、批次 API 与修图管线；不引入消息队列、对象存储或多服务拆分。

#### Scenario: Single uvicorn worker

- GIVEN 生产或开发环境启动服务
- WHEN uvicorn 加载应用
- THEN worker 数固定为 1（SQLite 单写者硬前提）
- AND 进程内单例（配置、连接池、线程池、HTTP 客户端）在同一进程生命周期内复用

#### Scenario: No external middleware dependencies

- GIVEN 系统架构评审
- WHEN 评估基础设施依赖
- THEN 不引入 Redis、MinIO/boto3、SQLAlchemy、任务队列
- AND 图片与元数据均在本机文件系统 + SQLite 完成

### Requirement: Layered Module Structure

系统 SHALL 按以下分层组织代码，职责边界清晰、单向依赖：

```
web/                    # 前端单页（index.html + app.js + cropper.js vendor）
src/app/                # FastAPI 装配：main / api / deps / lifespan
src/core/               # 基础设施：config / database / executor / http
src/jobs/               # 批次编排：pipeline / store / ttl
src/steps/              # 五步算法：watermark / fill / crop / outline / resize + imaging 公共
db/schema.sql           # SQLite 建表 DDL（幂等，JobStore 启动读取）
```

#### Scenario: App layer routes requests

- GIVEN HTTP 请求到达
- WHEN 路由匹配 src/app/api.py
- THEN 校验与限流在 API 层完成
- AND 建批后委托 RetouchPipeline（src/jobs/pipeline.py）异步处理

#### Scenario: Steps are independent modules

- GIVEN 管线执行某一步
- WHEN 调用 src/steps/{step}/ 下实现
- THEN 各步仅通过管线传入/传出图像与 stage_results
- AND 步骤间不直接互相 import 业务逻辑（共享编解码走 src/steps/imaging/）

### Requirement: End-to-End Request Lifecycle

系统 SHALL 支持以下主链路：上传建批 → 异步修图 → 轮询状态 → 下载成品。

#### Scenario: Upload to job creation

- GIVEN 客户在 web/ 选择 1–9 张原图及 crop_meta 声明
- WHEN POST /api/jobs
- THEN API 校验 → 写 SQLite → 原图落盘 data/jobs/{job_id}/in_{seq}.png
- AND 返回 job_id + image_ids，BackgroundTasks 启动管线

#### Scenario: Poll to download

- GIVEN 批次已创建
- WHEN 前端每 ~10s GET /api/jobs/{job_id}
- THEN 返回各图 status、stage_results、quality_hint、result_url
- AND completed 后 GET result_url 返回 out_{seq}.png

### Requirement: Asynchronous Processing Model

系统 SHALL 将修图管线提交至 ThreadPoolExecutor，与 FastAPI 事件循环解耦；批内图片串行、批次间可并行。

#### Scenario: Pipeline runs off event loop

- GIVEN POST /api/jobs 建批成功
- WHEN BackgroundTasks 触发 RetouchPipeline
- THEN 管线主体在 core/executor 线程池线程中同步执行
- AND API 事件循环不被 CPU 密集型步骤阻塞

#### Scenario: Inner-batch serial processing

- GIVEN 一个批次含多张图
- WHEN 管线处理该批次
- THEN 按 seq 逐图串行（非批内并行）
- AND 避免与 recovery/TTL 竞态

### Requirement: Unified HTTP Client for External APIs

系统 SHALL 通过 src/core/http.py 的全进程唯一后台事件循环线程执行全部外部 HTTP；步骤代码调用 http_sync(coroutine)，禁止在调用方线程 run_until_complete。

#### Scenario: Cross-thread safe external call

- GIVEN 多个线程池线程并发发起外部 API 请求
- WHEN 步骤调用 http_sync
- THEN 协程提交到唯一后台 loop 执行
- AND 不出现 "bound to a different event loop" 错误

#### Scenario: External payload privacy

- GIVEN 启用百炼或佐糖 API
- WHEN 外呼发起
- THEN 仅传输图片字节（multipart/base64）
- AND 不透传 job_id、client_ip 或任何业务元数据

### Requirement: Configuration Single Entry

系统 SHALL 通过 src/core/config.py::PatternToolSettings（pydantic-settings，PT_ 前缀）读取全部配置；优先级：环境变量 > .env > 默认值。

#### Scenario: Config access from any module

- GIVEN 任意后端模块需要配置项
- WHEN 读取配置
- THEN 经 PatternToolSettings 注入或 deps 获取
- AND 不得绕过直接读 os.environ

#### Scenario: Secret handling

- GIVEN API Key 类配置
- WHEN 写入日志、error_msg 或 API 响应
- THEN 密钥绝不出现
- AND .env 不入库、不进前端

### Requirement: SQLite Per-Thread Connection Model

系统 SHALL 使用 SQLite WAL 模式；连接按每线程每库路径一条（threading.local），写事务由 JobStore._write_lock 串行化。

#### Scenario: Concurrent batch writes

- GIVEN 多个批次并行处理
- WHEN 各线程写 JobStore
- THEN 每线程独立 SQLite 连接
- AND 写操作经 _write_lock 串行，避免 SQLITE_BUSY

### Requirement: Explicit Architecture Non-Goals

系统 SHALL NOT 引入服务端自动裁剪、用户账号体系、订单/ERP 集成、GPU 本地超分、OpenCV 水印检测、本地像素填充判据或前端构建链；恢复前须用户明确决策。

#### Scenario: Rejected pattern detection

- GIVEN 开发者考虑引入 OpenCV/RapidOCR 水印检测
- WHEN 架构评审
- THEN 拒绝引入；qwen-vl 为唯一检测器（见 watermark spec）

#### Scenario: Rejected account system

- GIVEN 需求提出注册登录
- WHEN 架构评审
- THEN 拒绝；匿名 + IP 限流为既定模型

### Requirement: Non-Functional Constraints

系统 SHALL 满足以下非功能约束：

#### Scenario: Input validation limits

- GIVEN 客户上传图片
- WHEN API 校验
- THEN 1–9 张；PNG/JPG/WebP；≤15MB/张；200–3600px；同批 SHA-256 查重

#### Scenario: Rate and concurrency limits

- GIVEN 同 IP 并发建批
- WHEN 进行中批次 > 3
- THEN 返回 429
- AND 全局 processing 信号量 ≤ 2×CPU 核

#### Scenario: Timeout and TTL

- GIVEN 单图进入 processing
- WHEN 处理时长
- THEN 单图上限 180s（只计 processing）；排队超 10 分钟置 failed
- AND 批次与缓存 TTL 24h，TTLCleaner 每小时清理

#### Scenario: Privacy and data retention

- GIVEN 客户图片上传
- WHEN 存储与传输
- THEN 匿名处理、24h 自动删除、job_id 为 uuid4 不可枚举
- AND /api/meta 返回第三方 API 免责声明

### Requirement: Spec Domain Organization

系统 SHALL 将行为规格划分为 architecture、pipeline、watermark、fill、crop、outline、resize、job-api、frontend、storage 十个子域；跨域变更 MUST 同步更新所有受影响子域 spec。

#### Scenario: Cross-domain change

- GIVEN 变更同时影响 HTTP 接口与管线编排
- WHEN 创建 OpenSpec 提案
- THEN 同时更新 job-api 与 pipeline spec 增量
- AND 若改模块边界则同步更新 architecture spec

#### Scenario: Single-domain change

- GIVEN 变更仅影响去水印算法
- WHEN 创建 OpenSpec 提案
- THEN 仅更新 watermark spec 增量
- AND architecture spec 保持不变
