# 糯米纸图案自动化修图工具（pattern-tool）

面向 C 端客户的自助修图 Web 工具：客户在浏览器上传蛋糕糯米纸图案 → 服务端自动
去水印 / 背景填充 / 尺寸缩放 / 裁剪 / 描边 → 拿到可打印的透明 PNG 成品，直接发回
聊天窗口。替代运营手动 PS 的重复性标准动作（每单 15+ 步 → 零步）。

```
上传 1-9 张图（可选逐图裁剪；未裁剪图以"统一形状"为默认（自由矩形），打印尺寸整批统一）→ 提交 → 自动修图 → 单张下载或 ZIP 批量打包（桌面下载/复制，移动端长按存相册）→ 发回聊天窗口
```

结果回显走服务端 512px 预览（`?preview=1` 缓存），悬停放大/下载/复制取全幅成品（第四十次修订）；处理中可「取消任务」放弃等待（会话级，后台批次由 TTL 清理，第三十七次修订）。

## 核心链路（五步管线，各步独立跳过）

| 步骤 | 做什么 | 关键口径 |
|---|---|---|
| 去水印 | qwen-vl 语义预检判有无（~¥0.003/次）→ 有才调石榴智能高级版修复（异步提交+轮询，唯一供应商） | 判无 skipped 零外呼；修复失败（欠费/超时）原图零误伤交付，记 failed 前端红显 |
| 填充 | qwen-vl 判棋盘格背景 → qwen-image-2.0 生成式换纯白底 | 非棋盘格（白底/照片）skipped 零外呼；结果缓存同图免重复计费；生成失败原图交付记 failed |
| 裁剪 | 按前端声明（形状+框）运行时执行：框裁外接区 + 形状掩膜塑形（形状外 alpha=0） | 统一形状（无框声明）=**自动默认框**（图内居中最大形状包围盒宽高比框）——与单独裁剪弹层「不动默认框直接确认」同几何（第三十六次修订）；切换统一形状即全局重声明（第三十次修订"后改优先"）；后端只收声明不收像素；同图切形状零重复外呼 |
| 描边 | **外边缘描边**（第三十二次修订）：形状外扩线宽的灰环画在画布外扩边上——内容零接触、环外全透明，沿线剪=线随废料丢弃 | 白底才画线（非白底边界看得见不加线，第三十三次修订）；线宽批级可选 1–2mm（第三十四次修订，默认跟随配置）；矩形四角=线宽级圆角（等宽环） |
| 尺寸缩放 | 按打印尺寸档（4/6/8/10/12 寸 + 自定义 5-33cm，上限=打印机 A3+ 幅面）缩放到 @300DPI 目标短边 | 缩小本地 INTER_AREA；放大走佐糖 scale-pro 高级变清晰（sync=0 异步，服务端定倍）；失败记 failed 前端红显不交付 |

管线全程**原始域**处理（同图不同形状共享全部缓存与模型外呼）；棋盘格图自动换序
先填充再去水印（避免去水印重绘格子致填充判定失效）。
步骤记档语义（2026-08-27 定案）："该做但没做成"（欠费/超时）记 failed 前端红显
"执行失败"；"不需要做"记 skipped——两者都不阻断交付，原图零误伤（quality_hint
提示线已全线撤销）。

## 技术栈与架构

- **后端**：Python 3.11/3.12 · FastAPI · uvicorn（worker=1，SQLite 单写者前提）· OpenCV · rembg
- **存储**：SQLite（WAL，两表：`process_jobs` / `job_images`，DDL 在 `db/schema.sql` 启动自动执行）+ 本机文件系统（原图/结果 PNG，TTL 24h 自动清理）
- **前端**：无构建链原生 HTML/CSS/JS 单页（批级形状/尺寸自绘下拉 + 逐图 cropper.js 裁剪弹层），同进程静态挂载
- **外部 API**：阿里云百炼（qwen-vl 判定 / qwen-image 换白）、石榴智能（去水印）、佐糖 PicWish（scale-pro 超分变清晰）——全部可配置开关，未配 key 时对应步骤零误伤跳过

```
src/
├── app/          # FastAPI 装配：main(工厂) / api(路由) / deps(依赖注入) / lifespan(单例生命周期)
├── core/         # 基础设施：config(pydantic-settings) / database(SQLite 连接) /
│                 # executor(线程池单例) / http(共享事件循环 HTTP 客户端)
├── jobs/         # 批次编排：pipeline(五步管线+状态机) / store(两表读写) / ttl(过期清理+重启恢复)
└── steps/        # 五个算法步：watermark / fill / crop / outline / resize(+imaging 编解码公共)
web/              # 前端单页（index.html + app.js + vendor）
db/schema.sql     # 建表 DDL（JobStore 初始化时读取执行，幂等）
tests/            # pytest（API 校验 / 管线阶段 / 描边像素 / 填充生成 / TTL 恢复 / HTTP 线程模型）
docs/plan/        # 技术方案文档（口径真源，改代码先改文档）
```

## 快速开始

```bash
# 1. 启动（自动建 .venv、装依赖、复制 .env 模板；会先清掉 data/ 旧数据并杀端口占用进程）
./start.sh

# 2. 打开 http://localhost:8200
```

需要 Python 3.11 或 3.12（3.13+ 的 opencv/rembg 轮子未验证）。

其他命令：

```bash
./start.sh init    # 仅初始化/刷新依赖（requirements.txt 变更后）
./start.sh check   # 环境自检，不安装不启动
```

> `start.sh` 的 `run` 分支每次启动会**清空 `data/`**（调试期防旧数据干扰）。
> 生产部署保留数据（TTL 自动清理）时，注释掉脚本里的 `clear_runtime_data` 调用。

## 配置（.env）

复制 `.env.example` 为 `.env` 按需修改；优先级：真实环境变量 > `.env` > 代码默认值。
常用项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `PT_PORT` | 监听端口 | 8200 |
| `PT_DATA_DIR` | 数据根目录（库+图片+缓存+日志） | data |
| `PT_LOG_LEVEL` | 控制台日志级别 | INFO |
| `PT_LOG_FILE_LEVEL` | 文件日志级别（`{data_dir}/logs/app.log`，10MB×5 轮转） | DEBUG |
| `PT_WM_PRECHECK_KEY` / `PT_SHILIU_API_KEY` | 百炼 VL 预检 / 石榴去水印 Key | 空（未配则对应步跳过） |
| `PT_PICWISH_API_KEY` | 佐糖 scale-pro 变清晰 Key | 空（未配则放大 failed） |
| `PT_FILL_GEN_KEY` | 百炼 qwen-image 换白底 Key | 空（未配则填充跳过） |
| `PT_PICWISH_MAX_CONCURRENT` / `PT_DASHSCOPE_MAX_CONCURRENT` / `PT_SHILIU_MAX_CONCURRENT` | 第三方 API 并发闸（在途任务上限，超额排队防 429；0=不限） | 3 / 4 / 2 |
| `PT_CROP_ENABLED` | 是否执行声明式裁剪（关 = 原图直通不塑形） | true |
| `PT_OUTLINE_WIDTH_MM` | 描边线宽（毫米） | 1.5 |
| `PT_JOB_TTL_HOURS` | 批次保留时长 | 24 |

密钥只经环境变量注入，绝不入库、不进前端与日志。全部变量见 `.env.example`（含逐项注释）。

## 测试

```bash
.venv/bin/python -m pytest        # 全量（114 用例约 50s；无需外部 API key，外呼路径均有 mock/降级断言）
```

## 运维要点

- **重启恢复**：启动时悬挂 `processing/queued` 图自动置 failed（话术"服务重启中断，请重新提交"），不留死状态
- **TTL 清理**：每小时扫描，删过期批次文件、两表行与 API 结果缓存（24h 口径）
- **限流**：同 IP 进行中批次 ≤ 9（429）；上传校验 PNG/JPG/WebP ≤ 15MB、单边 60–3600px、同批 SHA-256 查重
- **日志**：去水印/填充链路 DEBUG 级打点（线上排障主径），不记图片内容；失败语义见上文"步骤记档语义"

## 文档

- [`docs/plan/修图工具最小技术方案.md`](docs/plan/修图工具最小技术方案.md) — 模块技术设计唯一真源（需求口径、ER 图、接口契约、修订记录）；**改代码先改文档**
- [`deploy/部署文档.md`](deploy/部署文档.md) — 生产部署（Docker Compose + Nginx 网关域名/HTTPS）
