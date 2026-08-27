# AGENTS.md

本文件为 AI 编码助手提供**工程级**指导。规划与规格相关规则见 `openspec/config.yaml`。

## 项目概述

**cake-pattern-tool**（糯米纸图案自动化修图工具）

面向 C 端客户的自助修图 Web 工具：上传蛋糕糯米纸图案 → 五步管线（去水印 / 填充 / 裁剪 / 描边 / 尺寸缩放）→ 可打印透明 PNG 成品。

- 远程仓库：`https://github.com/zs2731070169-web/cake-pattern-tool.git`
- 规格驱动开发：使用 [OpenSpec](https://openspec.dev) 管理需求与变更
- 技术设计真源：`docs/plan/修图工具最小技术方案.md`（改代码先改文档）
- 开发规范：`docs/spec/SKILL.md`（最小闭环、自解释命名、防御性编程）

## 沟通约定

- 始终使用**中文**回复
- 收到任务时先确认理解，再动手
- 变更范围大时，先通过 OpenSpec 提案再实现
- 行动前先给分析报告，确认后再改代码

## OpenSpec 工作流

| 阶段 | 命令 | 说明 |
|------|------|------|
| 探索 | `/opsx:explore` | 理清需求与方案（可选） |
| 提案 | `/opsx:propose "变更名"` | 创建 proposal / specs / design / tasks |
| 实现 | `/opsx:apply` | 按 tasks 逐项实现 |
| 同步 | `/opsx:sync` | 同步规格与代码 |
| 归档 | `/opsx:archive` | 合并 spec 并归档变更 |

目录结构：

```
openspec/
├── config.yaml      # 项目上下文与 artifact 规则（规划时注入）
├── specs/           # 系统行为规格（真相来源）
└── changes/         # 进行中的变更提案
    └── archive/     # 已归档变更
```

**职责划分：**

- `AGENTS.md`（本文件）：编码习惯、工程约定、通用 AI 行为
- `openspec/config.yaml`：规划 artifact 时的项目背景与写作规则
- `docs/plan/修图工具最小技术方案.md`：模块技术设计（ER、接口、时序、修订记录）

## 常用命令

```bash
./start.sh                # 启动服务（.venv 缺失时自动初始化；会先清空 data/ 并杀端口占用进程）
./start.sh init           # 仅初始化/刷新依赖（requirements.txt 变更后）
./start.sh check          # 环境自检，不安装不启动

.venv/bin/python -m pytest                                                  # 全量测试（无需外部 API key）
.venv/bin/python -m pytest tests/test_outline_pixels.py                     # 单文件
.venv/bin/python -m pytest "tests/test_outline_pixels.py::test_outline_threshold_fallback"  # 单测试
```

- 需要 Python 3.11 或 3.12（3.13+ 的 opencv/rembg 轮子未验证）；服务地址 http://localhost:8200（`PT_PORT`）
- **uvicorn worker=1 是硬前提**（SQLite 单写者，技术方案 6 节），进程内单例（配置/连接池/线程池）都依赖它，不要改
- `start.sh` 每次启动**清空 `data/`**（调试期口径）；生产部署注释掉 `clear_runtime_data` 调用
- 外部 API（百炼 qwen-vl/qwen-image、佐糖 PicWish）全部有配置开关，未配 key 时对应步零误伤跳过——测试不需要任何真实 key

## 工作流铁律：文档先行

`docs/plan/修图工具最小技术方案.md` 是模块技术设计的**唯一真源**（需求口径、ER 图、接口契约、时序图、修订记录）。任何改动的顺序固定为：**文档 → 代码 → 测试 → 真图验证**。改表结构先改 `db/schema.sql` 再动 `src/jobs/store.py`。`docs/spec/SKILL.md` 是开发规范（最小闭环防过度设计、自解释命名、防御性编程、行动前先给分析报告），动代码前先对齐。

## 架构大图

C 端自助修图工具：上传 1-9 张图 → 五步管线 → 透明 PNG 成品。产品概述、快速开始、配置表见 `README.md`（与代码同步维护）。

**请求主链路**：`web/`（原生 JS 单页，同进程 StaticFiles 挂载）→ `POST /api/jobs`（`src/app/api.py` 校验+落盘+建批）→ `RetouchPipeline.submit_job`（`src/jobs/pipeline.py`）→ core 线程池逐图跑步骤 → `GET /api/jobs/{id}` 轮询 → 逐图下载结果。状态机：图 `queued → processing → completed/failed`（终态单向不回退），批 `processing → completed`。

**五步管线**（`pipeline.py::_run_steps`，每步独立 skipped，单图失败不影响批内其他图）：

1. **去水印**：qwen-vl 语义预检判有无（唯一检测器）→ 有才调佐糖 PicWish 高级版修复；失败=原图+`failed` 记档（该修没修成）
2. **填充**：qwen-vl 判棋盘格背景 → qwen-image-2.0 生成式换纯白底；非棋盘格 skipped
3. **裁剪**：按前端**声明**（shape+框）运行时执行——框裁外接区+形状掩膜塑形（形状外 alpha=0）
4. **描边**：形状边带白底判定 → rembg 分割兜底灰度阈值 → 沿**形状边界**内缩画 1.5mm@300DPI 灰线（打印裁切参考线）
5. **尺寸缩放**：按打印档（cm）缩放到 @300DPI 目标短边；缩小 INTER_AREA，放大优先佐糖超分失败 Lanczos 兜底

**棋盘格顺序门**：原始图问 VL 是否棋盘格背景，true 则顺序换为 填充→去水印（去水印生成会重绘棋盘格致填充判定失效）。

### 原始域原则（改管线必读）

管线全程在**原始图**上处理：前端只声明裁剪（shape+框）不裁像素，真正的裁剪由 CropStep 在填充之后运行时执行。目的：判定/外呼/缓存的键稳定——同图不同形状共享缓存与模型外呼，零重复计费。破坏这一点（比如让前端裁完再传）会直接回归"同图 4 次提交 3 次外呼"的翻车现场。

### 缓存设计

去水印与生成式换白的结果缓存都在 `data/cache/{watermark,fill_gen}/`，键 = 送 API 的分析图（白底合成副本）内容 SHA-256。新增外部 API 缓存目录必须登记到 `src/jobs/ttl.py::_CACHE_DIR_NAMES`（随批次 24h 清理）。缓存只存颜色图，透明通道由管线按原图 alpha 重新合并。

### 线程与 HTTP 模型（易翻车点）

- 管线主体跑在 ThreadPoolExecutor 线程里（API 事件循环之外），步骤代码是同步的
- 所有外部 HTTP 必须走 `src/core/http.py::http_sync(coroutine)`——协程提交到**全进程唯一后台事件循环线程**执行。"每线程各建 loop"的旧模型会在批次并行下抛 "bound to a different event loop"（共享 AsyncClient 连接绑定首个使用线程的循环）。绝不在调用方线程 `run_until_complete`（uvicorn 主线程自带运行中的循环会炸）
- SQLite 连接按 **每线程每库路径** 一条（`threading.local` 字典，`src/core/database.py`），写事务由 `JobStore._write_lock` 串行化。测试逐 `tmp_path` 新库天然隔离，不要做全局 reset（会死锁，2026-08-26 实测）
- 批内图片**串行**处理（2026-08-26 定案撤回批内并行——与 recovery/TTL 竞态），批次间并行
- 重启恢复：启动时悬挂 `processing/queued` 图自动置 failed（话术"服务重启中断，请重新提交"）

### 记档与失败语义

`stage_results` 值域：`done` / `done(api)` / `done(interpolated)` / `skipped` / `failed`。核心区分（2026-08-27 定案）：**"该修但没修成"（欠费/超时）记 `failed`** 前端红显"执行失败"；**"不需要修"（无水印/非棋盘格）记 `skipped`**。外部 API 失败一律**原图零误伤交付**，不让整图 failed——降级不断交付。`error_msg` 脱敏（不含路径/堆栈），key 绝不进前端/日志/错误信息。

### 已退役方案（不要重新引入）

代码注释里有大量"退役存档"，都是真图实测翻车后的用户定案，恢复前必须先问用户：

- OpenCV 水印检测（RapidOCR+频域差分）——实测 2/4 错且两方向都错；qwen-vl 是唯一检测器
- 填充的本地像素判据（白度占比/色簇/熵）——米白误放、卡通主体拉高熵；改 qwen-vl 棋盘格判定
- isnet/u2net 分割填白与拓扑填白（Path B）——分割毛边污染主体、淡彩主体误漂白
- 生成式输出的本地验证门与色阶归一（v18.6）——模型对复杂主体稳定输出 247-249 纸白，252 纯白口径拒致整类图失效；**生成成功即原样交付，不做任何本地像素后处理**
- qwen-image 做去水印修复——多图实测效果不理想，回退佐糖
- `low-res` quality_hint、IOPaint/LaMa 本地档、旧 `PT_WM_*` 像素检测键

### 判定哲学（测量事实，不猜身份）

真假 alpha 不做身份推断（永远有反例）——测**背景白度**这类可验证事实：判定、任务路由、验收用同一把尺子，验证在交付域做。判定失败一律按"无需处理"降级（零误伤），不抛错。

### 前后端形状几何同源

`src/steps/outline/outline.py::crop_shape_region_mask`（circle/heart/star 掩膜，心 32:28.9、星内半径 0.44r 等精确公式）必须与 `web/app.js` 的 `buildShapePath` 保持同源——所见即所得。改形状公式必须两端同改。

### 配置

`src/core/config.py::PatternToolSettings`（pydantic-settings，键前缀 `PT_`）是配置唯一入口——任何模块不得绕过它自行读 `os.environ`。优先级：真实环境变量 > `.env` > 字段默认值；`.env.example` 是逐项注释的模板（键与字段一一对应，改配置项三处同步）。

### 测试口径

`tests/conftest.py` 的 `test_settings` fixture 指向 `tmp_path` 临时目录（`_env_file=None` 隔离真实 .env），`api_client` 用 `create_app(app_settings=...)` 工厂注入。外部 API 全部 monkeypatch 客户端或走未配置降级路径断言。改动跨文件时按 SKILL.md 要求显式给出受影响面与最小回归方案。

## 编码原则

1. **文档先行** — 改动顺序：文档 → 代码 → 测试 → 真图验证
2. **最小改动** — 只改与任务相关的代码，不做无关重构
3. **遵循现有风格** — 命名、目录、抽象与周边代码保持一致
4. **原始域原则** — 管线全程在原始图上处理；前端只声明裁剪，后端 CropStep 运行时执行
5. **零误伤交付** — 外部 API 失败原图交付，记 `failed`/`skipped`，不阻断整批
6. **配置唯一入口** — 通过 `PatternToolSettings`（前缀 `PT_`），不得绕过读 `os.environ`
7. **HTTP 走 http_sync** — 外部 HTTP 必须走 `src/core/http.py::http_sync`，不在调用方线程 `run_until_complete`
8. **形状几何同源** — `outline.py::crop_shape_region_mask` 与 `web/app.js::buildShapePath` 必须保持一致
9. **注释克制** — 只注释非显而易见的业务或技术细节
10. **测试按需** — 用户要求或跨文件改动时给出最小回归方案

## Git 约定

- 仅在用户明确要求时提交
- 不 force push 到 main/master
- 不提交密钥、凭证等敏感文件（`.env`、`data/`）
- 提交信息简洁，说明「为什么」而非罗列「改了什么」

## 开始新功能

1. 若需求不清晰 → `/opsx:explore`
2. 需求明确 → `/opsx:propose "功能描述"`
3. 同步更新 `docs/plan/修图工具最小技术方案.md` 修订记录
4. 审阅 `openspec/changes/<name>/` 下的 artifact
5. 确认后 → `/opsx:apply` 开始实现
