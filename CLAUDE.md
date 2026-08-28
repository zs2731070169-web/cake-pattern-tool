# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
./start.sh                # 启动服务（.venv 缺失时自动初始化；会先清空 data/ 并杀端口占用进程）
./start.sh init           # 仅初始化/刷新依赖（requirements.txt 变更后）
./start.sh check          # 环境自检，不安装不启动

.venv/bin/python -m pytest                                                  # 全量测试（无需外部 API key）
.venv/bin/python -m pytest tests/test_outline_pixels.py                     # 单文件
.venv/bin/python -m pytest "tests/test_outline_pixels.py::test_outline_threshold_fallback"  # 单测试
```

- 需要 Python 3.11 或 3.12（3.13+ 的 opencv/rembg 轮子未验证）；服务地址 http://localhost:8200（`PT_PORT`）。
- **uvicorn worker=1 是硬前提**（SQLite 单写者，技术方案 6 节），进程内单例（配置/连接池/线程池）都依赖它，不要改。
- `start.sh` 每次启动清理 `data/` 下批次/库/缓存（**保留 `data/logs/`**——崩溃后重启排障要日志；调试期口径）；生产部署注释掉 `clear_runtime_data` 调用。
- 外部 API（百炼 qwen-vl/qwen-image、石榴智能去水印、佐糖超分 scale-pro）全部有配置开关，未配 key 时对应步零误伤跳过——测试不需要任何真实 key。佐糖超分第十八次修订复活（key 充值，scale-pro 主力）；去水印仍石榴唯一。

## 工作流铁律：文档先行

`docs/plan/修图工具最小技术方案.md` 是模块技术设计的**唯一真源**（需求口径、ER 图、接口契约、时序图、修订记录）。任何改动的顺序固定为：**文档 → 代码 → 测试 → 真图验证**。改表结构先改 `db/schema.sql` 再动 `src/jobs/store.py`。`docs/spec/SKILL.md` 是开发规范（最小闭环防过度设计、自解释命名、防御性编程、行动前先给分析报告），动代码前先对齐。

## 架构大图

C 端自助修图工具：上传 1-9 张图 → 五步管线 → 透明 PNG 成品。产品概述、快速开始、配置表见 `README.md`（与代码同步维护）。

**请求主链路**：`web/`（原生 JS 单页，同进程 StaticFiles 挂载）→ `POST /api/jobs`（`src/app/api.py` 校验+落盘+建批）→ `RetouchPipeline.submit_job`（`src/jobs/pipeline.py`）→ core 线程池逐图跑步骤 → `GET /api/jobs/{id}` 轮询 → 逐图下载结果。状态机：图 `queued → processing → completed/failed`（终态单向不回退），批 `processing → completed`。

**五步管线**（`pipeline.py::_run_steps`，每步独立 skipped，单图失败不影响批内其他图）：

1. **去水印**：qwen-vl 语义预检判有无（唯一检测器）→ 有才调石榴智能高级版修复（async_submit→async_fetch 异步轮询，唯一供应商，佐糖已下线）；失败=原图+`failed` 记档（该修没修成）。
2. **填充**：qwen-vl 判棋盘格背景 → qwen-image-2.0 生成式换纯白底；非棋盘格 skipped。
3. **尺寸缩放**：按打印档（cm，自定义上限 33cm=iX6880 A3+）缩放到 @300DPI 目标短边；缩小 INTER_AREA；放大走佐糖 scale-pro 高级变清晰（2026-08-28 第十八次修订，sync=0 显式异步提交-轮询-下载，服务端定倍，出幅对齐目标缩回/补尾程）；失败记 failed 整图不交付（第十七次修订语义——废图交付不是保交付）。**缩放在裁剪之前（2026-08-28 第二十七次修订）**——送佐糖的是去水印+填充后的原始域图，超分缓存键（内容 SHA-256+目标宽）不随形状/框变化，同图二次提交跨形状命中零重复外呼。
4. **裁剪**：按前端**声明**（shape+框）在放大图上运行时执行——框坐标按 frame→当前幅等比映射，形状掩膜高幅塑形（形状外 alpha=0，解析几何任意分辨率无损）。
5. **描边**：形状边带白底判定 → rembg 分割兜底灰度阈值 → 沿**形状边界**内缩画 1.5mm@300DPI 灰线（打印裁切参考线；仍最末步目标幅直画，第十九次修订口径不变）。

**棋盘格顺序门**：原始图问 VL 是否棋盘格背景，true 则顺序换为 填充→去水印（去水印生成会重绘棋盘格致填充判定失效）。

### 原始域原则（改管线必读）

管线全程在**原始图**上处理：前端只声明裁剪（shape+框）不裁像素，真正的裁剪由 CropStep 在填充之后运行时执行。目的：判定/外呼/缓存的键稳定——同图不同形状共享缓存与模型外呼，零重复计费。破坏这一点（比如让前端裁完再传）会直接回归"同图 4 次提交 3 次外呼"的翻车现场。

### 缓存设计

去水印与生成式换白的结果缓存都在 `data/cache/{watermark,fill_gen}/`，键 = 送 API 的分析图（白底合成副本）内容 SHA-256。新增外部 API 缓存目录必须登记到 `src/jobs/ttl.py::_CACHE_DIR_NAMES`（随批次 24h 清理）。缓存只存颜色图，透明通道由管线按原图 alpha 重新合并。

### 线程与 HTTP 模型（易翻车点）

- 管线主体跑在 ThreadPoolExecutor 线程里（API 事件循环之外），步骤代码是同步的。
- **API 四接口全部是同步 `def`（2026-08-28 第二十三次修订）**——不要改回 `async def`：async 函数体会直接跑在 uvicorn 事件循环上，体内的 SQLite/文件/图像编解码同步调用会卡住全站（建批 9 张 3600² 图实测连续卡 1–4s）。同步 def 由 FastAPI 自动 `run_in_threadpool` 丢入 anyio 线程池；UploadFile 读取用 `upload_file.file.read()`（同步体内不能 await）。
- 所有外部 HTTP 必须走 `src/core/http.py::http_sync(coroutine)`——协程提交到**全进程唯一后台事件循环线程**执行。"每线程各建 loop"的旧模型会在批次并行下抛 "bound to a different event loop"（共享 AsyncClient 连接绑定首个使用线程的循环）。绝不在调用方线程 `run_until_complete`（uvicorn 主线程自带运行中的循环会炸）。
- SQLite 连接按 **每线程每库路径** 一条（`threading.local` 字典，`src/core/database.py`），写事务由 `JobStore._write_lock` 串行化。测试逐 `tmp_path` 新库天然隔离，不要做全局 reset（会死锁，2026-08-26 实测）。
- 批内图片**并行**处理（2026-08-28 第二十六次修订，撤销 2026-08-26 串行定案——当年翻车根因是无条件完成回写竞态，非并行本身）：每图独立投递 core 池；完成回写走 `try_complete_image`（`WHERE status='processing'` 条件更新），终态单向在 DB 层闭环；链头双 VL（棋盘格+水印预检）入口并发问 + 判定复用（fill-first 下预检不复用——生成图域答案不成立）；五步本体仍严格串行（原始域原则 + 描边必须在缩放后）。
- 重启恢复：启动时悬挂 `processing/queued` 图自动置 failed（话术"服务重启中断，请重新提交"）。

### 记档与失败语义

`stage_results` 值域：`done` / `done(api)` / `skipped` / `failed`（`done(interpolated)` 已于 2026-08-28 第十七次修订退役）。核心区分：**"该修但没修成"（欠费/超时）记 `failed`** 前端红显"执行失败"；**"不需要修"（无水印/非棋盘格）记 `skipped`**。去水印失败原图零误伤交付；**放大失败整图 failed 不交付**（插值废图不是交付）。`error_msg` 脱敏（不含路径/堆栈），key 绝不进前端/日志/错误信息。

### 已退役方案（不要重新引入）

代码注释里有大量"退役存档"，都是真图实测翻车后的用户定案，恢复前必须先问用户：

- OpenCV 水印检测（RapidOCR+频域差分）——实测 2/4 错且两方向都错；qwen-vl 是唯一检测器。
- 填充的本地像素判据（白度占比/色簇/熵）——米白误放、卡通主体拉高熵；改 qwen-vl 棋盘格判定。
- isnet/u2net 分割填白与拓扑填白（Path B）——分割毛边污染主体、淡彩主体误漂白。
- 生成式输出的本地验证门与色阶归一（v18.6）——模型对复杂主体稳定输出 247-249 纸白，252 纯白口径拒致整类图失效；**生成成功即原样交付，不做任何本地像素后处理**。
- qwen-image 做去水印修复——多图实测效果不理想，回退。
- 佐糖 PicWish 去水印——第十次修订下线（成本 4~6 倍于石榴），`watermark_picwish.py` 已删恢复需从 git 历史。佐糖超分曾第十一次修订下线、**第十八次修订复活**（key 充值 + 石榴历版目检不符），`picwish_scale.py` 现役主力。
- 佐糖万物抠图做填充换白底（2026-08-28 实测否决）——分割式对浅色主体误分割：
  e9455 真图主体仅保留 32.1%（蛋糕浅色部分灰度 216-245 被当背景抠掉），用户判定不符合要求；
  与 rembg/isnet 退役同根（浅色主体 vs 背景界限模糊必翻车），生成式 qwen-image-2.0 维持现役。
- **石榴全部超分形态（2026-08-28 第十八次修订全线退役，`shiliu_scale.py` 已删，回退从 git 历史）**——基础版（锐化感强）、大图变高清 width 直出（超倍率锐度崩）、纯大图链（边缘钝化 10.6→4.5 用户目检"变糊"）、高级×4 打底+大图续跳两段链（用户终判"历版无一符合要求"）；石榴在系统的存留职责只剩去水印。本地 Lanczos 插值兜底——9.5 倍拉伸锐度 ~3 是废图，第十七次修订删除（失败了就 failed）。
- `low-res` quality_hint、IOPaint/LaMa 本地档、旧 `PT_WM_*` 像素检测键。

### 判定哲学（测量事实，不猜身份）

真假 alpha 不做身份推断（永远有反例）——测**背景白度**这类可验证事实：判定、任务路由、验收用同一把尺子，验证在交付域做。判定失败一律按"无需处理"降级（零误伤），不抛错。

### 前后端形状几何同源

`src/steps/outline/outline.py::crop_shape_region_mask`（circle/heart/star 掩膜，心 32:28.9、星内半径 0.44r 等精确公式）必须与 `web/app.js` 的 `buildShapePath` 保持同源——所见即所得。改形状公式必须两端同改。

### 配置

`src/core/config.py::PatternToolSettings`（pydantic-settings，键前缀 `PT_`）是配置唯一入口——任何模块不得绕过它自行读 `os.environ`。优先级：真实环境变量 > `.env` > 字段默认值；`.env.example` 是逐项注释的模板（键与字段一一对应，改配置项三处同步）。日志配置在 `src/core/logger.py::configure_logging`（lifespan 启动段自动创建单例，幂等；入口不在 main.py）：控制台 `PT_LOG_LEVEL`（默认 INFO）/ 文件 `PT_LOG_FILE_LEVEL`（默认 DEBUG）双通道，文件落 `data/logs/app.log`（10MB×5 轮转）。

### 测试口径

`tests/conftest.py` 的 `test_settings` fixture 指向 `tmp_path` 临时目录（`_env_file=None` 隔离真实 .env），`api_client` 用 `create_app(app_settings=...)` 工厂注入。外部 API 全部 monkeypatch 客户端或走未配置降级路径断言。改动跨文件时按 SKILL.md 要求显式给出受影响面与最小回归方案。
